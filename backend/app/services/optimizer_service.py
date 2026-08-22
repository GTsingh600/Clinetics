"""Connects the pure optimizer to the database and to Phase 3's models.

`optimizer/` cannot import SQLAlchemy — CI enforces it — so this module does all
three impure jobs:

1. Read a clinic-day out of Postgres and build a `ScheduleRequest`.
2. Enrich it with predictions: expected duration and no-show probability.
3. Persist a `Solution` into `analytics.schedule` / `analytics.schedule_entry`.

**Predictions are enrichment, not a dependency.** If no model artifacts exist,
the booked duration and a zero no-show probability are used instead, and the
result records that it ran unenriched. A scheduler that refuses to run because
an ML artifact is missing is worse than one that schedules slightly less well.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.services import inference_service
from optimizer import greedy, model, simulate
from optimizer.objective import DEFAULT_WEIGHTS, OverbookingPolicy, Weights
from optimizer.score import ScheduleScore, improvement, score_solution
from optimizer.types import (
    AppointmentRequest,
    DoctorDay,
    RoomCapacity,
    ScheduleRequest,
    Solution,
    to_minutes,
    to_time,
)

log = logging.getLogger(__name__)

_APPOINTMENTS_SQL = """
SELECT a.id, a.patient_id, a.doctor_id, a.specialty_id, s.slug AS specialty,
       a.start_time, a.duration_minutes, a.urgency::text AS urgency,
       a.is_new_patient, a.room_id
FROM appointment a
JOIN specialty s ON s.id = a.specialty_id
WHERE a.clinic_id = :clinic_id
  AND a.appointment_date = :on_date
  AND a.status <> 'cancelled'
ORDER BY a.start_time, a.id
"""

# Availability is stored per weekday as separate windows either side of lunch,
# so the break arrives as a gap and needs no special handling here.
_AVAILABILITY_SQL = """
SELECT av.doctor_id, av.start_time, av.end_time
FROM availability av
WHERE av.is_active
  AND av.weekday = CAST(:weekday AS weekday)
  AND av.effective_from <= :on_date
  AND (av.effective_to IS NULL OR av.effective_to >= :on_date)
  AND av.doctor_id = ANY(:doctor_ids)
ORDER BY av.doctor_id, av.start_time
"""

_SPECIALTIES_SQL = """
SELECT doctor_id, specialty_id FROM doctor_specialty WHERE doctor_id = ANY(:doctor_ids)
"""

_CLINIC_SQL = "SELECT opens_at, closes_at FROM clinic WHERE id = :clinic_id"

_WEEKDAY_NAMES = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


class NoSuchClinicDayError(RuntimeError):
    """Nothing to schedule for that clinic and date."""


def build_request(
    engine: Engine,
    clinic_id: int,
    on_date: dt.date,
    *,
    use_predictions: bool = True,
    allow_overbooking: bool = False,
    max_overbooked_slots: int = 0,
) -> ScheduleRequest:
    """Assemble one clinic-day into a pure `ScheduleRequest`."""
    with Session(engine) as session:
        clinic = session.execute(text(_CLINIC_SQL), {"clinic_id": clinic_id}).mappings().first()
        if clinic is None:
            raise NoSuchClinicDayError(f"no clinic {clinic_id}")

        rows = (
            session.execute(text(_APPOINTMENTS_SQL), {"clinic_id": clinic_id, "on_date": on_date})
            .mappings()
            .all()
        )
        if not rows:
            raise NoSuchClinicDayError(f"clinic {clinic_id} has no appointments on {on_date}")

        doctor_ids = sorted({int(r["doctor_id"]) for r in rows})
        weekday = _WEEKDAY_NAMES[on_date.weekday()]

        availability = (
            session.execute(
                text(_AVAILABILITY_SQL),
                {"weekday": weekday, "on_date": on_date, "doctor_ids": doctor_ids},
            )
            .mappings()
            .all()
        )

        specialties = (
            session.execute(text(_SPECIALTIES_SQL), {"doctor_ids": doctor_ids}).mappings().all()
        )

        room_rows = (
            session.execute(
                text("SELECT id, capacity FROM room WHERE clinic_id = :c AND is_active"),
                {"c": clinic_id},
            )
            .mappings()
            .all()
        )

    windows: dict[int, list[tuple[int, int]]] = {}
    for row in availability:
        windows.setdefault(int(row["doctor_id"]), []).append(
            (to_minutes(row["start_time"]), to_minutes(row["end_time"]))
        )

    held: dict[int, set[int]] = {}
    for row in specialties:
        held.setdefault(int(row["doctor_id"]), set()).add(int(row["specialty_id"]))

    doctors = tuple(
        DoctorDay(
            doctor_id=doctor_id,
            windows=tuple(sorted(windows.get(doctor_id, []))),
            specialty_ids=frozenset(held.get(doctor_id, set())),
        )
        for doctor_id in doctor_ids
        # A doctor with no availability on this weekday cannot be scheduled.
        # Their appointments will be reported by _validate rather than silently
        # placed outside working hours.
        if windows.get(doctor_id)
    )

    predictions = _predictions_for(engine, rows, on_date) if use_predictions else {}

    appointments = tuple(
        AppointmentRequest(
            appointment_id=int(row["id"]),
            patient_id=int(row["patient_id"]),
            doctor_id=int(row["doctor_id"]),
            specialty_id=int(row["specialty_id"]),
            requested_start_minute=to_minutes(row["start_time"]),
            duration_minutes=int(
                predictions.get(int(row["id"]), {}).get("duration", row["duration_minutes"])
            ),
            urgency=str(row["urgency"]),
            no_show_probability=float(predictions.get(int(row["id"]), {}).get("no_show", 0.0)),
            room_id=int(row["room_id"]) if row["room_id"] is not None else None,
        )
        for row in rows
    )

    return ScheduleRequest(
        clinic_id=clinic_id,
        date=on_date,
        open_minute=to_minutes(clinic["opens_at"]),
        close_minute=to_minutes(clinic["closes_at"]),
        appointments=appointments,
        doctors=doctors,
        rooms=tuple(
            RoomCapacity(room_id=int(r["id"]), capacity=int(r["capacity"])) for r in room_rows
        ),
        allow_overbooking=allow_overbooking,
        max_overbooked_slots=max_overbooked_slots,
    )


def _predictions_for(
    engine: Engine, rows: Sequence[Any], on_date: dt.date
) -> dict[int, dict[str, float]]:
    """Predicted duration and no-show probability, or nothing if unavailable.

    Degrades rather than raising: without artifacts the optimizer falls back to
    booked durations, which is a slightly worse schedule rather than no schedule.
    """
    payload = [
        {
            "patient_id": int(row["patient_id"]),
            "specialty": str(row["specialty"]),
            "appointment_date": on_date,
            "start_time": row["start_time"].isoformat(),
            "urgency": str(row["urgency"]),
            "is_new_patient": bool(row["is_new_patient"]),
        }
        for row in rows
    ]

    out: dict[int, dict[str, float]] = {}
    try:
        durations = inference_service.predict_duration(engine, payload)
        for row, prediction in zip(rows, durations, strict=True):
            out.setdefault(int(row["id"]), {})["duration"] = round(
                prediction["predicted_duration_minutes"]
            )
    except Exception as exc:
        log.warning("duration predictions unavailable (%s); using booked durations", exc)

    try:
        no_shows = inference_service.predict_no_show(engine, payload)
        for row, prediction in zip(rows, no_shows, strict=True):
            out.setdefault(int(row["id"]), {})["no_show"] = prediction["no_show_probability"]
    except Exception as exc:
        log.warning("no-show predictions unavailable (%s); assuming full attendance", exc)

    return out


def optimize_day(
    engine: Engine,
    clinic_id: int,
    on_date: dt.date,
    *,
    weights: Weights = DEFAULT_WEIGHTS,
    allow_overbooking: bool = False,
    max_overbooked_slots: int = 0,
    time_limit_seconds: float = 20.0,
    simulate_runs: int = 200,
) -> dict[str, Any]:
    """Optimize a day, score it against the greedy baseline, and simulate both."""
    request = build_request(
        engine,
        clinic_id,
        on_date,
        allow_overbooking=allow_overbooking,
        max_overbooked_slots=max_overbooked_slots,
    )

    policy = OverbookingPolicy(
        enabled=allow_overbooking,
        max_slots=max_overbooked_slots,
        max_per_slot=2 if allow_overbooking else 1,
    )
    optimized = model.solve(
        request, weights=weights, overbooking=policy, time_limit_seconds=time_limit_seconds
    )
    baseline = greedy.solve(request)

    optimized_score = score_solution(optimized, request, weights)
    baseline_score = score_solution(baseline, request, weights)

    # Same seed for both, so they see identical sampled durations and
    # attendance. A paired comparison: any difference is the schedule, not luck.
    optimized_sim = simulate.simulate(optimized, request, runs=simulate_runs, seed=42)
    baseline_sim = simulate.simulate(baseline, request, runs=simulate_runs, seed=42)

    return {
        "clinic_id": clinic_id,
        "date": str(on_date),
        "appointments": len(request.appointments),
        "doctors": len(request.doctors),
        "optimized": {
            "status": optimized.solver_status,
            "solve_time_ms": optimized.solve_time_ms,
            "score": optimized_score.as_dict(),
            "simulation": optimized_sim.as_dict(),
        },
        "baseline": {
            "status": baseline.solver_status,
            "solve_time_ms": baseline.solve_time_ms,
            "score": baseline_score.as_dict(),
            "simulation": baseline_sim.as_dict(),
        },
        "improvement": improvement(baseline_score, optimized_score),
        "simulated_wait_improvement_pct": _pct(
            baseline_sim.mean_wait_minutes, optimized_sim.mean_wait_minutes
        ),
        "solution": optimized,
        "request": request,
    }


def _pct(before: float, after: float) -> float:
    if before <= 0:
        return 0.0
    return round((before - after) / before * 100.0, 2)


def persist_schedule(
    engine: Engine,
    request: ScheduleRequest,
    solution: Solution,
    score: ScheduleScore,
    *,
    weights: Weights = DEFAULT_WEIGHTS,
    is_baseline: bool = False,
) -> int:
    """Write a solution into `analytics.schedule` and return its id.

    Writes to the ANALYTICS schema, never to `appointment`. That is the point:
    a generated schedule is a *proposal*, and Phase 1 separated proposals from
    the transactional record precisely so one can exist without disturbing the
    other. It is also what makes an overbooked proposal representable at all —
    `analytics.schedule_entry` has no exclusion constraint, while `appointment`
    does.
    """
    with Session(engine) as session:
        schedule_id = session.execute(
            text("""
                INSERT INTO analytics.schedule (
                    clinic_id, schedule_date, solver_status, is_baseline,
                    objective_value, total_wait_minutes, total_idle_minutes,
                    total_overtime_minutes, urgency_penalty, weights, solve_time_ms
                ) VALUES (
                    :clinic_id, :schedule_date, :solver_status, :is_baseline,
                    :objective_value, :wait, :idle, :overtime, :urgency,
                    CAST(:weights AS jsonb), :solve_time_ms
                ) RETURNING id
                """),
            {
                "clinic_id": request.clinic_id,
                "schedule_date": request.date,
                "solver_status": solution.solver_status,
                "is_baseline": is_baseline,
                "objective_value": score.objective,
                "wait": score.total_delay_minutes,
                "idle": score.total_idle_minutes,
                "overtime": score.total_overtime_minutes,
                "urgency": score.urgency_penalty,
                "weights": _json(weights.as_dict()),
                "solve_time_ms": solution.solve_time_ms,
            },
        ).scalar_one()

        by_id = {a.appointment_id: a for a in request.appointments}
        for assignment in solution.assignments:
            appointment = by_id.get(assignment.appointment_id)
            requested = (
                appointment.requested_start_minute if appointment else assignment.start_minute
            )
            session.execute(
                text("""
                    INSERT INTO analytics.schedule_entry (
                        schedule_id, appointment_id, doctor_id, room_id,
                        assigned_start, assigned_end, wait_minutes
                    ) VALUES (
                        :schedule_id, :appointment_id, :doctor_id, :room_id,
                        :assigned_start, :assigned_end, :wait_minutes
                    )
                    """),
                {
                    "schedule_id": schedule_id,
                    "appointment_id": assignment.appointment_id,
                    "doctor_id": assignment.doctor_id,
                    "room_id": assignment.room_id,
                    "assigned_start": to_time(assignment.start_minute),
                    "assigned_end": to_time(assignment.end_minute),
                    "wait_minutes": max(0, assignment.start_minute - requested),
                },
            )
        session.commit()
    return int(schedule_id)


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload)


def refuse_to_apply(solution: Solution) -> str | None:
    """Why an overbooked proposal cannot be written to `appointment`.

    Returns None when the schedule is applicable, or an explanation when it is
    not. The database would reject it anyway — the exclusion constraint makes
    two overlapping appointments unrepresentable — but surfacing an
    IntegrityError to a user is a poor way to communicate a deliberate clinic
    policy. The optimizer is permitted to RECOMMEND overbooking; enacting it is
    a policy change, and a policy change is not the scheduler's to make.
    """
    shared = {(a.doctor_id, a.start_minute) for a in solution.assignments if a.is_overbooked}
    if not shared:
        return None
    return (
        f"this schedule overbooks {len(shared)} slot(s). Appointments cannot be "
        "written with overlapping times: the database enforces one appointment "
        "per doctor per interval. The proposal is stored in analytics.schedule "
        "for review, but applying it requires a deliberate change to clinic "
        "overbooking policy."
    )
