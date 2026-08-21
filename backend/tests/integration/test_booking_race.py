"""Concurrency safety of the booking path.

CLAUDE.md asks for "a test that reproduces the race *without* the fix and passes
*with* it". There are two distinct races here and they need different fixes, so
both are demonstrated.

**Race 1 — same doctor, same slot.** Phase 1's exclusion constraint makes the
corrupt state unrepresentable, so no application fix is needed for correctness.
What the tests pin is that the loser gets a clean `SlotTakenError` (a 409), not
a 500, and that exactly one booking survives.

**Race 2 — room capacity.** A genuine lost update. "At most `capacity`
overlapping appointments in this room" is a property of a set of rows, so no
CHECK or EXCLUDE can express it, and the naive count-then-insert has a real
time-of-check/time-of-use window. `test_room_capacity_race_WITHOUT_lock`
reproduces over-capacity; `test_room_capacity_race_WITH_lock` shows
`SELECT ... FOR UPDATE` closing it.

These tests use real, separately-committing transactions on separate
connections. Two sessions sharing one connection cannot contend for a lock, so
the usual rollback-per-test fixture would quietly turn every assertion here into
a tautology.

The interleaving is forced with `asyncio.Barrier` rather than left to timing: a
concurrency test that only fails sometimes is not evidence of anything.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Appointment, Availability, Clinic, Doctor, Patient, Room, Specialty, Weekday
from app.services import booking_service

pytestmark = pytest.mark.integration

# A Monday, comfortably in the future so the "not in the past" rules never bite.
RACE_DATE = dt.date.today() + dt.timedelta(days=(7 - dt.date.today().weekday()) % 7 + 7)
SLOT_START = dt.time(10, 0)


async def _seed(session: AsyncSession, *, room_capacity: int = 2) -> dict[str, int]:
    """Create the minimum world two concurrent bookings need, and COMMIT it.

    Committed, not flushed: the other transaction runs on a different connection
    and would not otherwise see any of it.
    """
    clinic = Clinic(
        name=f"Race Clinic {dt.datetime.now().timestamp()}",
        opens_at=dt.time(8, 0),
        closes_at=dt.time(18, 0),
    )
    session.add(clinic)
    await session.flush()

    specialty = Specialty(name="Race Medicine", slug="race-medicine", default_duration_minutes=30)
    session.add(specialty)
    await session.flush()

    room = Room(clinic_id=clinic.id, name="Shared Bay", capacity=room_capacity)
    session.add(room)

    doctors: list[Doctor] = []
    for i in range(2):
        doctor = Doctor(
            clinic_id=clinic.id,
            first_name=f"Race{i}",
            last_name="Doctor",
            license_number=f"RACE-{i}-{dt.datetime.now().timestamp()}",
        )
        session.add(doctor)
        await session.flush()
        await session.execute(
            text(
                "INSERT INTO doctor_specialty (doctor_id, specialty_id, is_primary) "
                "VALUES (:d, :s, true)"
            ),
            {"d": doctor.id, "s": specialty.id},
        )
        session.add(
            Availability(
                doctor_id=doctor.id,
                weekday=Weekday(RACE_DATE.isoweekday()),
                start_time=dt.time(8, 0),
                end_time=dt.time(18, 0),
                effective_from=dt.date(2020, 1, 1),
                is_active=True,
            )
        )
        doctors.append(doctor)

    patients: list[Patient] = []
    for i in range(4):
        patient = Patient(
            first_name=f"Race{i}", last_name="Patient", date_of_birth=dt.date(1990, 1, 1)
        )
        session.add(patient)
        patients.append(patient)

    await session.flush()
    await session.commit()

    return {
        "clinic_id": clinic.id,
        "specialty_id": specialty.id,
        "room_id": room.id,
        "doctor_a": doctors[0].id,
        "doctor_b": doctors[1].id,
        "patients": [p.id for p in patients],
    }


# ==========================================================================
# Race 1 — two patients, same doctor, same slot
# ==========================================================================
async def test_concurrent_booking_same_slot_only_one_wins(
    committing_sessions,
) -> None:
    """The exclusion constraint decides the winner; the loser gets a clean error.

    Both transactions pass their pre-checks (the slot genuinely is free when
    each looks), then both insert. The database rejects the second. Without the
    constraint, both would commit and the doctor would be double-booked.
    """
    setup_session = committing_sessions()
    ids = await _seed(setup_session)

    barrier = asyncio.Barrier(2)

    async def attempt(patient_id: int) -> str:
        session = committing_sessions()
        try:
            await booking_service.book_appointment(
                session,
                clinic_id=ids["clinic_id"],
                doctor_id=ids["doctor_a"],
                patient_id=patient_id,
                specialty_id=ids["specialty_id"],
                appointment_date=RACE_DATE,
                start_time=SLOT_START,
                duration_minutes=30,
                # Both transactions reach the insert together, so the conflict
                # is guaranteed rather than dependent on scheduling luck.
                _after_capacity_check=barrier.wait,
            )
            await session.commit()
            return "booked"
        except booking_service.SlotTakenError:
            await session.rollback()
            return "slot_taken"
        finally:
            await session.close()

    results = await asyncio.gather(
        attempt(ids["patients"][0]), attempt(ids["patients"][1]), return_exceptions=True
    )

    assert not any(
        isinstance(r, BaseException) for r in results
    ), f"a booking raised something other than SlotTakenError: {results}"
    assert sorted(results) == ["booked", "slot_taken"], results

    verify = committing_sessions()
    count = await verify.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.doctor_id == ids["doctor_a"],
            Appointment.appointment_date == RACE_DATE,
        )
    )
    assert count == 1, f"expected exactly one appointment to survive, found {count}"


async def test_back_to_back_concurrent_bookings_both_succeed(
    committing_sessions,
) -> None:
    """Adjacent slots must not be treated as a conflict.

    Guards against "fixing" the race by over-locking: a lock on the doctor's
    whole day would make this fail, and would serialise every booking in the
    clinic for no reason.
    """
    setup_session = committing_sessions()
    ids = await _seed(setup_session)
    barrier = asyncio.Barrier(2)

    async def attempt(patient_id: int, start: dt.time) -> str:
        session = committing_sessions()
        try:
            await booking_service.book_appointment(
                session,
                clinic_id=ids["clinic_id"],
                doctor_id=ids["doctor_a"],
                patient_id=patient_id,
                specialty_id=ids["specialty_id"],
                appointment_date=RACE_DATE,
                start_time=start,
                duration_minutes=30,
                _after_capacity_check=barrier.wait,
            )
            await session.commit()
            return "booked"
        except booking_service.BookingError as exc:
            await session.rollback()
            return f"failed: {exc}"
        finally:
            await session.close()

    results = await asyncio.gather(
        attempt(ids["patients"][0], dt.time(10, 0)),
        attempt(ids["patients"][1], dt.time(10, 30)),
    )
    assert results == ["booked", "booked"], results


# ==========================================================================
# Race 2 — room capacity: the genuine lost update
# ==========================================================================
async def test_room_capacity_race_WITHOUT_lock_reproduces_overbooking(  # noqa: N802
    committing_sessions,
) -> None:
    """THE BUG. Without the row lock, capacity is exceeded.

    Two transactions each count the room's overlapping appointments, both see
    0 < 2, and both insert. A third would too. The count and the insert are not
    atomic with respect to each other, and no database constraint can catch it
    because capacity is a property of the *set*, not of any row.

    Different doctors on purpose: the exclusion constraint must not be the thing
    that stops this, or the test would be measuring the wrong mechanism.

    This test asserts the bug REPRODUCES. If it ever starts failing, either the
    lock was applied unconditionally (good, but then delete this test) or the
    seam was removed.
    """
    setup_session = committing_sessions()
    ids = await _seed(setup_session, room_capacity=1)

    barrier = asyncio.Barrier(2)

    async def attempt(patient_id: int, doctor_id: int) -> str:
        session = committing_sessions()
        try:
            await booking_service.book_appointment(
                session,
                clinic_id=ids["clinic_id"],
                doctor_id=doctor_id,
                patient_id=patient_id,
                specialty_id=ids["specialty_id"],
                appointment_date=RACE_DATE,
                start_time=SLOT_START,
                duration_minutes=30,
                room_id=ids["room_id"],
                _use_room_lock=False,  # ← the bug under test
                _after_capacity_check=barrier.wait,
            )
            await session.commit()
            return "booked"
        except booking_service.RoomFullError:
            await session.rollback()
            return "room_full"
        finally:
            await session.close()

    results = await asyncio.gather(
        attempt(ids["patients"][0], ids["doctor_a"]),
        attempt(ids["patients"][1], ids["doctor_b"]),
    )

    verify = committing_sessions()
    in_room = await verify.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.room_id == ids["room_id"],
            Appointment.appointment_date == RACE_DATE,
        )
    )

    assert results == ["booked", "booked"], results
    assert in_room == 2, (
        "expected the unlocked path to overbook a capacity-1 room; " f"found {in_room} appointments"
    )


async def test_room_capacity_race_WITH_lock_holds_the_limit(  # noqa: N802
    committing_sessions,
) -> None:
    """THE FIX. `SELECT ... FOR UPDATE` on the room row serialises the check.

    The second transaction blocks on the lock until the first commits, so its
    count sees the committed insert and it correctly refuses.

    No barrier here, deliberately: with the lock in place the second transaction
    would block *before* reaching the hook, and a barrier waiting for it would
    deadlock. The assertion is timing-independent anyway — the lock guarantees
    exactly one winner however the two are scheduled.
    """
    setup_session = committing_sessions()
    ids = await _seed(setup_session, room_capacity=1)

    async def attempt(patient_id: int, doctor_id: int) -> str:
        session = committing_sessions()
        try:
            await booking_service.book_appointment(
                session,
                clinic_id=ids["clinic_id"],
                doctor_id=doctor_id,
                patient_id=patient_id,
                specialty_id=ids["specialty_id"],
                appointment_date=RACE_DATE,
                start_time=SLOT_START,
                duration_minutes=30,
                room_id=ids["room_id"],
                _use_room_lock=True,  # ← the fix
            )
            await session.commit()
            return "booked"
        except booking_service.RoomFullError:
            await session.rollback()
            return "room_full"
        finally:
            await session.close()

    results = await asyncio.gather(
        attempt(ids["patients"][0], ids["doctor_a"]),
        attempt(ids["patients"][1], ids["doctor_b"]),
    )

    verify = committing_sessions()
    in_room = await verify.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.room_id == ids["room_id"],
            Appointment.appointment_date == RACE_DATE,
        )
    )

    assert sorted(results) == ["booked", "room_full"], results
    assert in_room == 1, f"capacity-1 room should hold exactly 1 appointment, found {in_room}"


async def test_locks_on_different_rooms_do_not_serialise(
    committing_sessions,
) -> None:
    """The lock must be per-room, not global.

    A coarse lock would also make the capacity test pass while destroying
    throughput, so this pins that concurrent bookings into *different* rooms
    both succeed.
    """
    setup_session = committing_sessions()
    ids = await _seed(setup_session, room_capacity=1)

    second_room = Room(clinic_id=ids["clinic_id"], name="Second Bay", capacity=1)
    setup_session.add(second_room)
    await setup_session.commit()

    async def attempt(patient_id: int, doctor_id: int, room_id: int) -> str:
        session = committing_sessions()
        try:
            await booking_service.book_appointment(
                session,
                clinic_id=ids["clinic_id"],
                doctor_id=doctor_id,
                patient_id=patient_id,
                specialty_id=ids["specialty_id"],
                appointment_date=RACE_DATE,
                start_time=SLOT_START,
                duration_minutes=30,
                room_id=room_id,
            )
            await session.commit()
            return "booked"
        except booking_service.BookingError as exc:
            await session.rollback()
            return f"failed: {exc}"
        finally:
            await session.close()

    results = await asyncio.gather(
        attempt(ids["patients"][0], ids["doctor_a"], ids["room_id"]),
        attempt(ids["patients"][1], ids["doctor_b"], second_room.id),
    )
    assert results == ["booked", "booked"], results
