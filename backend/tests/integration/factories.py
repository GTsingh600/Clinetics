"""Minimal object builders for integration tests.

Deliberately plain functions rather than a factory library: the tests are about
database constraints, so the less machinery between the test and the SQL, the
easier a failure is to read.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Appointment,
    AppointmentStatus,
    Clinic,
    Doctor,
    Patient,
    Room,
    Specialty,
    Urgency,
)

TODAY = dt.date(2026, 6, 1)  # a Monday, fixed so tests never depend on "now"


async def make_clinic(db: AsyncSession, **kw: object) -> Clinic:
    defaults = {
        "name": f"Clinic {kw.pop('suffix', '1')}",
        "opens_at": dt.time(8, 0),
        "closes_at": dt.time(18, 0),
        "timezone": "UTC",
    }
    defaults.update(kw)
    clinic = Clinic(**defaults)  # type: ignore[arg-type]
    db.add(clinic)
    await db.flush()
    return clinic


async def make_specialty(db: AsyncSession, name: str = "Cardiology", **kw: object) -> Specialty:
    defaults = {
        "name": name,
        "slug": name.lower().replace(" ", "-"),
        "default_duration_minutes": 30,
    }
    defaults.update(kw)
    specialty = Specialty(**defaults)  # type: ignore[arg-type]
    db.add(specialty)
    await db.flush()
    return specialty


async def make_doctor(db: AsyncSession, clinic: Clinic, **kw: object) -> Doctor:
    defaults = {
        "clinic_id": clinic.id,
        "first_name": "Asha",
        "last_name": "Sharma",
        "license_number": kw.pop("license_number", f"LIC-{id(kw) % 100000}"),
    }
    defaults.update(kw)
    doctor = Doctor(**defaults)  # type: ignore[arg-type]
    db.add(doctor)
    await db.flush()
    return doctor


async def make_patient(db: AsyncSession, **kw: object) -> Patient:
    defaults = {
        "first_name": "Sam",
        "last_name": "Rivera",
        "date_of_birth": dt.date(1990, 5, 17),
    }
    defaults.update(kw)
    patient = Patient(**defaults)  # type: ignore[arg-type]
    db.add(patient)
    await db.flush()
    return patient


async def make_room(db: AsyncSession, clinic: Clinic, **kw: object) -> Room:
    defaults = {"clinic_id": clinic.id, "name": "Room A", "capacity": 1}
    defaults.update(kw)
    room = Room(**defaults)  # type: ignore[arg-type]
    db.add(room)
    await db.flush()
    return room


async def make_appointment(
    db: AsyncSession,
    *,
    clinic: Clinic,
    doctor: Doctor,
    patient: Patient,
    specialty: Specialty,
    date: dt.date = TODAY,
    start: dt.time = dt.time(9, 0),
    end: dt.time = dt.time(9, 30),
    status: AppointmentStatus = AppointmentStatus.SCHEDULED,
    urgency: Urgency = Urgency.ROUTINE,
    booked_at: dt.datetime | None = None,
    flush: bool = True,
    **kw: object,
) -> Appointment:
    appt = Appointment(
        clinic_id=clinic.id,
        doctor_id=doctor.id,
        patient_id=patient.id,
        specialty_id=specialty.id,
        appointment_date=date,
        start_time=start,
        end_time=end,
        status=status,
        urgency=urgency,
        booked_at=booked_at
        or dt.datetime.combine(date, dt.time(0, 0), tzinfo=dt.UTC) - dt.timedelta(days=7),
        **kw,  # type: ignore[arg-type]
    )
    db.add(appt)
    if flush:
        await db.flush()
    return appt


async def base_fixtures(db: AsyncSession) -> tuple[Clinic, Doctor, Patient, Specialty]:
    """The four rows nearly every appointment test needs."""
    clinic = await make_clinic(db)
    specialty = await make_specialty(db)
    doctor = await make_doctor(db, clinic)
    patient = await make_patient(db)
    return clinic, doctor, patient, specialty
