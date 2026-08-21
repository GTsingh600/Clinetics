"""The doctor_utilization trigger must stay correct under every write shape.

These tests deliberately write through raw SQL as well as the ORM. The whole
argument for a trigger over service-layer bookkeeping is that it holds for
writers that never touch the application, so the tests exercise that claim
rather than assuming it.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppointmentStatus
from tests.integration.factories import (
    TODAY,
    base_fixtures,
    make_appointment,
    make_doctor,
)

pytestmark = pytest.mark.integration


async def utilization(db: AsyncSession, doctor_id: int, date: dt.date) -> dict[str, int] | None:
    row = (
        await db.execute(
            text(
                "SELECT scheduled_count, completed_count, cancelled_count, "
                "no_show_count, booked_minutes "
                "FROM analytics.doctor_utilization "
                "WHERE doctor_id = :d AND utilization_date = :dt"
            ),
            {"d": doctor_id, "dt": date},
        )
    ).one_or_none()
    if row is None:
        return None
    return {
        "scheduled": row[0],
        "completed": row[1],
        "cancelled": row[2],
        "no_show": row[3],
        "minutes": row[4],
    }


async def test_insert_creates_and_populates_summary_row(db: AsyncSession) -> None:
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
    stats = await utilization(db, doctor.id, TODAY)
    assert stats == {
        "scheduled": 1,
        "completed": 0,
        "cancelled": 0,
        "no_show": 0,
        "minutes": 30,
    }


async def test_multiple_appointments_accumulate(db: AsyncSession) -> None:
    clinic, doctor, patient, specialty = await base_fixtures(db)
    for start, end in [
        (dt.time(9, 0), dt.time(9, 30)),
        (dt.time(10, 0), dt.time(10, 45)),
        (dt.time(11, 0), dt.time(11, 15)),
    ]:
        await make_appointment(
            db,
            clinic=clinic,
            doctor=doctor,
            patient=patient,
            specialty=specialty,
            start=start,
            end=end,
        )
    stats = await utilization(db, doctor.id, TODAY)
    assert stats is not None
    assert stats["scheduled"] == 3
    assert stats["minutes"] == 30 + 45 + 15


async def test_cancelling_moves_the_count_and_releases_minutes(db: AsyncSession) -> None:
    """Cancelled appointments count, but do not consume booked minutes.

    This must agree with the exclusion constraint's definition of an occupied
    slot, or utilisation would report time the calendar considers free.
    """
    clinic, doctor, patient, specialty = await base_fixtures(db)
    appt = await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(9, 0),
        end=dt.time(9, 30),
    )
    await db.execute(
        text("UPDATE appointment SET status = 'cancelled' WHERE id = :i"), {"i": appt.id}
    )
    stats = await utilization(db, doctor.id, TODAY)
    assert stats == {
        "scheduled": 0,
        "completed": 0,
        "cancelled": 1,
        "no_show": 0,
        "minutes": 0,
    }


async def test_status_transition_to_completed(db: AsyncSession) -> None:
    clinic, doctor, patient, specialty = await base_fixtures(db)
    appt = await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(9, 0),
        end=dt.time(9, 30),
    )
    await db.execute(
        text("UPDATE appointment SET status = 'completed' WHERE id = :i"), {"i": appt.id}
    )
    stats = await utilization(db, doctor.id, TODAY)
    assert stats is not None
    assert stats["scheduled"] == 0
    assert stats["completed"] == 1
    # A completed appointment still consumed the slot.
    assert stats["minutes"] == 30


async def test_no_show_still_consumes_the_slot(db: AsyncSession) -> None:
    clinic, doctor, patient, specialty = await base_fixtures(db)
    appt = await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(9, 0),
        end=dt.time(9, 30),
    )
    await db.execute(
        text("UPDATE appointment SET status = 'no_show' WHERE id = :i"), {"i": appt.id}
    )
    stats = await utilization(db, doctor.id, TODAY)
    assert stats is not None
    assert stats["no_show"] == 1
    assert stats["minutes"] == 30


async def test_delete_removes_the_contribution(db: AsyncSession) -> None:
    clinic, doctor, patient, specialty = await base_fixtures(db)
    appt = await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(9, 0),
        end=dt.time(9, 30),
    )
    await db.execute(text("DELETE FROM appointment WHERE id = :i"), {"i": appt.id})
    stats = await utilization(db, doctor.id, TODAY)
    assert stats is not None
    assert stats["scheduled"] == 0
    assert stats["minutes"] == 0


async def test_duration_change_is_reflected(db: AsyncSession) -> None:
    """The generated duration changes, and the summary must follow it."""
    clinic, doctor, patient, specialty = await base_fixtures(db)
    appt = await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(9, 0),
        end=dt.time(9, 30),
    )
    await db.execute(
        text("UPDATE appointment SET end_time = '10:00' WHERE id = :i"), {"i": appt.id}
    )
    stats = await utilization(db, doctor.id, TODAY)
    assert stats is not None
    assert stats["minutes"] == 60


# --------------------------------------------------------------------------
# The cases a naive trigger gets wrong
# --------------------------------------------------------------------------
async def test_moving_appointment_to_another_date_updates_both_days(
    db: AsyncSession,
) -> None:
    """The case that breaks "just update the summary row" implementations.

    An UPDATE that changes the date must DECREMENT the old day's row and
    INCREMENT the new day's. A trigger keyed only on NEW leaves the old cell
    permanently overstated.
    """
    clinic, doctor, patient, specialty = await base_fixtures(db)
    tomorrow = TODAY + dt.timedelta(days=1)
    appt = await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor,
        patient=patient,
        specialty=specialty,
        start=dt.time(9, 0),
        end=dt.time(9, 30),
    )
    await db.execute(
        text("UPDATE appointment SET appointment_date = :d WHERE id = :i"),
        {"d": tomorrow, "i": appt.id},
    )

    old_day = await utilization(db, doctor.id, TODAY)
    new_day = await utilization(db, doctor.id, tomorrow)
    assert old_day is not None
    assert old_day["scheduled"] == 0, "old day must be decremented"
    assert old_day["minutes"] == 0
    assert new_day is not None
    assert new_day["scheduled"] == 1, "new day must be incremented"
    assert new_day["minutes"] == 30


async def test_reassigning_appointment_to_another_doctor_updates_both(
    db: AsyncSession,
) -> None:
    """Same failure mode, across doctors rather than dates."""
    clinic, doctor_a, patient, specialty = await base_fixtures(db)
    doctor_b = await make_doctor(
        db, clinic, license_number="LIC-MOVE", first_name="Ravi", last_name="Patel"
    )
    appt = await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor_a,
        patient=patient,
        specialty=specialty,
        start=dt.time(9, 0),
        end=dt.time(9, 30),
    )
    await db.execute(
        text("UPDATE appointment SET doctor_id = :d WHERE id = :i"),
        {"d": doctor_b.id, "i": appt.id},
    )

    a = await utilization(db, doctor_a.id, TODAY)
    b = await utilization(db, doctor_b.id, TODAY)
    assert a is not None and a["scheduled"] == 0 and a["minutes"] == 0
    assert b is not None and b["scheduled"] == 1 and b["minutes"] == 30


async def test_moving_doctor_and_date_and_status_at_once(db: AsyncSession) -> None:
    """All three change in one UPDATE — the delta must still balance."""
    clinic, doctor_a, patient, specialty = await base_fixtures(db)
    doctor_b = await make_doctor(
        db, clinic, license_number="LIC-MULTI", first_name="Lena", last_name="Ortiz"
    )
    tomorrow = TODAY + dt.timedelta(days=1)
    appt = await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor_a,
        patient=patient,
        specialty=specialty,
        start=dt.time(9, 0),
        end=dt.time(9, 30),
    )
    await db.execute(
        text(
            "UPDATE appointment SET doctor_id = :d, appointment_date = :dt, "
            "status = 'completed', end_time = '10:00' WHERE id = :i"
        ),
        {"d": doctor_b.id, "dt": tomorrow, "i": appt.id},
    )

    assert (await utilization(db, doctor_a.id, TODAY)) == {
        "scheduled": 0,
        "completed": 0,
        "cancelled": 0,
        "no_show": 0,
        "minutes": 0,
    }
    assert (await utilization(db, doctor_b.id, tomorrow)) == {
        "scheduled": 0,
        "completed": 1,
        "cancelled": 0,
        "no_show": 0,
        "minutes": 60,
    }


async def test_summary_matches_a_full_recount(db: AsyncSession) -> None:
    """Property check: the incremental deltas must equal a from-scratch count.

    This is the test that would catch a drift bug the individual cases miss —
    it recomputes the aggregate directly from `appointment` and compares.
    """
    clinic, doctor, patient, specialty = await base_fixtures(db)
    slots = [
        (dt.time(9, 0), dt.time(9, 30), AppointmentStatus.SCHEDULED),
        (dt.time(10, 0), dt.time(10, 30), AppointmentStatus.COMPLETED),
        (dt.time(11, 0), dt.time(11, 30), AppointmentStatus.CANCELLED),
        (dt.time(12, 0), dt.time(12, 30), AppointmentStatus.NO_SHOW),
        (dt.time(13, 0), dt.time(14, 0), AppointmentStatus.COMPLETED),
    ]
    for start, end, status in slots:
        await make_appointment(
            db,
            clinic=clinic,
            doctor=doctor,
            patient=patient,
            specialty=specialty,
            start=start,
            end=end,
            status=status,
        )

    recount = (
        await db.execute(
            text(
                "SELECT COUNT(*) FILTER (WHERE status='scheduled'), "
                "COUNT(*) FILTER (WHERE status='completed'), "
                "COUNT(*) FILTER (WHERE status='cancelled'), "
                "COUNT(*) FILTER (WHERE status='no_show'), "
                "COALESCE(SUM(duration_minutes) FILTER (WHERE status<>'cancelled'),0) "
                "FROM appointment WHERE doctor_id=:d AND appointment_date=:dt"
            ),
            {"d": doctor.id, "dt": TODAY},
        )
    ).one()

    stats = await utilization(db, doctor.id, TODAY)
    assert stats is not None
    assert (
        stats["scheduled"],
        stats["completed"],
        stats["cancelled"],
        stats["no_show"],
        stats["minutes"],
    ) == tuple(recount)


async def test_trigger_fires_for_writers_that_bypass_the_orm(db: AsyncSession) -> None:
    """The entire justification for using a trigger.

    Inserts via raw SQL, exactly as the data generator's bulk load and a manual
    psql fix would.
    """
    clinic, doctor, patient, specialty = await base_fixtures(db)
    await db.execute(
        text(
            "INSERT INTO appointment (clinic_id, doctor_id, patient_id, specialty_id, "
            "appointment_date, start_time, end_time, booked_at, status, urgency, "
            "is_new_patient) VALUES (:c, :d, :p, :s, :dt, '09:00', '09:30', "
            ":b, 'scheduled', 'routine', false)"
        ),
        {
            "c": clinic.id,
            "d": doctor.id,
            "p": patient.id,
            "s": specialty.id,
            "dt": TODAY,
            "b": dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
        },
    )
    stats = await utilization(db, doctor.id, TODAY)
    assert stats is not None
    assert stats["scheduled"] == 1
    assert stats["minutes"] == 30
