"""Value types for the optimizer.

Plain dataclasses, no pandas and no ORM. `optimizer/` is a pure package — CI
fails the build if it imports SQLAlchemy, FastAPI, or `app.*` — so everything
crossing the boundary is defined here and the service layer does the conversion.

**Time is minutes-from-midnight, as integers.** CP-SAT works over integers, and
carrying `datetime.time` into the model would mean converting at every
constraint. Converting once at the boundary keeps the model readable and makes
every arithmetic constraint obviously correct.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

# The scheduling grid. 15 minutes matches `Availability`, the Phase 2 slot API,
# and the generator, so the optimizer cannot propose a start time the booking
# path would refuse.
GRANULARITY_MINUTES = 15

# Relative cost of delaying a patient, by urgency. Applied as a multiplier on
# that patient's delay, so an emergency pushed 15 minutes costs the same as a
# routine patient pushed two hours.
URGENCY_WEIGHT: dict[str, float] = {"routine": 1.0, "urgent": 3.0, "emergency": 8.0}


def to_minutes(value: dt.time) -> int:
    return value.hour * 60 + value.minute


def to_time(minutes: int) -> dt.time:
    return dt.time(hour=(minutes // 60) % 24, minute=minutes % 60)


@dataclass(frozen=True)
class AppointmentRequest:
    """One appointment the scheduler must place.

    `doctor_id` is fixed: the optimizer re-times appointments within the doctor
    they were booked with, and does not reassign. That is a deliberate scope
    choice — patients frequently want a specific clinician — and it means the
    specialty-match rule becomes an invariant to VALIDATE rather than a decision
    to search over. It is still checked, because bad data can violate it.
    """

    appointment_id: int
    patient_id: int
    doctor_id: int
    specialty_id: int
    # The slot the patient originally asked for. Delay is measured against this.
    requested_start_minute: int
    duration_minutes: int
    urgency: str = "routine"
    # From the Phase 3 classifier. Calibrated, which is what makes it legitimate
    # to multiply by minutes and sum.
    no_show_probability: float = 0.0
    room_id: int | None = None

    @property
    def urgency_weight(self) -> float:
        return URGENCY_WEIGHT.get(self.urgency, 1.0)

    @property
    def attendance_probability(self) -> float:
        return 1.0 - self.no_show_probability


@dataclass(frozen=True)
class DoctorDay:
    """A doctor's working windows for one day.

    `windows` are disjoint (start, end) pairs. Mandatory breaks need no separate
    constraint: a lunch break is simply the gap between two windows, exactly as
    `Availability` stores it. Requiring each appointment to fit inside a single
    window enforces breaks for free.
    """

    doctor_id: int
    windows: tuple[tuple[int, int], ...]
    specialty_ids: frozenset[int] = field(default_factory=frozenset)

    @property
    def available_minutes(self) -> int:
        return sum(end - start for start, end in self.windows)

    @property
    def earliest(self) -> int:
        return min(start for start, _ in self.windows) if self.windows else 0

    @property
    def latest(self) -> int:
        return max(end for _, end in self.windows) if self.windows else 0


@dataclass(frozen=True)
class RoomCapacity:
    room_id: int
    capacity: int


@dataclass(frozen=True)
class ScheduleRequest:
    """Everything needed to schedule one clinic-day.

    One day per solve. It matches the grain of `analytics.schedule`, keeps the
    model small enough to prove optimal in seconds, and reflects how a clinic
    actually replans — nobody re-optimises next March because today ran late.
    """

    clinic_id: int
    date: dt.date
    open_minute: int
    close_minute: int
    appointments: tuple[AppointmentRequest, ...]
    doctors: tuple[DoctorDay, ...]
    rooms: tuple[RoomCapacity, ...] = ()
    granularity_minutes: int = GRANULARITY_MINUTES
    # Overbooking is OFF by default. See `objective.OverbookingPolicy` and
    # docs/phase-4-optimizer.md for why it is a proposal-layer capability only.
    allow_overbooking: bool = False
    max_overbooked_slots: int = 0

    def doctor(self, doctor_id: int) -> DoctorDay | None:
        return next((d for d in self.doctors if d.doctor_id == doctor_id), None)

    @property
    def horizon(self) -> int:
        """Latest minute the model may use.

        Extends past clinic close so overtime is *representable* — a model that
        cannot express running late has no way to trade it off, and would report
        infeasible instead of "this day needs 40 minutes of overtime".
        """
        latest_doctor = max((d.latest for d in self.doctors), default=self.close_minute)
        return max(self.close_minute, latest_doctor) + 180


@dataclass(frozen=True)
class Assignment:
    """Where one appointment ended up."""

    appointment_id: int
    doctor_id: int
    start_minute: int
    end_minute: int
    room_id: int | None = None
    # Appointment ids sharing this start slot with the same doctor. Empty unless
    # overbooking was enabled AND the solver judged it worthwhile.
    overbooked_with: tuple[int, ...] = ()

    @property
    def is_overbooked(self) -> bool:
        return bool(self.overbooked_with)


@dataclass
class Solution:
    """A schedule, plus how it was produced."""

    assignments: tuple[Assignment, ...]
    solver_status: str
    solve_time_ms: int
    objective_value: float | None = None
    scheduler: str = "cpsat"
    # Appointments that could not be placed at all. Reported rather than
    # silently dropped: "we scheduled 34 of 36" is a materially different result
    # from "we scheduled the day", and a solver that hides the difference is
    # worse than one that fails loudly.
    unscheduled: tuple[int, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def by_doctor(self) -> dict[int, list[Assignment]]:
        grouped: dict[int, list[Assignment]] = {}
        for assignment in self.assignments:
            grouped.setdefault(assignment.doctor_id, []).append(assignment)
        for items in grouped.values():
            items.sort(key=lambda a: a.start_minute)
        return grouped

    @property
    def is_usable(self) -> bool:
        """Whether the result is worth applying.

        FEASIBLE counts: hitting the time limit with a valid schedule is a
        perfectly good outcome, and treating it as failure would throw away work
        the solver already did. INFEASIBLE and MODEL_INVALID do not.
        """
        return self.solver_status in {"OPTIMAL", "FEASIBLE"} and bool(self.assignments)
