"""Read models for the per-role dashboards.

These are shaped for the screens that consume them rather than mirroring
tables — a dashboard that needed five round-trips to render would be a worse
API even if it were a purer one.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel

from app.schemas.clinical import AppointmentOut


class MetricCard(BaseModel):
    label: str
    value: float
    unit: str | None = None
    # Percentage change against the comparison window; None when there is no
    # prior period to compare against, which the UI must render as "--" rather
    # than as 0%.
    change_pct: float | None = None
    hint: str | None = None


class UtilizationPoint(BaseModel):
    date: dt.date
    booked_minutes: int
    available_minutes: int
    utilization_pct: float


class DoctorUtilizationRow(BaseModel):
    doctor_id: int
    doctor_name: str
    booked_minutes: int
    available_minutes: int
    utilization_pct: float
    scheduled_count: int
    completed_count: int
    cancelled_count: int
    no_show_count: int


class DemandPoint(BaseModel):
    hour: int
    count: int


class AdminDashboard(BaseModel):
    metrics: list[MetricCard]
    utilization_by_doctor: list[DoctorUtilizationRow]
    demand_by_hour: list[DemandPoint]
    utilization_trend: list[UtilizationPoint]


class DoctorDashboard(BaseModel):
    metrics: list[MetricCard]
    today: list[AppointmentOut]
    upcoming: list[AppointmentOut]
    utilization_trend: list[UtilizationPoint]


class PatientDashboard(BaseModel):
    metrics: list[MetricCard]
    upcoming: list[AppointmentOut]
    past: list[AppointmentOut]
