"""Per-role dashboard endpoints.

One endpoint per role rather than one generic endpoint with a `role` parameter.
Each role's dashboard answers a different question and needs a different shape,
and a single endpoint branching on role would end up doing all three queries and
discarding two thirds of the work.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.deps import get_current_doctor, get_current_patient, require_admin
from app.core.db import get_db
from app.models import Appointment, AppointmentStatus, Doctor, Patient
from app.schemas.clinical import AppointmentOut
from app.schemas.dashboard import (
    AdminDashboard,
    DoctorDashboard,
    MetricCard,
    PatientDashboard,
)
from app.services import dashboard_service

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_APPOINTMENT_LOADS = (
    selectinload(Appointment.doctor),
    selectinload(Appointment.patient),
    selectinload(Appointment.specialty),
)


@router.get("/admin", response_model=AdminDashboard, dependencies=[Depends(require_admin)])
async def admin_dashboard(
    days: int = Query(default=30, ge=1, le=365, description="Window ending today"),
    db: AsyncSession = Depends(get_db),
) -> AdminDashboard:
    end = dt.date.today()
    start = end - dt.timedelta(days=days - 1)
    return AdminDashboard(
        metrics=await dashboard_service.headline_metrics(db, start, end),
        utilization_by_doctor=await dashboard_service.utilization_by_doctor(db, start, end),
        demand_by_hour=await dashboard_service.demand_by_hour(db, start, end),
        utilization_trend=await dashboard_service.utilization_trend(db, start, end),
    )


@router.get("/doctor", response_model=DoctorDashboard)
async def doctor_dashboard(
    days: int = Query(default=30, ge=1, le=365),
    doctor: Doctor = Depends(get_current_doctor),
    db: AsyncSession = Depends(get_db),
) -> DoctorDashboard:
    """Scoped to the calling doctor by the dependency, not by a query parameter.

    A `doctor_id` parameter here would be an authorization hole waiting to
    happen: one missing check and any doctor reads another's schedule.
    """
    today = dt.date.today()
    start = today - dt.timedelta(days=days - 1)

    todays = (
        await db.scalars(
            select(Appointment)
            .options(*_APPOINTMENT_LOADS)
            .where(
                Appointment.doctor_id == doctor.id,
                Appointment.appointment_date == today,
                Appointment.status != AppointmentStatus.CANCELLED,
            )
            .order_by(Appointment.start_time)
        )
    ).all()

    upcoming = (
        await db.scalars(
            select(Appointment)
            .options(*_APPOINTMENT_LOADS)
            .where(
                Appointment.doctor_id == doctor.id,
                Appointment.appointment_date > today,
                Appointment.status == AppointmentStatus.SCHEDULED,
            )
            .order_by(Appointment.appointment_date, Appointment.start_time)
            .limit(20)
        )
    ).all()

    booked_today = sum(a.duration_minutes for a in todays)
    return DoctorDashboard(
        metrics=[
            MetricCard(label="Today", value=len(todays), hint="appointments scheduled"),
            MetricCard(label="Booked today", value=round(booked_today / 60, 1), unit="h"),
            MetricCard(label="Upcoming", value=len(upcoming), hint="next 20 scheduled"),
        ],
        today=[AppointmentOut.model_validate(a) for a in todays],
        upcoming=[AppointmentOut.model_validate(a) for a in upcoming],
        utilization_trend=await dashboard_service.utilization_trend(
            db, start, today, doctor_id=doctor.id
        ),
    )


@router.get("/patient", response_model=PatientDashboard)
async def patient_dashboard(
    patient: Patient = Depends(get_current_patient),
    db: AsyncSession = Depends(get_db),
) -> PatientDashboard:
    today = dt.date.today()

    upcoming = (
        await db.scalars(
            select(Appointment)
            .options(*_APPOINTMENT_LOADS)
            .where(
                Appointment.patient_id == patient.id,
                Appointment.appointment_date >= today,
                Appointment.status == AppointmentStatus.SCHEDULED,
            )
            .order_by(Appointment.appointment_date, Appointment.start_time)
        )
    ).all()

    past = (
        await db.scalars(
            select(Appointment)
            .options(*_APPOINTMENT_LOADS)
            .where(
                Appointment.patient_id == patient.id,
                Appointment.appointment_date < today,
            )
            .order_by(Appointment.appointment_date.desc(), Appointment.start_time.desc())
            .limit(20)
        )
    ).all()

    attended = [a for a in past if a.status is AppointmentStatus.COMPLETED]
    missed = [a for a in past if a.status is AppointmentStatus.NO_SHOW]
    resolved = len(attended) + len(missed)

    return PatientDashboard(
        metrics=[
            MetricCard(label="Upcoming", value=len(upcoming)),
            MetricCard(label="Attended", value=len(attended), hint="past appointments"),
            MetricCard(
                label="Missed",
                value=len(missed),
                # Shown to the patient because it is their own record. The
                # Phase 3 model's *prediction* about them is deliberately not
                # exposed here: a predicted-no-show score shown to the subject
                # is both a self-fulfilling nudge and a fairness problem.
                hint=f"{round(len(missed) / resolved * 100)}% of visits" if resolved else None,
            ),
        ],
        upcoming=[AppointmentOut.model_validate(a) for a in upcoming],
        past=[AppointmentOut.model_validate(a) for a in past],
    )
