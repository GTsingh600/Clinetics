"""Read-only directory routes: doctors, specialties, rooms, availability.

Grouped into one module because they are all thin reads over reference data.
Splitting them across four files would add navigation cost without adding
structure.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.deps import assert_may_view_patient, get_current_user, require_admin
from app.core.db import get_db
from app.models import Availability, Doctor, Patient, Room, Specialty, User
from app.schemas.clinical import (
    AvailabilityOut,
    DoctorOut,
    PatientOut,
    RoomOut,
    SpecialtyOut,
)

router = APIRouter(tags=["directory"])


@router.get("/specialties", response_model=list[SpecialtyOut])
async def list_specialties(
    _: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[SpecialtyOut]:
    rows = (await db.scalars(select(Specialty).order_by(Specialty.name))).all()
    return [SpecialtyOut.model_validate(r) for r in rows]


@router.get("/doctors", response_model=list[DoctorOut])
async def list_doctors(
    specialty_id: int | None = Query(
        default=None, description="Only doctors holding this specialty"
    ),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DoctorOut]:
    stmt = (
        select(Doctor)
        .options(selectinload(Doctor.specialties))
        .where(Doctor.is_active.is_(True))
        .order_by(Doctor.last_name)
    )
    if specialty_id is not None:
        # Filter through the junction table, which is the whole point of
        # modelling this as many-to-many rather than a string column.
        stmt = stmt.where(Doctor.specialties.any(Specialty.id == specialty_id))
    rows = (await db.scalars(stmt)).all()
    return [DoctorOut.model_validate(r) for r in rows]


@router.get("/doctors/{doctor_id}", response_model=DoctorOut)
async def get_doctor(
    doctor_id: int,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DoctorOut:
    doctor = await db.scalar(
        select(Doctor).options(selectinload(Doctor.specialties)).where(Doctor.id == doctor_id)
    )
    if doctor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return DoctorOut.model_validate(doctor)


@router.get("/doctors/{doctor_id}/availability", response_model=list[AvailabilityOut])
async def doctor_availability(
    doctor_id: int,
    on_date: dt.date | None = Query(
        default=None, description="Only windows effective on this date"
    ),
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AvailabilityOut]:
    stmt = select(Availability).where(
        Availability.doctor_id == doctor_id, Availability.is_active.is_(True)
    )
    if on_date is not None:
        stmt = stmt.where(
            Availability.effective_from <= on_date,
            or_(Availability.effective_to.is_(None), Availability.effective_to >= on_date),
        )
    rows = (await db.scalars(stmt.order_by(Availability.weekday, Availability.start_time))).all()
    return [AvailabilityOut.model_validate(r) for r in rows]


@router.get("/rooms", response_model=list[RoomOut])
async def list_rooms(
    _: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[RoomOut]:
    rows = (
        await db.scalars(select(Room).where(Room.is_active.is_(True)).order_by(Room.name))
    ).all()
    return [RoomOut.model_validate(r) for r in rows]


@router.get("/patients", response_model=list[PatientOut], dependencies=[Depends(require_admin)])
async def list_patients(
    search: str | None = Query(default=None, min_length=2, max_length=80),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[PatientOut]:
    """Admin only. Listing every patient is exactly the endpoint that must not
    be reachable by a patient, so the role check is a route-level dependency
    rather than something a handler could forget."""
    stmt = select(Patient)
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            or_(
                Patient.first_name.ilike(pattern),
                Patient.last_name.ilike(pattern),
                Patient.email.ilike(pattern),
            )
        )
    rows = (await db.scalars(stmt.order_by(Patient.last_name).limit(limit))).all()
    return [PatientOut.model_validate(r) for r in rows]


@router.get("/patients/{patient_id}", response_model=PatientOut)
async def get_patient(
    patient_id: int,
    _: None = Depends(assert_may_view_patient),
    db: AsyncSession = Depends(get_db),
) -> PatientOut:
    """Object-level authorization, not just role-level.

    `assert_may_view_patient` lets staff read anyone but restricts a patient to
    their own record — without it, any logged-in patient could walk the id space
    and read every other patient.
    """
    patient = await db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return PatientOut.model_validate(patient)
