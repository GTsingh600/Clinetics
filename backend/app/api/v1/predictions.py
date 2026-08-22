"""Prediction endpoints.

**Staff only, deliberately.** Every route here requires admin or doctor.

A patient must not see their own predicted no-show score. Two reasons, and both
matter:

* It is a self-fulfilling nudge. Telling someone the system expects them to miss
  their appointment is not neutral information.
* It is a fairness problem. The score is driven partly by that patient's own
  history, so surfacing it to them turns a statistical estimate into something
  that reads as an accusation.

The patient dashboard shows factual attendance history and nothing predictive.
That is why these routes sit behind `require_staff` rather than
`get_current_user`.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import create_engine

from app.api.v1.deps import require_staff
from app.core.config import settings
from app.schemas.predictions import (
    DemandPoint,
    DurationPrediction,
    DurationRequestItem,
    ModelStatus,
    NoShowPrediction,
    NoShowRequestItem,
)
from app.services import inference_service

router = APIRouter(prefix="/predictions", tags=["predictions"])

# The forecasting path is synchronous — pandas and scikit-learn are not async —
# so it uses a sync engine rather than the app's asyncpg one. Created once at
# import so the pool is shared across requests.
_sync_engine = create_engine(settings.database_url_sync, future=True, pool_pre_ping=True)


@router.get("/status", response_model=ModelStatus)
async def model_status() -> ModelStatus:
    """What is loaded, when it was trained, and at what operating point.

    Intentionally detailed: a prediction whose provenance cannot be checked
    should not be trusted, and this is how the Phase 5 agent will cite which
    model version produced a number it is explaining.
    """
    return ModelStatus.model_validate(inference_service.model_status())


@router.post("/no-show", response_model=list[NoShowPrediction])
async def no_show(
    items: list[NoShowRequestItem],
    _: object = Depends(require_staff),
) -> list[NoShowPrediction]:
    if not items:
        return []
    rows = [
        {
            "patient_id": item.patient_id,
            "doctor_id": 0,
            "specialty": item.specialty,
            "appointment_date": item.appointment_date,
            "start_time": item.start_time.isoformat(),
            "urgency": item.urgency.value,
            "is_new_patient": item.is_new_patient,
            "duration_minutes": item.duration_minutes or 30,
            # The prediction is being made now, so "now" is the booking time.
            # It bounds which of the patient's history the features may see.
            "booked_at": dt.datetime.now(dt.UTC),
        }
        for item in items
    ]
    try:
        return [
            NoShowPrediction.model_validate(p)
            for p in inference_service.predict_no_show(_sync_engine, rows)
        ]
    except inference_service.ModelsUnavailableError as exc:
        # 503, not 500: the service is fine, the artifact is simply absent, and
        # the fix is to run the training script rather than to debug a crash.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.get("/demand", response_model=list[DemandPoint])
async def demand(
    specialty: str = Query(description="Specialty slug"),
    start_date: dt.date = Query(),
    end_date: dt.date = Query(),
    _: object = Depends(require_staff),
) -> list[DemandPoint]:
    if end_date < start_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="end_date must not precede start_date",
        )
    if (end_date - start_date).days > 90:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="range is limited to 90 days",
        )
    try:
        return [
            DemandPoint.model_validate(p)
            for p in inference_service.predict_demand(specialty, start_date, end_date)
        ]
    except inference_service.ModelsUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.post("/duration", response_model=list[DurationPrediction])
async def duration(
    items: list[DurationRequestItem],
    _: object = Depends(require_staff),
) -> list[DurationPrediction]:
    """Predict consultation length.

    Unlike the other two, this never returns 503: if no model is loaded it
    falls back to a default duration and says so with `fallback: true`. The
    optimizer always needs *a* number to allocate a slot, and a sensible default
    is more useful to it than an error.
    """
    if not items:
        return []
    rows = [
        {
            "patient_id": item.patient_id,
            "doctor_id": 0,
            "specialty": item.specialty,
            "appointment_date": item.appointment_date,
            "start_time": item.start_time.isoformat(),
            "urgency": item.urgency.value,
            "is_new_patient": item.is_new_patient,
            "booked_at": dt.datetime.now(dt.UTC),
        }
        for item in items
    ]
    return [
        DurationPrediction.model_validate(p)
        for p in inference_service.predict_duration(_sync_engine, rows)
    ]
