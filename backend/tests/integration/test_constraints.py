"""Every database constraint gets a test that proves it actually fires.

A constraint nobody tests is a constraint you *believe* exists. These tests
assert the database rejects the bad row, not that the application declines to
write one — the distinction matters, because the whole argument for putting
these rules in PostgreSQL is that they hold for writers who bypass the app.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppointmentStatus
from tests.integration.factories import (
    TODAY,
    base_fixtures,
    make_appointment,
    make_clinic,
    make_doctor,
    make_room,
    make_specialty,
)

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------
# CHECK constraints
# --------------------------------------------------------------------------
async def test_appointment_end_must_be_after_start(db: AsyncSession) -> None:
    clinic, doctor, patient, specialty = await base_fixtures(db)
    await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(10, 0),
        end=dt.time(9, 0),
        flush=False,
    )
    # Either CHECK is a correct rejection. `duration_minutes` is a generated
    # column, so an inverted interval computes to -60 and trips
    # ck_appointment_duration_positive first; PostgreSQL does not promise an
    # evaluation order between CHECKs, so the assertion must not depend on one.
    with pytest.raises(IntegrityError, match=r"duration_positive|end_after_start"):
        await db.flush()


async def test_appointment_zero_duration_rejected(db: AsyncSession) -> None:
    """`duration_minutes > 0` and `end_time > start_time` overlap here.

    Both are required, and both are real: duration is a generated column, so
    the duration CHECK also guards against a future change to how it is
    computed.
    """
    clinic, doctor, patient, specialty = await base_fixtures(db)
    await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(10, 0),
        end=dt.time(10, 0),
        flush=False,
    )
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_appointment_outside_day_bounds_rejected(db: AsyncSession) -> None:
    clinic, doctor, patient, specialty = await base_fixtures(db)
    await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(4, 0),  # before the 06:00 global floor
        end=dt.time(5, 0),
        flush=False,
    )
    with pytest.raises(IntegrityError, match="within_clinic_day_bounds"):
        await db.flush()


async def test_appointment_cannot_be_booked_after_it_starts(db: AsyncSession) -> None:
    clinic, doctor, patient, specialty = await base_fixtures(db)
    await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        booked_at=dt.datetime.combine(TODAY, dt.time(12, 0), tzinfo=dt.UTC),
        start=dt.time(9, 0),
        end=dt.time(9, 30),
        flush=False,
    )
    with pytest.raises(IntegrityError, match="booked_before_start"):
        await db.flush()


async def test_clinic_must_close_after_it_opens(db: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="closes_after_opens"):
        # make_clinic flushes internally, so the violation is raised here.
        await make_clinic(db, opens_at=dt.time(18, 0), closes_at=dt.time(8, 0))


async def test_room_capacity_must_be_positive(db: AsyncSession) -> None:
    clinic = await make_clinic(db)
    with pytest.raises(IntegrityError, match="capacity_positive"):
        await make_room(db, clinic, capacity=0)


async def test_generated_duration_matches_start_and_end(db: AsyncSession) -> None:
    """The generated column is computed by PostgreSQL, not by Python."""
    clinic, doctor, patient, specialty = await base_fixtures(db)
    appt = await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(9, 0),
        end=dt.time(9, 45),
    )
    await db.refresh(appt)
    assert appt.duration_minutes == 45


# --------------------------------------------------------------------------
# The exclusion constraint — the centrepiece
# --------------------------------------------------------------------------
async def test_overlapping_appointments_for_same_doctor_rejected(db: AsyncSession) -> None:
    clinic, doctor, patient, specialty = await base_fixtures(db)
    await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(9, 0),
        end=dt.time(9, 30),
    )
    await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(9, 15),  # overlaps the first
        end=dt.time(9, 45),
        flush=False,
    )
    with pytest.raises(IntegrityError, match="excl_appointment_doctor_no_overlap"):
        await db.flush()


async def test_back_to_back_appointments_allowed(db: AsyncSession) -> None:
    """Half-open `'[)'` bounds: 09:00-09:30 and 09:30-10:00 do not overlap.

    With inclusive bounds these would be rejected and ordinary scheduling would
    be impossible, so this test pins the bound style.
    """
    clinic, doctor, patient, specialty = await base_fixtures(db)
    await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(9, 0),
        end=dt.time(9, 30),
    )
    await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(9, 30),
        end=dt.time(10, 0),
    )  # must not raise


async def test_cancelled_appointment_frees_its_slot(db: AsyncSession) -> None:
    """The partial WHERE clause on the exclusion constraint.

    Without `WHERE (status <> 'cancelled')`, cancelling would permanently burn
    the slot — the row would keep blocking a time nobody is attending.
    """
    clinic, doctor, patient, specialty = await base_fixtures(db)
    await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(9, 0),
        end=dt.time(9, 30),
        status=AppointmentStatus.CANCELLED,
    )
    await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(9, 0),
        end=dt.time(9, 30),
    )  # must not raise


async def test_different_doctors_may_share_a_time_slot(db: AsyncSession) -> None:
    clinic, doctor_a, patient, specialty = await base_fixtures(db)
    doctor_b = await make_doctor(db, clinic, license_number="LIC-OTHER", first_name="Ravi")
    for doctor in (doctor_a, doctor_b):
        await make_appointment(
            db,
            clinic=clinic,
            doctor=doctor,
            patient=patient,
            specialty=specialty,
            start=dt.time(9, 0),
            end=dt.time(9, 30),
        )  # must not raise


async def test_same_doctor_may_repeat_slot_on_a_different_date(db: AsyncSession) -> None:
    clinic, doctor, patient, specialty = await base_fixtures(db)
    for offset in (0, 1):
        await make_appointment(
            db,
            clinic=clinic,
            doctor=doctor,
            patient=patient,
            specialty=specialty,
            date=TODAY + dt.timedelta(days=offset),
            start=dt.time(9, 0),
            end=dt.time(9, 30),
        )  # must not raise


# --------------------------------------------------------------------------
# Referential integrity / ON DELETE behaviour
# --------------------------------------------------------------------------
async def test_deleting_doctor_with_appointments_is_restricted(db: AsyncSession) -> None:
    """RESTRICT protects clinical history from a careless delete."""
    clinic, doctor, patient, specialty = await base_fixtures(db)
    await make_appointment(db, clinic=clinic, doctor=doctor, patient=patient, specialty=specialty)
    with pytest.raises(IntegrityError):
        await db.execute(text("DELETE FROM doctor WHERE id = :i"), {"i": doctor.id})
        await db.flush()


async def test_deleting_room_nulls_the_appointment_reference(db: AsyncSession) -> None:
    """SET NULL: decommissioning a room must not delete its appointments."""
    clinic, doctor, patient, specialty = await base_fixtures(db)
    room = await make_room(db, clinic)
    appt = await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        room_id=room.id,
    )
    await db.execute(text("DELETE FROM room WHERE id = :i"), {"i": room.id})
    await db.flush()
    row = (
        await db.execute(text("SELECT room_id FROM appointment WHERE id = :i"), {"i": appt.id})
    ).scalar_one_or_none()
    assert row is None, "appointment should survive with a NULL room_id"


async def test_deleting_doctor_cascades_to_availability(db: AsyncSession) -> None:
    clinic = await make_clinic(db)
    doctor = await make_doctor(db, clinic)
    await db.execute(
        text(
            "INSERT INTO availability "
            "(doctor_id, weekday, start_time, end_time, effective_from, is_active) "
            "VALUES (:d, 'monday', '09:00', '17:00', :f, true)"
        ),
        {"d": doctor.id, "f": TODAY},
    )
    await db.flush()
    await db.execute(text("DELETE FROM doctor WHERE id = :i"), {"i": doctor.id})
    await db.flush()
    remaining = (
        await db.execute(
            text("SELECT count(*) FROM availability WHERE doctor_id = :i"), {"i": doctor.id}
        )
    ).scalar_one()
    assert remaining == 0


async def test_specialty_in_use_cannot_be_deleted(db: AsyncSession) -> None:
    """RESTRICT: deleting a specialty must not silently strip qualifications."""
    clinic = await make_clinic(db)
    specialty = await make_specialty(db, "Dermatology")
    doctor = await make_doctor(db, clinic)
    await db.execute(
        text(
            "INSERT INTO doctor_specialty (doctor_id, specialty_id, is_primary) "
            "VALUES (:d, :s, true)"
        ),
        {"d": doctor.id, "s": specialty.id},
    )
    await db.flush()
    with pytest.raises(IntegrityError):
        await db.execute(text("DELETE FROM specialty WHERE id = :i"), {"i": specialty.id})
        await db.flush()


async def test_doctor_may_hold_at_most_one_primary_specialty(db: AsyncSession) -> None:
    """The partial unique index, which a plain UNIQUE could not express."""
    clinic = await make_clinic(db)
    doctor = await make_doctor(db, clinic)
    cardio = await make_specialty(db, "Cardiology")
    derm = await make_specialty(db, "Dermatology")
    await db.execute(
        text(
            "INSERT INTO doctor_specialty (doctor_id, specialty_id, is_primary) "
            "VALUES (:d, :s, true)"
        ),
        {"d": doctor.id, "s": cardio.id},
    )
    await db.flush()
    with pytest.raises(IntegrityError, match="uq_doctor_specialty_one_primary"):
        await db.execute(
            text(
                "INSERT INTO doctor_specialty (doctor_id, specialty_id, is_primary) "
                "VALUES (:d, :s, true)"
            ),
            {"d": doctor.id, "s": derm.id},
        )
        await db.flush()


async def test_doctor_may_hold_several_non_primary_specialties(db: AsyncSession) -> None:
    """Guards against the naive UNIQUE(doctor_id, is_primary) mistake."""
    clinic = await make_clinic(db)
    doctor = await make_doctor(db, clinic)
    for name in ("Cardiology", "Dermatology", "Neurology"):
        specialty = await make_specialty(db, name)
        await db.execute(
            text(
                "INSERT INTO doctor_specialty (doctor_id, specialty_id, is_primary) "
                "VALUES (:d, :s, false)"
            ),
            {"d": doctor.id, "s": specialty.id},
        )
    await db.flush()  # must not raise


async def test_deleting_user_keeps_the_doctor_record(db: AsyncSession) -> None:
    """SET NULL: revoking portal access must not erase the clinical record."""
    clinic = await make_clinic(db)
    await db.execute(
        text(
            "INSERT INTO user_account (email, hashed_password, role, is_active) "
            "VALUES ('doc@example.test', 'x', 'doctor', true)"
        )
    )
    user_id = (
        await db.execute(text("SELECT id FROM user_account WHERE email='doc@example.test'"))
    ).scalar_one()
    doctor = await make_doctor(db, clinic, user_id=user_id)
    await db.execute(text("DELETE FROM user_account WHERE id = :i"), {"i": user_id})
    await db.flush()
    still_there = (
        await db.execute(text("SELECT user_id FROM doctor WHERE id = :i"), {"i": doctor.id})
    ).scalar_one_or_none()
    assert still_there is None
    count = (
        await db.execute(text("SELECT count(*) FROM doctor WHERE id = :i"), {"i": doctor.id})
    ).scalar_one()
    assert count == 1
