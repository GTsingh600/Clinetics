"""The CP-SAT scheduling model.

Decides a start time for every appointment within its booked doctor's working
windows, minimising the weighted objective in `objective.py`.

--------------------------------------------------------------------------
THE MODEL
--------------------------------------------------------------------------
For each appointment i:
    start_i        integer, on the 15-minute grid
    interval_i     fixed-size interval [start_i, start_i + duration_i)
    window_i[w]    boolean, true when i sits inside doctor window w

Constraints:
    exactly one window per appointment          -> breaks respected for free
    NoOverlap over each doctor's intervals      -> no double-booking
    Cumulative over each room's intervals       -> capacity never exceeded
    urgency ordering (soft, see below)

--------------------------------------------------------------------------
WHY NoOverlap RATHER THAN PAIRWISE DISJUNCTIONS
--------------------------------------------------------------------------
Writing "for every pair, i before j or j before i" is O(n^2) reified booleans
and gives the solver almost nothing to propagate with. `AddNoOverlap` hands
CP-SAT a global constraint with a dedicated scheduling propagator: it reasons
about the whole set at once (edge-finding, energetic reasoning) and prunes
orders of magnitude more of the search. On a 67-appointment day the difference
is between milliseconds and a time limit.

--------------------------------------------------------------------------
BREAKS ARE NOT A CONSTRAINT
--------------------------------------------------------------------------
`Availability` stores a doctor's day as separate windows either side of lunch.
Requiring each appointment to fit inside ONE window means an appointment can
never span the gap. Mandatory breaks are therefore enforced by the same
constraint that enforces working hours, with no extra modelling — which is a
consequence of the Phase 1 schema decision, not a coincidence.

--------------------------------------------------------------------------
DETERMINISM
--------------------------------------------------------------------------
`num_search_workers=1` and a fixed seed. Multi-threaded CP-SAT is faster and
NON-DETERMINISTIC: workers race, and the returned solution depends on timing.
For a benchmark whose number is meant to be reproducible, a figure that changes
between runs is not a result. The cost is real (single-threaded solves are
slower) and it is the right trade here.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from ortools.sat.python import cp_model

from optimizer.objective import (
    DEFAULT_SLACK,
    DEFAULT_WEIGHTS,
    NO_OVERBOOKING,
    OverbookingPolicy,
    SlackPolicy,
    Weights,
)
from optimizer.types import Assignment, ScheduleRequest, Solution

log = logging.getLogger(__name__)

DEFAULT_TIME_LIMIT_SECONDS = 20.0
SOLVER_SEED = 42


class InfeasibleScheduleError(RuntimeError):
    """The day cannot be scheduled at all under the stated constraints."""


def _validate(request: ScheduleRequest) -> list[str]:
    """Data problems that would otherwise surface as a confusing INFEASIBLE.

    Specialty match is checked here rather than constrained in the model. With
    doctors fixed, it is not a decision the solver makes — so a mismatch is bad
    data, and reporting it plainly beats letting the solver report INFEASIBLE
    and leaving someone to work out why.
    """
    problems: list[str] = []
    for appointment in request.appointments:
        doctor = request.doctor(appointment.doctor_id)
        if doctor is None:
            problems.append(
                f"appointment {appointment.appointment_id}: doctor "
                f"{appointment.doctor_id} has no availability on this day"
            )
            continue
        if doctor.specialty_ids and appointment.specialty_id not in doctor.specialty_ids:
            problems.append(
                f"appointment {appointment.appointment_id}: doctor {doctor.doctor_id} "
                f"does not hold specialty {appointment.specialty_id}"
            )
        if not any(end - start >= appointment.duration_minutes for start, end in doctor.windows):
            problems.append(
                f"appointment {appointment.appointment_id}: no window of doctor "
                f"{doctor.doctor_id} is long enough for {appointment.duration_minutes} min"
            )
    return problems


def solve(
    request: ScheduleRequest,
    *,
    weights: Weights = DEFAULT_WEIGHTS,
    overbooking: OverbookingPolicy = NO_OVERBOOKING,
    slack: SlackPolicy = DEFAULT_SLACK,
    time_limit_seconds: float = DEFAULT_TIME_LIMIT_SECONDS,
    log_search: bool = False,
) -> Solution:
    """Schedule one clinic-day."""
    started = time.perf_counter()

    problems = _validate(request)
    if problems:
        raise InfeasibleScheduleError("; ".join(problems[:5]))
    if not request.appointments:
        return Solution(
            assignments=(),
            solver_status="OPTIMAL",
            solve_time_ms=0,
            objective_value=0.0,
            metadata={"note": "no appointments to schedule"},
        )

    model = cp_model.CpModel()
    grid = request.granularity_minutes
    horizon = request.horizon

    starts: dict[int, cp_model.IntVar] = {}
    ends: dict[int, cp_model.IntVar] = {}
    intervals: dict[int, cp_model.IntervalVar] = {}
    delays: dict[int, cp_model.IntVar] = {}
    overtimes: dict[int, cp_model.IntVar] = {}
    by_doctor: dict[int, list[cp_model.IntervalVar]] = defaultdict(list)

    for appointment in request.appointments:
        key = appointment.appointment_id
        doctor = request.doctor(appointment.doctor_id)
        assert doctor is not None  # _validate guarantees this

        # The variable's own lower bound must admit the requested time, or the
        # constraint below would make an otherwise-schedulable day infeasible.
        earliest = min(doctor.earliest, appointment.requested_start_minute)
        latest = doctor.latest
        start = model.new_int_var(earliest, max(earliest, latest), f"start_{key}")
        end = model.new_int_var(earliest, horizon, f"end_{key}")
        model.add(end == start + appointment.duration_minutes)

        # Snap to the grid. Without this the solver would place appointments at
        # arbitrary minutes and produce times the booking UI cannot offer.
        offset = model.new_int_var(0, horizon // grid, f"slot_{key}")
        model.add(start == earliest + offset * grid)

        # The interval RESERVES duration + slack, so the next patient cannot be
        # scheduled tight against a consultation that may run over. `end` stays
        # the real finish time, so reported times and overtime are honest and
        # the reserved margin surfaces as idle in the score -- which is exactly
        # what it is: protective time bought on purpose.
        reserved = appointment.duration_minutes + slack.buffer_for(appointment.duration_minutes)
        reserved_end = model.new_int_var(earliest, horizon, f"reserved_end_{key}")
        model.add(reserved_end == start + reserved)
        interval = model.new_interval_var(start, reserved, reserved_end, f"interval_{key}")
        starts[key], ends[key], intervals[key] = start, end, interval
        by_doctor[appointment.doctor_id].append(interval)

        # Exactly one working window. This is what enforces breaks.
        window_literals = []
        for index, (window_start, window_end) in enumerate(doctor.windows):
            inside = model.new_bool_var(f"in_window_{key}_{index}")
            model.add(start >= window_start).only_enforce_if(inside)
            model.add(end <= window_end).only_enforce_if(inside)
            window_literals.append(inside)
        if window_literals:
            model.add_exactly_one(window_literals)

        # An appointment may never be moved EARLIER than the patient asked for.
        #
        # This is a hard constraint, and it was found by a test rather than
        # reasoned out in advance. Delay is one-sided, so pulling someone
        # forward costs nothing, while the idle term rewards compressing a
        # doctor's span -- so the solver happily scheduled a 14:00 request at
        # 13:00. That is not an optimisation, it is a patient turning up to find
        # their slot already gone. Moving someone earlier requires their
        # consent, which the optimizer cannot obtain.
        model.add(start >= appointment.requested_start_minute)

        # Delay, one-sided: being seen early is not a benefit to be earned.
        delay = model.new_int_var(0, horizon, f"delay_{key}")
        model.add(delay >= start - appointment.requested_start_minute)
        delays[key] = delay

        overtime = model.new_int_var(0, horizon, f"overtime_{key}")
        model.add(overtime >= end - request.close_minute)
        overtimes[key] = overtime

    # --- No double-booking -------------------------------------------------
    for doctor_id, doctor_intervals in by_doctor.items():
        if overbooking.enabled:
            _add_capacity_with_overbooking(model, doctor_intervals, overbooking)
        else:
            model.add_no_overlap(doctor_intervals)
        log.debug("doctor %s: %d intervals", doctor_id, len(doctor_intervals))

    # --- Room capacity -----------------------------------------------------
    # Cumulative, not NoOverlap: a room with capacity 3 may hold three
    # concurrent appointments, which NoOverlap cannot express.
    for room in request.rooms:
        room_intervals = [
            intervals[a.appointment_id] for a in request.appointments if a.room_id == room.room_id
        ]
        if room_intervals:
            model.add_cumulative(room_intervals, [1] * len(room_intervals), room.capacity)

    # --- Urgency ordering (soft) -------------------------------------------
    # "Urgent patients scheduled before non-urgent WHERE FEASIBLE." Enforcing it
    # hard would make a day INFEASIBLE whenever an emergency is booked for the
    # afternoon, which is both common and not an error. Expressed instead as
    # cost, via the urgency multiplier on delay below.

    objective_terms = []
    for appointment in request.appointments:
        key = appointment.appointment_id
        weight = weights.delay + weights.urgency * (appointment.urgency_weight - 1.0)
        objective_terms.append(round(weight * 100) * delays[key])
        objective_terms.append(round(weights.overtime * 100) * overtimes[key])

    idle_terms = _idle_objective(model, request, starts, ends, weights)
    objective_terms.extend(idle_terms)

    model.minimize(sum(objective_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    # Single-threaded and seeded: see the module docstring. A benchmark figure
    # that changes between runs is not a result.
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = SOLVER_SEED
    solver.parameters.log_search_progress = log_search

    status = solver.solve(model)
    status_name = solver.status_name(status)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Solution(
            assignments=(),
            solver_status=status_name,
            solve_time_ms=elapsed_ms,
            unscheduled=tuple(a.appointment_id for a in request.appointments),
            metadata={"reason": "solver found no feasible schedule"},
        )

    assignments = []
    slot_members: dict[tuple[int, int], list[int]] = defaultdict(list)
    for appointment in request.appointments:
        key = appointment.appointment_id
        start_value = int(solver.value(starts[key]))
        slot_members[(appointment.doctor_id, start_value)].append(key)

    for appointment in request.appointments:
        key = appointment.appointment_id
        start_value = int(solver.value(starts[key]))
        shared = [
            other for other in slot_members[(appointment.doctor_id, start_value)] if other != key
        ]
        assignments.append(
            Assignment(
                appointment_id=key,
                doctor_id=appointment.doctor_id,
                start_minute=start_value,
                end_minute=int(solver.value(ends[key])),
                room_id=appointment.room_id,
                overbooked_with=tuple(sorted(shared)),
            )
        )

    return Solution(
        assignments=tuple(sorted(assignments, key=lambda a: (a.doctor_id, a.start_minute))),
        solver_status=status_name,
        solve_time_ms=elapsed_ms,
        objective_value=solver.objective_value / 100.0,
        scheduler="cpsat",
        metadata={
            "weights": weights.as_dict(),
            "slack_fraction": slack.fraction,
            "overbooking_enabled": overbooking.enabled,
            "branches": solver.num_branches,
            "conflicts": solver.num_conflicts,
            "wall_time_s": round(solver.wall_time, 3),
        },
    )


def _add_capacity_with_overbooking(
    model: cp_model.CpModel,
    intervals: list[cp_model.IntervalVar],
    policy: OverbookingPolicy,
) -> None:
    """Allow a bounded number of appointments to share a doctor at one time.

    `AddCumulative` with capacity `max_per_slot` replaces `AddNoOverlap`: the
    doctor becomes a resource of capacity 2 rather than 1. The number of shared
    slots is bounded elsewhere (the objective penalty and `max_slots`), because
    a cumulative constraint alone would happily double-book the entire day.
    """
    model.add_cumulative(intervals, [1] * len(intervals), policy.max_per_slot)


def _idle_objective(
    model: cp_model.CpModel,
    request: ScheduleRequest,
    starts: dict[int, cp_model.IntVar],
    ends: dict[int, cp_model.IntVar],
    weights: Weights,
) -> list[cp_model.LinearExpr]:
    """Penalise a doctor's working span beyond the work actually in it.

    Idle time is span minus busy time, and busy time is a constant for a given
    day, so minimising `last_end - first_start` per doctor minimises idle
    exactly. That is a far smaller model than reifying every pairwise gap, and
    it is a linear expression CP-SAT propagates well.

    It also captures the sequencing effect the expected-idle term is after:
    compressing a doctor's span naturally pushes appointments together, and the
    scorer measures the no-show contribution separately.
    """
    terms: list[cp_model.LinearExpr] = []
    horizon = request.horizon

    by_doctor: dict[int, list[int]] = defaultdict(list)
    for appointment in request.appointments:
        by_doctor[appointment.doctor_id].append(appointment.appointment_id)

    for doctor_id, keys in by_doctor.items():
        if len(keys) < 2:
            continue
        doctor = request.doctor(doctor_id)
        earliest = doctor.earliest if doctor else 0

        span_start = model.new_int_var(earliest, horizon, f"span_start_{doctor_id}")
        span_end = model.new_int_var(earliest, horizon, f"span_end_{doctor_id}")
        model.add_min_equality(span_start, [starts[k] for k in keys])
        model.add_max_equality(span_end, [ends[k] for k in keys])

        busy = sum(a.duration_minutes for a in request.appointments if a.doctor_id == doctor_id)
        idle = model.new_int_var(0, horizon, f"idle_{doctor_id}")
        model.add(idle >= span_end - span_start - busy)
        terms.append(round(weights.idle * 100) * idle)

    return terms
