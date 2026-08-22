"""First-come-first-served baseline.

The thing the optimizer must beat. Same signature, same `Solution` type, scored
by the same `score.py` — so the benchmark compares two schedules, not two
implementations of a metric.

**This is deliberately a competent baseline, not a strawman.** It sorts by
requested time, respects every hard constraint the optimizer respects (working
windows, breaks, no double-booking, room capacity), and places each appointment
at the earliest legal slot at or after the time it was asked for. That is what a
booking system does today, and roughly what a receptionist does by hand.

Choosing a weak baseline is the easiest way to manufacture an impressive
improvement figure, and it is self-deception: the comparison IS the result. If
the optimizer only beats a scheduler that ignores lunch breaks, it has proved
nothing about scheduling.
"""

from __future__ import annotations

import time
from collections import defaultdict

from optimizer.objective import DEFAULT_SLACK, SlackPolicy
from optimizer.types import Assignment, ScheduleRequest, Solution


def _fits_in_window(start: int, duration: int, windows: tuple[tuple[int, int], ...]) -> bool:
    """An appointment must sit inside ONE window, never spanning the break."""
    return any(start >= w_start and start + duration <= w_end for w_start, w_end in windows)


def _room_free(
    start: int,
    duration: int,
    room_id: int | None,
    capacity: dict[int, int],
    placed: list[Assignment],
) -> bool:
    if room_id is None:
        return True
    limit = capacity.get(room_id, 1)
    concurrent = sum(
        1
        for a in placed
        if a.room_id == room_id and a.start_minute < start + duration and start < a.end_minute
    )
    return concurrent < limit


def solve(request: ScheduleRequest, *, slack: SlackPolicy = DEFAULT_SLACK) -> Solution:
    """Place appointments FCFS at the earliest legal slot from their request.

    Takes the same `SlackPolicy` as the optimizer, and must. Comparing an
    optimizer that reserves protective buffer against a baseline that packs
    back-to-back measures the POLICY, not the algorithm — and it flatters
    whichever one happens to suit the metric being reported. Both schedulers run
    the same policy so the difference is the scheduling.
    """
    started = time.perf_counter()
    grid = request.granularity_minutes
    capacity = {r.room_id: r.capacity for r in request.rooms}

    # Requested time, then urgency as the tie-break. A receptionist handed two
    # requests for the same slot takes the emergency first; not modelling that
    # would make the baseline worse than real practice.
    ordered = sorted(
        request.appointments,
        key=lambda a: (a.requested_start_minute, -a.urgency_weight, a.appointment_id),
    )

    placed_by_doctor: dict[int, list[Assignment]] = defaultdict(list)
    all_placed: list[Assignment] = []
    unscheduled: list[int] = []

    for appointment in ordered:
        doctor = request.doctor(appointment.doctor_id)
        if doctor is None or not doctor.windows:
            unscheduled.append(appointment.appointment_id)
            continue

        duration = appointment.duration_minutes
        reserved = duration + slack.buffer_for(duration)
        existing = placed_by_doctor[appointment.doctor_id]

        # Walk the grid forward from the requested time. Never earlier: pulling
        # an appointment forward is not something a booking system does to a
        # patient who asked for a later slot.
        earliest = max(appointment.requested_start_minute, doctor.earliest)
        candidate = earliest + (-earliest % grid)  # round up onto the grid

        chosen: int | None = None
        while candidate + duration <= doctor.latest:
            # Clash-checking uses the RESERVED span, matching the optimizer:
            # an appointment claims its duration plus its protective buffer.
            clashes = any(
                candidate < a.end_minute + slack.buffer_for(a.end_minute - a.start_minute)
                and a.start_minute < candidate + reserved
                for a in existing
            )
            if (
                not clashes
                and _fits_in_window(candidate, duration, doctor.windows)
                and _room_free(candidate, duration, appointment.room_id, capacity, all_placed)
            ):
                chosen = candidate
                break
            candidate += grid

        if chosen is None:
            unscheduled.append(appointment.appointment_id)
            continue

        assignment = Assignment(
            appointment_id=appointment.appointment_id,
            doctor_id=appointment.doctor_id,
            start_minute=chosen,
            end_minute=chosen + duration,
            room_id=appointment.room_id,
        )
        existing.append(assignment)
        all_placed.append(assignment)

    return Solution(
        assignments=tuple(sorted(all_placed, key=lambda a: (a.doctor_id, a.start_minute))),
        solver_status="OPTIMAL" if not unscheduled else "FEASIBLE",
        solve_time_ms=int((time.perf_counter() - started) * 1000),
        scheduler="greedy",
        unscheduled=tuple(unscheduled),
        metadata={"strategy": "first-come-first-served, earliest legal slot"},
    )
