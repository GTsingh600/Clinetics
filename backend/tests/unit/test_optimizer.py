"""Optimizer invariants.

CLAUDE.md asks for a test per constraint and per optimizer invariant. These run
in milliseconds with no database, on instances small enough to check by hand.

The most important ones are the *negative* tests: that the solver refuses to
book through a lunch break, refuses to double-book, and refuses to exceed room
capacity. A scheduler that produces a fast, low-cost, illegal schedule is worse
than no scheduler at all, because the number looks good.
"""

from __future__ import annotations

import datetime as dt
import itertools

import pytest

from optimizer import greedy, model, simulate
from optimizer.objective import OverbookingPolicy, Weights
from optimizer.score import improvement, score_solution
from optimizer.types import (
    AppointmentRequest,
    DoctorDay,
    RoomCapacity,
    ScheduleRequest,
    to_minutes,
    to_time,
)

DATE = dt.date(2026, 6, 1)
OPEN = 8 * 60  # 08:00
CLOSE = 18 * 60  # 18:00
MORNING = (8 * 60, 12 * 60 + 30)
AFTERNOON = (13 * 60 + 30, 18 * 60)


def doctor(
    doctor_id: int = 1, windows: tuple[tuple[int, int], ...] = (MORNING, AFTERNOON)
) -> DoctorDay:
    return DoctorDay(doctor_id=doctor_id, windows=windows, specialty_ids=frozenset({1}))


def appointment(
    appointment_id: int,
    requested: int,
    *,
    duration: int = 30,
    doctor_id: int = 1,
    urgency: str = "routine",
    no_show: float = 0.0,
    room_id: int | None = None,
    specialty_id: int = 1,
) -> AppointmentRequest:
    return AppointmentRequest(
        appointment_id=appointment_id,
        patient_id=appointment_id * 10,
        doctor_id=doctor_id,
        specialty_id=specialty_id,
        requested_start_minute=requested,
        duration_minutes=duration,
        urgency=urgency,
        no_show_probability=no_show,
        room_id=room_id,
    )


def request(
    appointments: list[AppointmentRequest],
    doctors: list[DoctorDay] | None = None,
    rooms: list[RoomCapacity] | None = None,
    **kwargs: object,
) -> ScheduleRequest:
    return ScheduleRequest(
        clinic_id=1,
        date=DATE,
        open_minute=OPEN,
        close_minute=CLOSE,
        appointments=tuple(appointments),
        doctors=tuple(doctors or [doctor()]),
        rooms=tuple(rooms or []),
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------
def test_minute_conversion_round_trips() -> None:
    for value in (dt.time(8, 0), dt.time(12, 30), dt.time(17, 45)):
        assert to_time(to_minutes(value)) == value


# --------------------------------------------------------------------------
# Hard constraints — the negative tests
# --------------------------------------------------------------------------
def test_no_two_appointments_overlap_for_one_doctor() -> None:
    """The constraint Phase 1 enforces in the database, enforced in the model."""
    solution = model.solve(request([appointment(i, 9 * 60) for i in range(1, 7)]))

    assert solution.is_usable
    placed = sorted(solution.assignments, key=lambda a: a.start_minute)
    for earlier, later in itertools.pairwise(placed):
        assert (
            earlier.end_minute <= later.start_minute
        ), f"appointments {earlier.appointment_id} and {later.appointment_id} overlap"


def test_nothing_is_scheduled_through_the_lunch_break() -> None:
    """Breaks are enforced by the one-window constraint, not a separate rule.

    Seven appointments all requested at 12:00 cannot fit before 12:30, so the
    solver must push most of them past 13:30. If any lands inside the gap, the
    window modelling is wrong.

    Seven, not ten: with protective slack reserved per appointment, ten 30-minute
    consultations no longer fit in the 4.5-hour afternoon window, and the test
    would fail as INFEASIBLE for a capacity reason rather than a break one. That
    slack costs capacity is real and intended; this test is about the break.
    """
    solution = model.solve(request([appointment(i, 12 * 60) for i in range(1, 8)]))

    assert solution.is_usable
    for assignment in solution.assignments:
        inside_morning = (
            assignment.start_minute >= MORNING[0] and assignment.end_minute <= MORNING[1]
        )
        inside_afternoon = (
            assignment.start_minute >= AFTERNOON[0] and assignment.end_minute <= AFTERNOON[1]
        )
        assert inside_morning or inside_afternoon, (
            f"appointment {assignment.appointment_id} at {to_time(assignment.start_minute)} "
            f"spans the 12:30-13:30 break"
        )


def test_appointments_stay_inside_working_windows() -> None:
    early = DoctorDay(doctor_id=1, windows=((9 * 60, 11 * 60),), specialty_ids=frozenset({1}))
    solution = model.solve(request([appointment(i, 8 * 60) for i in range(1, 4)], doctors=[early]))

    assert solution.is_usable
    for assignment in solution.assignments:
        assert assignment.start_minute >= 9 * 60
        assert assignment.end_minute <= 11 * 60


def test_room_capacity_is_never_exceeded() -> None:
    """Cumulative, not NoOverlap: a capacity-2 room may hold two at once.

    Four appointments all wanting 09:00 in one capacity-2 room. At most two may
    overlap at any instant.
    """
    appointments = [appointment(i, 9 * 60, room_id=7) for i in range(1, 5)]
    solution = model.solve(request(appointments, rooms=[RoomCapacity(room_id=7, capacity=2)]))

    assert solution.is_usable
    for minute in range(OPEN, CLOSE):
        concurrent = sum(
            1
            for a in solution.assignments
            if a.room_id == 7 and a.start_minute <= minute < a.end_minute
        )
        assert concurrent <= 2, f"{concurrent} appointments share room 7 at {to_time(minute)}"


def test_a_room_may_hold_two_when_capacity_allows() -> None:
    """Guards against modelling capacity as NoOverlap, which would pass the
    test above for the wrong reason."""
    appointments = [appointment(i, 9 * 60, room_id=7, doctor_id=i) for i in (1, 2)]
    doctors = [doctor(1), doctor(2)]
    solution = model.solve(
        request(appointments, doctors=doctors, rooms=[RoomCapacity(room_id=7, capacity=2)])
    )

    assert solution.is_usable
    starts = {a.start_minute for a in solution.assignments}
    assert len(starts) == 1, "two doctors sharing a capacity-2 room should both get 09:00"


def test_specialty_mismatch_is_reported_not_silently_solved() -> None:
    """With doctors fixed this is a data problem, so it must be named as one."""
    mismatched = appointment(1, 9 * 60, specialty_id=99)
    with pytest.raises(model.InfeasibleScheduleError, match="does not hold specialty"):
        model.solve(request([mismatched]))


def test_appointment_too_long_for_any_window_is_reported() -> None:
    huge = appointment(1, 9 * 60, duration=8 * 60)
    with pytest.raises(model.InfeasibleScheduleError, match="long enough"):
        model.solve(request([huge]))


def test_starts_land_on_the_scheduling_grid() -> None:
    """A start time off the 15-minute grid is one the booking API cannot offer."""
    solution = model.solve(request([appointment(i, 9 * 60 + 7) for i in range(1, 5)]))

    assert solution.is_usable
    for assignment in solution.assignments:
        assert (
            assignment.start_minute % 15 == 0
        ), f"{to_time(assignment.start_minute)} is not on the 15-minute grid"


def test_empty_day_is_solved_not_rejected() -> None:
    solution = model.solve(request([]))
    assert solution.solver_status == "OPTIMAL"
    assert solution.assignments == ()


# --------------------------------------------------------------------------
# Objective behaviour
# --------------------------------------------------------------------------
def test_an_uncontended_appointment_gets_the_time_it_asked_for() -> None:
    solution = model.solve(request([appointment(1, 10 * 60)]))
    assert solution.assignments[0].start_minute == 10 * 60


def test_emergencies_are_delayed_less_than_routine_patients() -> None:
    """The urgency multiplier, doing the job it exists for.

    Six appointments all want 09:00 with one doctor, so five must be pushed
    back. The emergency should not be among the ones pushed furthest.
    """
    appointments = [appointment(i, 9 * 60) for i in range(1, 6)]
    appointments.append(appointment(6, 9 * 60, urgency="emergency"))
    solution = model.solve(request(appointments))

    assert solution.is_usable
    placed = {a.appointment_id: a.start_minute for a in solution.assignments}
    emergency_start = placed[6]
    routine_starts = [placed[i] for i in range(1, 6)]
    assert emergency_start <= min(
        routine_starts
    ), "an emergency should be seen before routine patients competing for the same slot"


def test_no_appointment_is_moved_earlier_than_requested() -> None:
    """A patient must never arrive to find their slot already gone.

    Found by this test, not by reasoning ahead of it. Delay is one-sided, so
    pulling someone forward is free, and the idle term rewards compressing a
    doctor's span -- so the solver scheduled a 14:00 request at 13:00. Moving
    someone earlier needs their consent, which the optimizer cannot obtain, so
    it is now a hard constraint.
    """
    appointments = [appointment(1, 14 * 60), appointment(2, 14 * 60)]
    solution = model.solve(request(appointments))
    scored = score_solution(solution, request(appointments))
    assert scored.total_delay_minutes >= 0
    assert all(a.start_minute >= 14 * 60 for a in solution.assignments)


def test_solver_beats_or_matches_greedy_on_the_objective() -> None:
    """The central claim, on a contended instance.

    CP-SAT minimises the objective, so on the same instance it cannot do worse
    than a heuristic. If it ever does, the model and the scorer disagree about
    what is being optimised.
    """
    appointments = [appointment(i, 9 * 60 + (i % 3) * 15, duration=30) for i in range(1, 13)]
    instance = request(appointments)

    optimised = score_solution(model.solve(instance), instance)
    baseline = score_solution(greedy.solve(instance), instance)

    assert optimised.objective <= baseline.objective + 1e-6


def test_greedy_respects_every_hard_constraint() -> None:
    """The baseline must be legal, or the comparison is meaningless."""
    appointments = [appointment(i, 12 * 60) for i in range(1, 9)]
    instance = request(appointments)
    solution = greedy.solve(instance)

    placed = sorted(solution.assignments, key=lambda a: a.start_minute)
    for earlier, later in itertools.pairwise(placed):
        assert earlier.end_minute <= later.start_minute
    for assignment in solution.assignments:
        in_morning = assignment.start_minute >= MORNING[0] and assignment.end_minute <= MORNING[1]
        in_afternoon = (
            assignment.start_minute >= AFTERNOON[0] and assignment.end_minute <= AFTERNOON[1]
        )
        assert in_morning or in_afternoon


def test_greedy_never_schedules_earlier_than_requested() -> None:
    instance = request([appointment(1, 15 * 60)])
    solution = greedy.solve(instance)
    assert solution.assignments[0].start_minute >= 15 * 60


# --------------------------------------------------------------------------
# Overbooking
# --------------------------------------------------------------------------
def test_overbooking_is_off_by_default() -> None:
    appointments = [appointment(i, 9 * 60, no_show=0.9) for i in range(1, 5)]
    solution = model.solve(request(appointments))
    assert not any(a.is_overbooked for a in solution.assignments)


def test_overbooking_can_be_enabled_and_is_reported() -> None:
    """When permitted, a shared slot must be visible in the result.

    Silent overbooking would be the worst outcome: a schedule that looks normal
    but cannot be applied to the appointment table.
    """
    appointments = [appointment(i, 9 * 60, no_show=0.9, duration=30) for i in range(1, 7)]
    instance = request(appointments, allow_overbooking=True, max_overbooked_slots=3)
    solution = model.solve(
        instance,
        overbooking=OverbookingPolicy(enabled=True, max_slots=3, max_per_slot=2),
    )

    assert solution.is_usable
    # At most two appointments may share any doctor-minute.
    for minute in range(OPEN, CLOSE):
        concurrent = sum(1 for a in solution.assignments if a.start_minute <= minute < a.end_minute)
        assert concurrent <= 2

    if any(a.is_overbooked for a in solution.assignments):
        scored = score_solution(solution, instance)
        assert scored.overbooked_slots >= 1, "a shared slot must be counted in the score"


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def test_idle_excludes_the_lunch_break() -> None:
    """Counting the break as idle would make every schedule look catastrophic
    and would reward the solver for booking through lunch."""
    appointments = [appointment(1, 12 * 60), appointment(2, 13 * 60 + 30)]
    instance = request(appointments)
    solution = model.solve(instance)
    scored = score_solution(solution, instance)

    assert (
        scored.total_idle_minutes < 60
    ), f"the 12:30-13:30 break is being counted as idle ({scored.total_idle_minutes} min)"


def test_expected_idle_prefers_risky_patients_late() -> None:
    """A likely no-show at the end costs less than the same one mid-session."""
    risky = appointment(1, 9 * 60, no_show=0.9)
    safe_a = appointment(2, 10 * 60, no_show=0.0)
    safe_b = appointment(3, 11 * 60, no_show=0.0)
    instance = request([risky, safe_a, safe_b])

    from optimizer.types import Assignment, Solution

    def build(order: list[int]) -> Solution:
        return Solution(
            assignments=tuple(
                Assignment(
                    appointment_id=appointment_id,
                    doctor_id=1,
                    start_minute=9 * 60 + index * 30,
                    end_minute=9 * 60 + index * 30 + 30,
                )
                for index, appointment_id in enumerate(order)
            ),
            solver_status="OPTIMAL",
            solve_time_ms=0,
        )

    risky_first = score_solution(build([1, 2, 3]), instance)
    risky_last = score_solution(build([2, 3, 1]), instance)
    assert risky_last.expected_idle_minutes < risky_first.expected_idle_minutes


def test_improvement_handles_a_zero_baseline() -> None:
    """A day the baseline already scheduled perfectly must not report infinity,
    or a single such day would destroy the benchmark mean."""
    from optimizer.score import ScheduleScore

    perfect = ScheduleScore(total_delay_minutes=0, objective=0.0)
    result = improvement(perfect, perfect)
    assert result["delay_pct"] == 0.0
    assert result["objective_pct"] == 0.0


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------
def test_simulation_is_reproducible() -> None:
    appointments = [appointment(i, 9 * 60 + i * 30) for i in range(1, 6)]
    instance = request(appointments)
    solution = model.solve(instance)

    first = simulate.simulate(solution, instance, runs=50, seed=1)
    second = simulate.simulate(solution, instance, runs=50, seed=1)
    assert first.mean_wait_minutes == second.mean_wait_minutes


def test_packing_a_session_tighter_increases_waiting_room_time() -> None:
    """The simulator must respond to congestion, or it measures nothing.

    Same appointments, same durations. Spaced generously nobody waits; packed
    below the expected duration, overruns cascade.
    """
    appointments = [appointment(i, 9 * 60, duration=30) for i in range(1, 7)]
    instance = request(appointments)

    from optimizer.types import Assignment, Solution

    def build(spacing: int) -> Solution:
        return Solution(
            assignments=tuple(
                Assignment(
                    appointment_id=i,
                    doctor_id=1,
                    start_minute=9 * 60 + (i - 1) * spacing,
                    end_minute=9 * 60 + (i - 1) * spacing + 30,
                )
                for i in range(1, 7)
            ),
            solver_status="OPTIMAL",
            solve_time_ms=0,
        )

    roomy = simulate.simulate(build(45), instance, runs=200, seed=3)
    packed = simulate.simulate(build(15), instance, runs=200, seed=3)
    assert packed.mean_wait_minutes > roomy.mean_wait_minutes


def test_no_shows_reduce_waiting_room_time() -> None:
    """Attendance is sampled, not assumed. Treating everyone as attending would
    overstate waiting by roughly the no-show rate."""
    attending = [appointment(i, 9 * 60 + (i - 1) * 15, duration=30) for i in range(1, 7)]
    missing = [appointment(i, 9 * 60 + (i - 1) * 15, duration=30, no_show=0.8) for i in range(1, 7)]

    from optimizer.types import Assignment, Solution

    schedule = Solution(
        assignments=tuple(
            Assignment(
                appointment_id=i,
                doctor_id=1,
                start_minute=9 * 60 + (i - 1) * 15,
                end_minute=9 * 60 + (i - 1) * 15 + 30,
            )
            for i in range(1, 7)
        ),
        solver_status="OPTIMAL",
        solve_time_ms=0,
    )

    busy = simulate.simulate(schedule, request(attending), runs=200, seed=5)
    sparse = simulate.simulate(schedule, request(missing), runs=200, seed=5)
    assert sparse.mean_wait_minutes < busy.mean_wait_minutes


def test_what_if_reports_the_effect_on_everyone_else() -> None:
    """Answering only the mover's question would hide who paid for it."""
    appointments = [appointment(i, 9 * 60 + (i - 1) * 15, duration=30) for i in range(1, 7)]
    instance = request(appointments)
    solution = model.solve(instance)

    result = simulate.what_if_moved(solution, instance, appointment_id=6, new_start_minute=16 * 60)
    assert "wait_before_minutes" in result
    assert "clinic_mean_wait_after" in result
    assert result["new_start_minute"] == 16 * 60


def test_weights_are_configurable() -> None:
    """Weights are policy, so they must be overridable per solve."""
    appointments = [appointment(i, 9 * 60) for i in range(1, 5)]
    instance = request(appointments)
    heavy = Weights(delay=1.0, idle=50.0, overtime=3.0)

    solution = model.solve(instance, weights=heavy)
    assert solution.is_usable
    assert solution.metadata["weights"]["idle"] == 50.0
