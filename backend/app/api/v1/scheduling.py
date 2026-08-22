"""Optimizer endpoints.

Staff only. Producing a clinic-wide schedule is an operational act, and the
response exposes every doctor's day.

Two routes, answering two different questions:

* `POST /optimize`  — "what is the best schedule for this day, and how much
  better is it than what a booking system would do?"
* `POST /what-if`   — "what happens to my waiting time if I move?" — the
  single-patient question, answered by re-simulating the whole day so the
  effect on everyone else is visible too.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import create_engine

from app.api.v1.deps import require_staff
from app.core.config import settings
from app.schemas.scheduling import (
    OptimizeRequest,
    OptimizeResponse,
    WhatIfRequest,
    WhatIfResponse,
)
from app.services import optimizer_service
from optimizer import greedy, model, simulate
from optimizer.objective import DEFAULT_WEIGHTS, PATIENT_FIRST_WEIGHTS
from optimizer.score import score_solution
from optimizer.types import to_minutes

router = APIRouter(prefix="/scheduling", tags=["scheduling"])

# CP-SAT and the simulator are synchronous, so this uses the sync engine rather
# than the app's asyncpg one.
_sync_engine = create_engine(settings.database_url_sync, future=True, pool_pre_ping=True)

_POLICIES = {"balanced": DEFAULT_WEIGHTS, "patient_first": PATIENT_FIRST_WEIGHTS}


@router.post("/optimize", response_model=OptimizeResponse)
async def optimize(
    payload: OptimizeRequest,
    _: object = Depends(require_staff),
) -> OptimizeResponse:
    """Optimize one clinic-day and compare it against the FCFS baseline."""
    weights = _POLICIES.get(payload.policy)
    if weights is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown policy {payload.policy!r}; expected one of {sorted(_POLICIES)}",
        )

    try:
        result = optimizer_service.optimize_day(
            _sync_engine,
            payload.clinic_id,
            payload.date,
            weights=weights,
            allow_overbooking=payload.allow_overbooking,
            max_overbooked_slots=payload.max_overbooked_slots,
            time_limit_seconds=payload.time_limit_seconds,
        )
    except optimizer_service.NoSuchClinicDayError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except model.InfeasibleScheduleError as exc:
        # 422, not 500: the day genuinely cannot be scheduled under the stated
        # constraints, and the message names which appointments are the problem.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    solution = result.pop("solution")
    request = result.pop("request")

    schedule_id = None
    if payload.persist:
        schedule_id = optimizer_service.persist_schedule(
            _sync_engine,
            request,
            solution,
            score_solution(solution, request, weights),
            weights=weights,
        )

    return OptimizeResponse(
        **result,
        schedule_id=schedule_id,
        # Non-null when the proposal overbooks: the schedule is returned and
        # stored, but cannot be written to the appointment table.
        cannot_apply_reason=optimizer_service.refuse_to_apply(solution),
    )


@router.post("/what-if", response_model=WhatIfResponse)
async def what_if(
    payload: WhatIfRequest,
    _: object = Depends(require_staff),
) -> WhatIfResponse:
    """How would moving one appointment change waiting time?

    Simulated against the CURRENT booked schedule, not an optimised one: the
    question is about the day as it stands. The response includes the effect on
    the clinic as a whole, because an improvement for one patient is usually
    taken from somebody else.
    """
    try:
        request = optimizer_service.build_request(_sync_engine, payload.clinic_id, payload.date)
    except optimizer_service.NoSuchClinicDayError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if not any(a.appointment_id == payload.appointment_id for a in request.appointments):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"appointment {payload.appointment_id} is not scheduled on {payload.date}",
        )

    current = greedy.solve(request)
    result = simulate.what_if_moved(
        current,
        request,
        appointment_id=payload.appointment_id,
        new_start_minute=to_minutes(payload.new_start_time),
    )
    return WhatIfResponse.model_validate(result)
