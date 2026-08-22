"""Scoring a schedule.

**Both schedulers are scored by this module, and only by this module.** If the
CP-SAT model reported its own internal objective and the greedy baseline
computed its own metrics, any difference between them could be a difference in
the *metric* rather than in the schedule — and the benchmark headline would be
measuring the wrong thing while looking fine.

So the CP-SAT objective is what the solver optimises, and this is what both are
judged by. They are deliberately separate: the solver's objective is a linear
integer expression it can reason about, while this is plain arithmetic over the
finished schedule. When they disagree the schedule is what counts, and
`test_optimizer.py` asserts they agree on the terms where they should.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from optimizer.objective import DEFAULT_WEIGHTS, Weights
from optimizer.types import Assignment, ScheduleRequest, Solution


@dataclass
class ScheduleScore:
    """Every term, kept separate.

    Storing only the weighted total would make the benchmark unreadable: "the
    objective improved 22%" cannot distinguish "patients are seen sooner" from
    "we stopped running late", and those are different claims about the clinic.
    """

    total_delay_minutes: int = 0
    total_idle_minutes: int = 0
    total_overtime_minutes: int = 0
    urgency_penalty: float = 0.0
    expected_idle_minutes: float = 0.0
    overbooked_slots: int = 0
    scheduled_count: int = 0
    unscheduled_count: int = 0
    mean_delay_minutes: float = 0.0
    max_delay_minutes: int = 0
    objective: float = 0.0
    per_doctor: dict[int, dict[str, float]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_delay_minutes": self.total_delay_minutes,
            "total_idle_minutes": self.total_idle_minutes,
            "total_overtime_minutes": self.total_overtime_minutes,
            "urgency_penalty": round(self.urgency_penalty, 2),
            "expected_idle_minutes": round(self.expected_idle_minutes, 2),
            "overbooked_slots": self.overbooked_slots,
            "scheduled_count": self.scheduled_count,
            "unscheduled_count": self.unscheduled_count,
            "mean_delay_minutes": round(self.mean_delay_minutes, 2),
            "max_delay_minutes": self.max_delay_minutes,
            "objective": round(self.objective, 2),
        }


def _idle_minutes(assignments: list[Assignment], windows: tuple[tuple[int, int], ...]) -> int:
    """Working minutes the doctor spends doing nothing.

    Counted only INSIDE the doctor's availability windows. Time between windows
    is the lunch break — counting it as idle would make every schedule look
    catastrophic and would reward the solver for booking through lunch.

    Idle is also measured only between the first and last appointment in each
    window. A doctor whose window runs to 19:00 but whose last patient is at
    17:00 has finished early, which is not the same failure as a two-hour hole
    in the middle of a session, and pricing them identically would push the
    solver to spread appointments to the window edges.
    """
    total = 0
    for window_start, window_end in windows:
        inside = sorted(
            (
                a
                for a in assignments
                if a.start_minute >= window_start and a.end_minute <= window_end
            ),
            key=lambda a: a.start_minute,
        )
        if len(inside) < 2:
            continue
        for previous, current in itertools.pairwise(inside):
            gap = current.start_minute - previous.end_minute
            if gap > 0:
                total += gap
    return total


def _expected_idle(assignments: list[Assignment], request: ScheduleRequest) -> float:
    """Idle time expected from patients who probably will not attend.

    Only counted when work remains after them: a no-show at the end of a session
    lets the doctor finish early, while one in the middle leaves a gap nobody can
    fill. This is the term that makes the solver prefer to sequence high-risk
    patients late.
    """
    by_id = {a.appointment_id: a for a in request.appointments}
    ordered = sorted(assignments, key=lambda a: a.start_minute)
    total = 0.0
    for index, assignment in enumerate(ordered):
        appointment = by_id.get(assignment.appointment_id)
        if appointment is None:
            continue
        work_remains = index < len(ordered) - 1
        if work_remains:
            total += appointment.no_show_probability * appointment.duration_minutes
    return total


def score_solution(
    solution: Solution, request: ScheduleRequest, weights: Weights = DEFAULT_WEIGHTS
) -> ScheduleScore:
    """Grade a finished schedule against the request that produced it."""
    by_id = {a.appointment_id: a for a in request.appointments}
    score = ScheduleScore(
        scheduled_count=len(solution.assignments),
        unscheduled_count=len(solution.unscheduled),
    )

    delays: list[int] = []
    for assignment in solution.assignments:
        appointment = by_id.get(assignment.appointment_id)
        if appointment is None:
            continue
        # Delay is one-sided. Being seen EARLIER than requested is not a cost —
        # a signed difference would let the solver "earn" credit by pulling
        # appointments forward, which is not a benefit anyone asked for.
        delay = max(0, assignment.start_minute - appointment.requested_start_minute)
        delays.append(delay)
        score.total_delay_minutes += delay
        score.urgency_penalty += delay * (appointment.urgency_weight - 1.0)

        overtime = max(0, assignment.end_minute - request.close_minute)
        score.total_overtime_minutes += overtime

    grouped = solution.by_doctor()
    for doctor_id, assignments in grouped.items():
        doctor = request.doctor(doctor_id)
        windows = doctor.windows if doctor else ()
        idle = _idle_minutes(assignments, windows)
        score.total_idle_minutes += idle
        score.expected_idle_minutes += _expected_idle(assignments, request)
        score.per_doctor[doctor_id] = {
            "idle_minutes": idle,
            "appointments": len(assignments),
            "booked_minutes": sum(a.end_minute - a.start_minute for a in assignments),
        }

    # An overbooked slot is counted once, not once per participant.
    shared_slots: set[tuple[int, int]] = set()
    for assignment in solution.assignments:
        if assignment.is_overbooked:
            shared_slots.add((assignment.doctor_id, assignment.start_minute))
    score.overbooked_slots = len(shared_slots)

    score.mean_delay_minutes = sum(delays) / len(delays) if delays else 0.0
    score.max_delay_minutes = max(delays, default=0)

    score.objective = (
        weights.delay * score.total_delay_minutes
        + weights.idle * score.total_idle_minutes
        + weights.overtime * score.total_overtime_minutes
        + weights.urgency * score.urgency_penalty
        + weights.overbooking * score.overbooked_slots
    )
    return score


def improvement(baseline: ScheduleScore, candidate: ScheduleScore) -> dict[str, float]:
    """Percent reduction of `candidate` against `baseline`, per term.

    Returns 0.0 rather than infinity when the baseline term is already zero:
    "infinite improvement" over a day the baseline already scheduled perfectly is
    not a claim worth making, and a single such day would destroy any mean.
    """

    def pct(before: float, after: float) -> float:
        if before <= 0:
            return 0.0
        return round((before - after) / before * 100.0, 2)

    return {
        "delay_pct": pct(baseline.total_delay_minutes, candidate.total_delay_minutes),
        "idle_pct": pct(baseline.total_idle_minutes, candidate.total_idle_minutes),
        "overtime_pct": pct(baseline.total_overtime_minutes, candidate.total_overtime_minutes),
        "expected_idle_pct": pct(baseline.expected_idle_minutes, candidate.expected_idle_minutes),
        "objective_pct": pct(baseline.objective, candidate.objective),
        "mean_delay_pct": pct(baseline.mean_delay_minutes, candidate.mean_delay_minutes),
    }
