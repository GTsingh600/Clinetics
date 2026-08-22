"""Schemas for the optimizer endpoints."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


class OptimizeRequest(BaseModel):
    clinic_id: int = 1
    date: dt.date
    # Named policy rather than raw weights: weights are a clinic decision with
    # measured consequences, and exposing five floats invites tuning by feel.
    policy: str = Field(
        default="balanced",
        description="balanced (prioritise clinician utilisation) or patient_first "
        "(prioritise waiting-room time)",
    )
    allow_overbooking: bool = False
    max_overbooked_slots: int = Field(default=0, ge=0, le=10)
    time_limit_seconds: float = Field(default=15.0, gt=0, le=60)
    persist: bool = Field(default=False, description="Write the proposal to analytics.schedule")


class OptimizeResponse(BaseModel):
    clinic_id: int
    date: str
    appointments: int
    doctors: int
    optimized: dict[str, Any]
    baseline: dict[str, Any]
    improvement: dict[str, float]
    simulated_wait_improvement_pct: float
    schedule_id: int | None = None
    # Populated when the proposal overbooks. The schedule is still returned; it
    # simply cannot be written to the appointment table.
    cannot_apply_reason: str | None = None


class WhatIfRequest(BaseModel):
    clinic_id: int = 1
    date: dt.date
    appointment_id: int
    new_start_time: dt.time


class WhatIfResponse(BaseModel):
    appointment_id: int
    new_start_minute: int
    wait_before_minutes: float
    wait_after_minutes: float
    clinic_mean_wait_before: float
    clinic_mean_wait_after: float
    clinic_overtime_before: float
    clinic_overtime_after: float
