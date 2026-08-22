"""Request/response schemas for the prediction endpoints."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

from app.models.enums import Urgency


class NoShowRequestItem(BaseModel):
    """One appointment to score.

    Deliberately accepts appointments that do not exist yet: the model predicts
    at booking time, so the useful moment to ask is *before* the row is written.
    """

    patient_id: int
    specialty: str = Field(description="Specialty slug, e.g. 'cardiology'")
    appointment_date: dt.date
    start_time: dt.time
    urgency: Urgency = Urgency.ROUTINE
    is_new_patient: bool = False
    duration_minutes: int | None = None


class NoShowPrediction(BaseModel):
    patient_id: int
    appointment_date: str
    start_time: str
    no_show_probability: float = Field(ge=0.0, le=1.0)
    flagged: bool = Field(description="Whether the probability clears the operating threshold")
    threshold: float


class DemandPoint(BaseModel):
    specialty: str
    date: str
    hour: int
    predicted_demand: float


class DurationRequestItem(BaseModel):
    patient_id: int
    specialty: str
    appointment_date: dt.date
    start_time: dt.time
    urgency: Urgency = Urgency.ROUTINE
    is_new_patient: bool = False


class DurationPrediction(BaseModel):
    patient_id: int
    specialty: str
    predicted_duration_minutes: float
    fallback: bool = Field(description="True when no model is loaded and a default was substituted")


class ModelInfo(BaseModel):
    trained_at: str | None = None
    git_sha: str | None = None
    seed: int | None = None
    train_rows: int | None = None
    train_date_range: list[str] | None = None
    threshold: float | None = None
    threshold_rationale: str | None = None
    n_features: int | None = None


class ModelStatus(BaseModel):
    available: bool
    artifacts_dir: str
    models: dict[str, ModelInfo]
