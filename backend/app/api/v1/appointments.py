"""Appointment routes.

Handlers stay thin: parse, authorize, call a service, map errors to status
codes. Every booking rule lives in `services/booking_service.py`, so the agent's
tools and Celery tasks can reach the identical logic in Phase 5.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.deps import get_current_user
from app.core.db import get_db
from app.models import Appointment, AppointmentStatus, Doctor, Patient, User, UserRole
from app.schemas.clinical import (
    AppointmentOut,
    BookingRequest,
    RescheduleRequest,
    SlotOut,
    StatusUpdateRequest,
)
from app.schemas.common import Page
from app.services import booking_service

router = APIRouter(prefix="/appointments", tags=["appointments"])

# Maps each booking refusal to the status code that describes it. 409 for
# "someone else got there first" (retryable, state-dependent), 422 for
# "this request could never be valid" (a client bug, not a race).
_ERROR_STATUS: dict[type[Exception], int] = {
    booking_service.SlotTakenError: status.HTTP_409_CONFLICT,
    booking_service.RoomFullError: status.HTTP_409_CONFLICT,
    booking_service.OutsideClinicHoursError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    booking_service.DoctorUnavailableError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    booking_service.SpecialtyMismatchError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _to_http(exc: booking_service.BookingError) -> HTTPException:
    return HTTPException(
        status_code=_ERROR_STATUS.get(type(exc), status.HTTP_400_BAD_REQUEST),
        detail=str(exc),
    )


_APPOINTMENT_LOADS = (
    selectinload(Appointment.doctor),
    selectinload(Appointment.patient),
    selectinload(Appointment.specialty),
)


async def _load_out(db: AsyncSession, appointment_id: int) -> AppointmentOut:
    """Re-read an appointment with its relationships eagerly loaded.

    Required, not merely tidy. `AppointmentOut` embeds doctor/patient/specialty,
    and Pydantic reads those attributes while serialising. On an async session a
    lazy load at that point raises `MissingGreenlet` ("IO attempted in an
    unexpected place"), because serialisation happens outside the awaited
    context. Every route returning a single AppointmentOut must go through here.
    """
    row = await db.scalar(
        select(Appointment).options(*_APPOINTMENT_LOADS).where(Appointment.id == appointment_id)
    )
    if row is None:  # pragma: no cover - the caller has just written this row
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return AppointmentOut.model_validate(row)


async def _scope_to_caller(stmt: Select[Any], user: User, db: AsyncSession) -> Select[Any]:
    """Restrict a query to what this caller is allowed to see.

    Applied to the *query*, not to the results. Filtering after the fact would
    still have loaded other people's rows into memory, and one forgotten check
    would leak them.
    """
    if user.role is UserRole.ADMIN:
        return stmt
    if user.role is UserRole.DOCTOR:
        doctor_id = await db.scalar(select(Doctor.id).where(Doctor.user_id == user.id))
        return stmt.where(Appointment.doctor_id == doctor_id)
    patient_id = await db.scalar(select(Patient.id).where(Patient.user_id == user.id))
    return stmt.where(Appointment.patient_id == patient_id)


@router.get("", response_model=Page[AppointmentOut])
async def list_appointments(
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    doctor_id: int | None = None,
    patient_id: int | None = None,
    appointment_status: AppointmentStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Page[AppointmentOut]:
    stmt = select(Appointment).options(*_APPOINTMENT_LOADS)
    count_stmt = select(func.count()).select_from(Appointment)

    filters = []
    if date_from is not None:
        filters.append(Appointment.appointment_date >= date_from)
    if date_to is not None:
        filters.append(Appointment.appointment_date <= date_to)
    if doctor_id is not None:
        filters.append(Appointment.doctor_id == doctor_id)
    if patient_id is not None:
        filters.append(Appointment.patient_id == patient_id)
    if appointment_status is not None:
        filters.append(Appointment.status == appointment_status)
    if filters:
        stmt = stmt.where(*filters)
        count_stmt = count_stmt.where(*filters)

    stmt = await _scope_to_caller(stmt, user, db)
    count_stmt = await _scope_to_caller(count_stmt, user, db)

    total = await db.scalar(count_stmt) or 0
    rows = (
        await db.scalars(
            stmt.order_by(Appointment.appointment_date.desc(), Appointment.start_time)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page[AppointmentOut](
        items=[AppointmentOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/slots", response_model=list[SlotOut])
async def day_slots(
    doctor_id: int,
    date: dt.date,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SlotOut]:
    """A doctor's day as bookable slots.

    Derived from availability minus existing appointments by the same service
    the booking path uses, so the calendar cannot disagree with what booking
    will actually accept.
    """
    slots = await booking_service.get_day_slots(db, doctor_id=doctor_id, date=date)
    return [SlotOut(start_time=s, end_time=e, available=a, reason=r) for s, e, a, r in slots]


@router.post("", response_model=AppointmentOut, status_code=status.HTTP_201_CREATED)
async def book(
    payload: BookingRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AppointmentOut:
    # A patient may only book for themselves.
    if user.role is UserRole.PATIENT:
        own = await db.scalar(select(Patient.id).where(Patient.user_id == user.id))
        if own != payload.patient_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="patients may only book for themselves",
            )

    doctor = await db.get(Doctor, payload.doctor_id)
    if doctor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown doctor")

    try:
        appointment = await booking_service.book_appointment(
            db,
            clinic_id=doctor.clinic_id,
            doctor_id=payload.doctor_id,
            patient_id=payload.patient_id,
            specialty_id=payload.specialty_id,
            appointment_date=payload.appointment_date,
            start_time=payload.start_time,
            duration_minutes=payload.duration_minutes,
            room_id=payload.room_id,
            urgency=payload.urgency,
            notes=payload.notes,
        )
    except booking_service.BookingError as exc:
        raise _to_http(exc) from exc

    await db.commit()
    return await _load_out(db, appointment.id)


@router.get("/{appointment_id}", response_model=AppointmentOut)
async def get_appointment(
    appointment_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AppointmentOut:
    stmt = select(Appointment).where(Appointment.id == appointment_id)
    stmt = await _scope_to_caller(stmt, user, db)
    appointment = await db.scalar(stmt)
    if appointment is None:
        # 404 whether it is missing or merely not theirs: a 403 would confirm
        # the id exists and let a caller enumerate appointments.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return await _load_out(db, appointment.id)


@router.post("/{appointment_id}/cancel", response_model=AppointmentOut)
async def cancel(
    appointment_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AppointmentOut:
    """Cancel, releasing the slot.

    A status change, not a delete: the row is training data for the no-show
    model and part of the clinic's history. It also immediately frees the slot,
    because the exclusion constraint ignores cancelled rows.
    """
    stmt = select(Appointment).where(Appointment.id == appointment_id)
    stmt = await _scope_to_caller(stmt, user, db)
    appointment = await db.scalar(stmt)
    if appointment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if appointment.status is AppointmentStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a completed appointment cannot be cancelled",
        )

    appointment.status = AppointmentStatus.CANCELLED
    await db.commit()
    return await _load_out(db, appointment.id)


@router.patch("/{appointment_id}/status", response_model=AppointmentOut)
async def set_status(
    appointment_id: int,
    payload: StatusUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AppointmentOut:
    """Mark an appointment completed or no-show. Staff only.

    Patients must not be able to set their own outcome: `no_show` is the label
    the Phase 3 classifier trains on, so letting the subject write it would
    corrupt the training data.
    """
    if user.role is UserRole.PATIENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="requires role: admin, doctor"
        )
    stmt = select(Appointment).where(Appointment.id == appointment_id)
    stmt = await _scope_to_caller(stmt, user, db)
    appointment = await db.scalar(stmt)
    if appointment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    appointment.status = payload.status
    await db.commit()
    return await _load_out(db, appointment.id)


@router.post("/{appointment_id}/reschedule", response_model=AppointmentOut)
async def reschedule(
    appointment_id: int,
    payload: RescheduleRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AppointmentOut:
    """Move an appointment.

    Implemented as cancel-then-book rather than an in-place UPDATE, so the new
    time passes exactly the same validation as a fresh booking. Cancelling first
    also frees the original slot, which matters when moving within the same
    hour — an in-place update would otherwise collide with itself under the
    exclusion constraint.
    """
    stmt = select(Appointment).where(Appointment.id == appointment_id)
    stmt = await _scope_to_caller(stmt, user, db)
    appointment = await db.scalar(stmt)
    if appointment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if appointment.status is not AppointmentStatus.SCHEDULED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"only a scheduled appointment can be rescheduled (this one is {appointment.status.value})",
        )

    # Captured as typed locals rather than a dict: a dict[str, object] would
    # erase every field's type at the call site below.
    orig_clinic_id = appointment.clinic_id
    orig_doctor_id = appointment.doctor_id
    orig_patient_id = appointment.patient_id
    orig_specialty_id = appointment.specialty_id
    orig_urgency = appointment.urgency
    orig_notes = appointment.notes
    orig_duration = appointment.duration_minutes

    appointment.status = AppointmentStatus.CANCELLED
    await db.flush()

    try:
        moved = await booking_service.book_appointment(
            db,
            clinic_id=orig_clinic_id,
            doctor_id=orig_doctor_id,
            patient_id=orig_patient_id,
            specialty_id=orig_specialty_id,
            appointment_date=payload.appointment_date,
            start_time=payload.start_time,
            duration_minutes=payload.duration_minutes or orig_duration,
            room_id=payload.room_id,
            urgency=orig_urgency,
            notes=orig_notes,
        )
    except booking_service.BookingError as exc:
        # The rollback restores the original appointment: the cancel and the
        # rebook are one transaction, so a failed move leaves the patient with
        # the slot they already had rather than with nothing.
        await db.rollback()
        raise _to_http(exc) from exc

    await db.commit()
    return await _load_out(db, moved.id)


@router.get("/mine/upcoming", response_model=list[AppointmentOut])
async def my_upcoming(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AppointmentOut]:
    stmt = (
        select(Appointment)
        .options(*_APPOINTMENT_LOADS)
        .where(
            Appointment.appointment_date >= dt.date.today(),
            or_(
                Appointment.status == AppointmentStatus.SCHEDULED,
                Appointment.status == AppointmentStatus.COMPLETED,
            ),
        )
    )
    stmt = await _scope_to_caller(stmt, user, db)
    rows = (
        await db.scalars(
            stmt.order_by(Appointment.appointment_date, Appointment.start_time).limit(50)
        )
    ).all()
    return [AppointmentOut.model_validate(r) for r in rows]
