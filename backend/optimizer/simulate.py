"""Waiting-room simulation: how long patients actually sit there.

The CP-SAT objective minimises *delay* — the gap between the slot a patient
asked for and the slot they were given. That is deterministic and directly
optimisable. It is not the same thing as **waiting-room time**, which is what a
patient experiences: you arrive at 10:00, the doctor is still with the 09:45
patient who ran long, and you wait.

Waiting-room time arises from variance, so it cannot be read off a schedule. It
has to be simulated: sample actual durations and attendance, replay each
doctor's session, and measure.

--------------------------------------------------------------------------
WHY THIS IS A SEPARATE MODULE, AND CALLABLE ON ITS OWN
--------------------------------------------------------------------------
Two reasons.

**Evidence.** The optimizer is not trained on waiting-room time. If an optimised
schedule also reduces it, that is out-of-sample evidence the optimisation is
real rather than an artefact of the objective it was scored on. If it does not,
that is worth knowing and reporting.

**It answers questions.** "How long will I actually wait?" and "what happens to
my wait if I move to 14:00?" are questions a patient or an agent asks about one
person, not a benchmark. So the entry points take a schedule and return
per-appointment waits, and `what_if_moved` re-simulates a single change. Phase 5
and 6 call these directly.

--------------------------------------------------------------------------
THE SESSION MODEL
--------------------------------------------------------------------------
Per doctor, appointments in scheduled order:

    patient k arrives at their scheduled time t_k
    the doctor is free at f_(k-1)
    consultation starts at max(t_k, f_(k-1))
    wait_k = max(0, f_(k-1) - t_k)
    f_k    = start_k + actual_duration_k

A no-show consumes no time and waits zero, which is why attendance is sampled
rather than assumed — treating every patient as attending would systematically
overstate waiting-room time by about the no-show rate.

Actual duration is drawn lognormally around the prediction. Consultations are
right-skewed: most run near the expected length and a few run long, and it is
those few that create the queue. Symmetric noise would understate waiting
precisely where it matters.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from optimizer.types import Assignment, ScheduleRequest, Solution

# Spread of actual duration around the prediction, on the log scale. 0.25 gives
# roughly a 20% coefficient of variation: most consultations land within a few
# minutes of the estimate and a small tail runs substantially over, which is the
# shape the Phase 1 generator produced and the shape clinics report.
DURATION_SIGMA = 0.25
DEFAULT_RUNS = 200


@dataclass
class SimulationResult:
    """Waiting-room outcomes, averaged over runs."""

    mean_wait_minutes: float = 0.0
    median_wait_minutes: float = 0.0
    p90_wait_minutes: float = 0.0
    max_wait_minutes: float = 0.0
    share_waiting_over_15: float = 0.0
    mean_overtime_minutes: float = 0.0
    mean_doctor_idle_minutes: float = 0.0
    runs: int = 0
    # Per appointment, so a single patient can be answered about.
    per_appointment: dict[int, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mean_wait_minutes": round(self.mean_wait_minutes, 2),
            "median_wait_minutes": round(self.median_wait_minutes, 2),
            "p90_wait_minutes": round(self.p90_wait_minutes, 2),
            "max_wait_minutes": round(self.max_wait_minutes, 2),
            "share_waiting_over_15": round(self.share_waiting_over_15, 4),
            "mean_overtime_minutes": round(self.mean_overtime_minutes, 2),
            "mean_doctor_idle_minutes": round(self.mean_doctor_idle_minutes, 2),
            "runs": self.runs,
        }


def _sample_duration(rng: random.Random, predicted: int) -> float:
    """Actual consultation length, right-skewed around the prediction."""
    if predicted <= 0:
        return 0.0
    # Median of the lognormal is exp(mu), so mu = log(predicted) puts the
    # MEDIAN at the prediction. Using the mean instead would bias every
    # simulated session long.
    return max(1.0, rng.lognormvariate(math.log(predicted), DURATION_SIGMA))


def _run_once(
    assignments: list[Assignment],
    lookup: dict[int, Any],
    rng: random.Random,
    close_minute: int,
) -> tuple[dict[int, float], float, float]:
    """Replay one doctor session. Returns (waits, overtime, idle)."""
    ordered = sorted(assignments, key=lambda a: a.start_minute)
    waits: dict[int, float] = {}
    free_at: float | None = None
    busy = 0.0
    first_start: float | None = None
    last_end: float | None = None

    for assignment in ordered:
        appointment = lookup.get(assignment.appointment_id)
        if appointment is None:
            continue

        attends = rng.random() >= appointment.no_show_probability
        if not attends:
            # A no-show occupies nothing and waits nothing. Recording 0 rather
            # than skipping keeps the denominator honest.
            waits[assignment.appointment_id] = 0.0
            continue

        scheduled = float(assignment.start_minute)
        start = scheduled if free_at is None else max(scheduled, free_at)
        waits[assignment.appointment_id] = max(0.0, start - scheduled)

        duration = _sample_duration(rng, appointment.duration_minutes)
        free_at = start + duration
        busy += duration
        first_start = start if first_start is None else first_start
        last_end = free_at

    overtime = max(0.0, (last_end or 0.0) - close_minute) if last_end else 0.0
    span = (last_end - first_start) if (last_end and first_start) else 0.0
    idle = max(0.0, span - busy)
    return waits, overtime, idle


def simulate(
    solution: Solution,
    request: ScheduleRequest,
    *,
    runs: int = DEFAULT_RUNS,
    seed: int = 42,
) -> SimulationResult:
    """Monte-Carlo the waiting room for a whole day.

    Seeded, so a reported figure is reproducible. The same seed is reused across
    schedules being compared, which means both see the *same* sampled durations
    and attendance — a paired comparison, so a difference reflects the schedule
    rather than luck of the draw.
    """
    lookup = {a.appointment_id: a for a in request.appointments}
    grouped = solution.by_doctor()

    all_waits: list[float] = []
    per_appointment_totals: dict[int, float] = {}
    overtime_total = 0.0
    idle_total = 0.0

    for run in range(runs):
        rng = random.Random(seed + run)
        for assignments in grouped.values():
            waits, overtime, idle = _run_once(assignments, lookup, rng, request.close_minute)
            overtime_total += overtime
            idle_total += idle
            for appointment_id, wait in waits.items():
                all_waits.append(wait)
                per_appointment_totals[appointment_id] = (
                    per_appointment_totals.get(appointment_id, 0.0) + wait
                )

    if not all_waits:
        return SimulationResult(runs=runs)

    ordered = sorted(all_waits)
    return SimulationResult(
        mean_wait_minutes=sum(ordered) / len(ordered),
        median_wait_minutes=ordered[len(ordered) // 2],
        p90_wait_minutes=ordered[int(len(ordered) * 0.9)],
        max_wait_minutes=ordered[-1],
        share_waiting_over_15=sum(1 for w in ordered if w > 15) / len(ordered),
        mean_overtime_minutes=overtime_total / runs,
        mean_doctor_idle_minutes=idle_total / runs,
        runs=runs,
        per_appointment={k: v / runs for k, v in per_appointment_totals.items()},
    )


def expected_wait_for(
    solution: Solution,
    request: ScheduleRequest,
    appointment_id: int,
    *,
    runs: int = DEFAULT_RUNS,
    seed: int = 42,
) -> float:
    """Expected waiting-room minutes for ONE appointment.

    The single-patient question: "how long will I actually be waiting?"
    """
    result = simulate(solution, request, runs=runs, seed=seed)
    return round(result.per_appointment.get(appointment_id, 0.0), 2)


def what_if_moved(
    solution: Solution,
    request: ScheduleRequest,
    appointment_id: int,
    new_start_minute: int,
    *,
    runs: int = DEFAULT_RUNS,
    seed: int = 42,
) -> dict[str, Any]:
    """Answer "what happens to my wait if I move to a different time?"

    Re-simulates the whole day with that one appointment moved, because moving a
    patient changes the queue for everyone after them. Reporting only the mover's
    wait would answer the question asked and hide the one that matters — whether
    the improvement was taken from someone else.

    Deliberately does NOT check whether the new time is bookable. That is the
    booking service's job, and duplicating the rule here is how the two drift
    apart. This answers only "what would the wait be".
    """
    before = simulate(solution, request, runs=runs, seed=seed)

    moved = []
    for assignment in solution.assignments:
        if assignment.appointment_id == appointment_id:
            duration = assignment.end_minute - assignment.start_minute
            moved.append(
                Assignment(
                    appointment_id=assignment.appointment_id,
                    doctor_id=assignment.doctor_id,
                    start_minute=new_start_minute,
                    end_minute=new_start_minute + duration,
                    room_id=assignment.room_id,
                )
            )
        else:
            moved.append(assignment)

    candidate = Solution(
        assignments=tuple(moved),
        solver_status=solution.solver_status,
        solve_time_ms=0,
        scheduler=f"{solution.scheduler}+moved",
    )
    after = simulate(candidate, request, runs=runs, seed=seed)

    return {
        "appointment_id": appointment_id,
        "new_start_minute": new_start_minute,
        "wait_before_minutes": round(before.per_appointment.get(appointment_id, 0.0), 2),
        "wait_after_minutes": round(after.per_appointment.get(appointment_id, 0.0), 2),
        # The honest part: what the move costs everyone else.
        "clinic_mean_wait_before": round(before.mean_wait_minutes, 2),
        "clinic_mean_wait_after": round(after.mean_wait_minutes, 2),
        "clinic_overtime_before": round(before.mean_overtime_minutes, 2),
        "clinic_overtime_after": round(after.mean_overtime_minutes, 2),
    }
