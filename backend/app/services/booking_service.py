"""Booking — the concurrency-critical path.

There are two genuinely different races here, and they need two different
fixes. Conflating them is the usual mistake.

--------------------------------------------------------------------------
RACE 1: two patients book the same doctor at the same time
--------------------------------------------------------------------------
Already impossible to get wrong at the data level, because Phase 1's exclusion
constraint rejects overlapping appointments for a doctor at write time:

    EXCLUDE USING gist (doctor_id WITH =, tsrange(...) WITH &&)
    WHERE (status <> 'cancelled')

No amount of application-level checking could achieve this. Between a SELECT
that finds the slot free and the INSERT that claims it, another transaction can
run the same SELECT and also find it free. Both commit; the doctor is
double-booked. The constraint is evaluated *by the index during the write*, so
the window does not exist.

What the application still owes is a decent *response*: the loser of the race
must get a clean 409 "that slot was just taken", not a 500. That is what
`SlotTakenError` is for.

--------------------------------------------------------------------------
RACE 2: room capacity
--------------------------------------------------------------------------
This one the database cannot express. "No more than `capacity` overlapping
appointments in this room" is a property of a *set* of rows, not of any single
row, so it cannot be a CHECK (which sees one row) or an EXCLUDE (which compares
pairs). An EXCLUDE could enforce capacity == 1; it cannot enforce capacity == N.

So the naive implementation is a real, classic lost update:

    n = SELECT count(*) ... overlapping in this room   -- both see 1
    if n < capacity:                                   -- both pass
        INSERT                                         -- both insert -> 3 in a 2-room

The fix is to serialise the check and the write by taking a row lock on the room
*before* counting:

    SELECT ... FROM room WHERE id = :id FOR UPDATE

The second transaction blocks on that lock until the first commits, so its count
sees the committed insert. `tests/integration/test_booking_race.py` runs this
race both ways: without the lock it reproduces over-capacity, with the lock it
does not.

Why not just use SERIALIZABLE isolation? It would also work, but it pushes
serialization failures onto *every* booking and requires retry logic throughout.
A row lock scoped to the one resource that needs it is more surgical.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import ColumnElement, and_, func, or_, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Appointment,
    AppointmentStatus,
    Availability,
    Clinic,
    Doctor,
    Patient,
    Room,
    Specialty,
    Urgency,
    Weekday,
    doctor_specialty,
)

log = logging.getLogger(__name__)

# PostgreSQL SQLSTATEs meaning "this transaction lost a contest with another".
# Both are retryable and both mean the same thing to a patient: somebody else
# was booking the same slot at the same moment.
_CONTENTION_SQLSTATES = frozenset(
    {
        "40P01",  # deadlock_detected
        "40001",  # serialization_failure
    }
)


class BookingError(Exception):
    """Base for all refusals to book. Carries a client-safe message."""


class SlotTakenError(BookingError):
    """The doctor is already booked over this interval."""


class RoomFullError(BookingError):
    """The room is at capacity for this interval."""


class OutsideClinicHoursError(BookingError):
    """The requested time falls outside the clinic's operating hours."""


class DoctorUnavailableError(BookingError):
    """The doctor has no availability window covering this interval."""


class SpecialtyMismatchError(BookingError):
    """The doctor does not hold the requested specialty."""


def _overlaps(interval_start: dt.time, interval_end: dt.time) -> ColumnElement[bool]:
    """Half-open overlap predicate, matching the exclusion constraint's `'[)'`.

    Two intervals overlap iff `a.start < b.end AND b.start < a.end`. Using `<`
    rather than `<=` is what makes 09:00-09:30 and 09:30-10:00 non-overlapping,
    exactly as the database constraint treats them. If this disagreed with the
    constraint, the application would report conflicts the database allows (or
    worse, allow ones it rejects, turning a clean 409 into a 500).
    """
    return and_(
        Appointment.start_time < interval_end,
        interval_start < Appointment.end_time,
    )


async def _resolve_duration(db: AsyncSession, specialty_id: int, requested: int | None) -> int:
    if requested is not None:
        return requested
    specialty = await db.get(Specialty, specialty_id)
    if specialty is None:
        raise BookingError("unknown specialty")
    return specialty.default_duration_minutes


def _end_time(start: dt.time, duration_minutes: int) -> dt.time:
    total = start.hour * 60 + start.minute + duration_minutes
    if total >= 24 * 60:
        raise BookingError("appointment would run past midnight")
    return dt.time(total // 60, total % 60)


async def _assert_within_clinic_hours(
    db: AsyncSession, clinic_id: int, start: dt.time, end: dt.time
) -> None:
    """The rule a CHECK constraint cannot express.

    A PostgreSQL CHECK may only reference its own row, and clinic hours live on
    `clinic`. The database enforces absolute outer bounds (06:00-22:00); the
    exact per-clinic window is enforced here.
    """
    clinic = await db.get(Clinic, clinic_id)
    if clinic is None:
        raise BookingError("unknown clinic")
    if start < clinic.opens_at or end > clinic.closes_at:
        raise OutsideClinicHoursError(
            f"clinic is open {clinic.opens_at:%H:%M}-{clinic.closes_at:%H:%M}; "
            f"requested {start:%H:%M}-{end:%H:%M}"
        )


async def _assert_doctor_holds_specialty(
    db: AsyncSession, doctor_id: int, specialty_id: int
) -> None:
    held = await db.scalar(
        select(func.count())
        .select_from(doctor_specialty)
        .where(
            doctor_specialty.c.doctor_id == doctor_id,
            doctor_specialty.c.specialty_id == specialty_id,
        )
    )
    if not held:
        raise SpecialtyMismatchError("this doctor does not hold the requested specialty")


async def _assert_doctor_available(
    db: AsyncSession, doctor_id: int, date: dt.date, start: dt.time, end: dt.time
) -> None:
    """The appointment must sit inside one availability window.

    Inside *one* window, not merely between the day's first and last: a doctor
    working 09:00-12:30 and 13:30-19:00 must not be booked 12:00-14:00, because
    that spans the lunch break. Requiring containment in a single window is how
    mandatory breaks are enforced without modelling breaks separately.
    """
    weekday = Weekday(date.isoweekday())
    covering = await db.scalar(
        select(func.count())
        .select_from(Availability)
        .where(
            Availability.doctor_id == doctor_id,
            Availability.weekday == weekday,
            Availability.is_active.is_(True),
            Availability.start_time <= start,
            Availability.end_time >= end,
            Availability.effective_from <= date,
            or_(Availability.effective_to.is_(None), Availability.effective_to >= date),
        )
    )
    if not covering:
        raise DoctorUnavailableError(
            "the doctor has no availability window covering that interval "
            "(note: an appointment may not span a break)"
        )


async def _assert_room_has_capacity(
    db: AsyncSession,
    room_id: int,
    date: dt.date,
    start: dt.time,
    end: dt.time,
    *,
    use_lock: bool,
    exclude_appointment_id: int | None = None,
) -> None:
    """Check room capacity, optionally taking the row lock that makes it correct.

    `use_lock=False` exists ONLY so the race test can demonstrate the bug. It is
    never used by the application; `book_appointment` always locks.

    The lock is `SELECT ... FOR UPDATE` on the room row. It does not protect the
    room row's data — nothing here modifies it. It is used as a **mutex keyed on
    the room**: any two bookings for the same room serialise, so the second one's
    count runs after the first has committed. Concurrent bookings for *different*
    rooms take different locks and do not block each other.
    """
    if use_lock:
        # FOR UPDATE on the room row. Must happen BEFORE the count, or the
        # count is still reading a snapshot taken before the lock was granted.
        locked = await db.execute(select(Room).where(Room.id == room_id).with_for_update())
        room = locked.scalar_one_or_none()
    else:
        room = await db.get(Room, room_id)

    if room is None:
        raise BookingError("unknown room")

    conditions: list[ColumnElement[bool]] = [
        Appointment.room_id == room_id,
        Appointment.appointment_date == date,
        Appointment.status != AppointmentStatus.CANCELLED,
        _overlaps(start, end),
    ]
    if exclude_appointment_id is not None:
        conditions.append(Appointment.id != exclude_appointment_id)

    concurrent = await db.scalar(select(func.count()).select_from(Appointment).where(*conditions))
    if (concurrent or 0) >= room.capacity:
        raise RoomFullError(
            f"room {room.name!r} holds {room.capacity} concurrent "
            f"appointment(s) and is already full for that interval"
        )


async def book_appointment(
    db: AsyncSession,
    *,
    clinic_id: int,
    doctor_id: int,
    patient_id: int,
    specialty_id: int,
    appointment_date: dt.date,
    start_time: dt.time,
    duration_minutes: int | None = None,
    room_id: int | None = None,
    urgency: Urgency = Urgency.ROUTINE,
    notes: str | None = None,
    _use_room_lock: bool = True,
    _after_capacity_check: Callable[[], Awaitable[None]] | None = None,
) -> Appointment:
    """Book a slot, or raise a `BookingError` explaining why not.

    Order of operations matters. Cheap, deterministic validations run first so a
    clearly invalid request never takes a lock. The room lock is taken last,
    immediately before the insert, to hold it for as short a time as possible.

    Two parameters are test seams, not options:

    * `_use_room_lock=False` disables the room lock so the race test can
      demonstrate the lost update it prevents.
    * `_after_capacity_check` is awaited between the capacity check and the
      insert, letting a test force both transactions to complete their counts
      before either writes. Without it the race would only reproduce
      *sometimes*, and a flaky test proving a concurrency bug is worthless.

    Neither is reachable from the API; both are keyword-only and underscored.
    """
    duration = await _resolve_duration(db, specialty_id, duration_minutes)
    if duration <= 0:
        raise BookingError("duration must be positive")
    end_time = _end_time(start_time, duration)

    doctor = await db.get(Doctor, doctor_id)
    if doctor is None or not doctor.is_active:
        raise BookingError("unknown or inactive doctor")
    patient = await db.get(Patient, patient_id)
    if patient is None:
        raise BookingError("unknown patient")

    await _assert_within_clinic_hours(db, clinic_id, start_time, end_time)
    await _assert_doctor_holds_specialty(db, doctor_id, specialty_id)
    await _assert_doctor_available(db, doctor_id, appointment_date, start_time, end_time)

    if room_id is not None:
        await _assert_room_has_capacity(
            db, room_id, appointment_date, start_time, end_time, use_lock=_use_room_lock
        )

    if _after_capacity_check is not None:
        await _after_capacity_check()

    is_new_patient = not await db.scalar(
        select(func.count()).select_from(Appointment).where(Appointment.patient_id == patient_id)
    )

    appointment = Appointment(
        clinic_id=clinic_id,
        doctor_id=doctor_id,
        patient_id=patient_id,
        specialty_id=specialty_id,
        room_id=room_id,
        appointment_date=appointment_date,
        start_time=start_time,
        end_time=end_time,
        status=AppointmentStatus.SCHEDULED,
        urgency=urgency,
        is_new_patient=bool(is_new_patient),
        notes=notes,
        booked_at=dt.datetime.now(dt.UTC),
    )
    db.add(appointment)

    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        # The exclusion constraint fired: another transaction claimed this slot
        # between our checks and our insert. This is the race the database wins
        # for us; all that is left is to report it properly.
        if "excl_appointment_doctor_no_overlap" in str(exc.orig):
            raise SlotTakenError(
                "that slot was just taken for this doctor; please pick another time"
            ) from exc
        raise
    except DBAPIError as exc:
        # DEADLOCK, and it is a lost race rather than a server fault.
        #
        # Found by CI, not locally: it depends on two transactions reaching the
        # insert close enough together, which local timing happened to avoid.
        #
        # An EXCLUDE constraint does not reject the second writer immediately.
        # PostgreSQL inserts speculatively and makes the conflicting transaction
        # WAIT to see whether the first commits or aborts. When two transactions
        # each claim the same slot at the same instant, each ends up waiting on
        # the other and the deadlock detector aborts one:
        #
        #     Process 213 waits for ShareLock on transaction 833;
        #       blocked by process 214.
        #     Process 214 waits for ShareLock on transaction 832;
        #       blocked by process 213.
        #
        # Semantically this is identical to losing the race: two people wanted
        # one slot and one did not get it. Surfacing it as a 500 would be wrong
        # twice over -- it is not a bug, and it tells the client to give up on
        # something a retry would resolve.
        #
        # 40001 (serialization_failure) is included for the same reason: it is
        # the other way PostgreSQL reports "your transaction lost a contest".
        await db.rollback()
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None) or getattr(
            exc.orig, "pgcode", None
        )
        if sqlstate in _CONTENTION_SQLSTATES:
            raise SlotTakenError(
                "that slot is being booked by someone else right now; please try again"
            ) from exc
        raise

    return appointment


async def get_day_slots(
    db: AsyncSession,
    *,
    doctor_id: int,
    date: dt.date,
    slot_minutes: int = 15,
) -> list[tuple[dt.time, dt.time, bool, str | None]]:
    """Materialise a doctor's day as slots, each marked available or not.

    Computed from availability windows minus existing appointments, so the
    calendar UI never has to reimplement the booking rules. Returns tuples the
    route maps to `SlotOut`.
    """
    weekday = Weekday(date.isoweekday())
    windows = (
        await db.scalars(
            select(Availability)
            .where(
                Availability.doctor_id == doctor_id,
                Availability.weekday == weekday,
                Availability.is_active.is_(True),
                Availability.effective_from <= date,
                or_(Availability.effective_to.is_(None), Availability.effective_to >= date),
            )
            .order_by(Availability.start_time)
        )
    ).all()

    booked = (
        await db.scalars(
            select(Appointment).where(
                Appointment.doctor_id == doctor_id,
                Appointment.appointment_date == date,
                Appointment.status != AppointmentStatus.CANCELLED,
            )
        )
    ).all()

    slots: list[tuple[dt.time, dt.time, bool, str | None]] = []
    for window in windows:
        cursor = window.start_time.hour * 60 + window.start_time.minute
        window_end = window.end_time.hour * 60 + window.end_time.minute
        while cursor + slot_minutes <= window_end:
            start = dt.time(cursor // 60, cursor % 60)
            end = dt.time((cursor + slot_minutes) // 60, (cursor + slot_minutes) % 60)
            clash = next((a for a in booked if a.start_time < end and start < a.end_time), None)
            slots.append((start, end, clash is None, "booked" if clash is not None else None))
            cursor += slot_minutes
    return slots


async def advisory_lock_key(db: AsyncSession, key: int) -> None:
    """Transaction-scoped advisory lock, released automatically on commit/rollback.

    Not used by the booking path (a row lock on `room` is more precise), but kept
    because Phase 4's optimizer needs to serialise whole-day rewrites against
    concurrent manual bookings, and a row lock cannot express "this entire day".
    """
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
