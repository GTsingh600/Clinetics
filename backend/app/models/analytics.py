"""Derived and analytical tables, isolated in the `analytics` PostgreSQL schema.

Everything here is *produced* by the system rather than entered into it:
forecasts come from trained models, schedules from the CP-SAT solver, and
utilisation from a trigger. None of it is a source of truth — dropping and
regenerating the whole schema loses nothing that cannot be recomputed.

Keeping it in a separate PostgreSQL schema rather than merely a separate module
makes that boundary real:

* `public` is the transactional core; `analytics` is derived output. A reader
  of the database, not just of this repo, can see which is which.
* It can be granted separately — a reporting role can read `analytics` without
  ever touching patient rows.
* It can be truncated wholesale when retraining, with no risk of catching a
  transactional table in the blast radius.

FKs still cross the boundary into `public`, so referential integrity holds: a
forecast for a deleted specialty cannot linger.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.doctor import Doctor

ANALYTICS_SCHEMA = "analytics"


class Forecast(Base):
    """Predicted demand for one (clinic, specialty, date, hour) cell.

    Grain is hourly because the demand patterns worth capturing are
    intra-day: Monday-morning peaks and dermatology's evening peak both
    disappear if the model only predicts daily totals.

    `model_version` is part of the uniqueness key so two model versions can
    coexist over the same period. That is what makes an honest before/after
    comparison possible when a model is retrained.
    """

    __tablename__ = "forecast"
    __table_args__ = (
        UniqueConstraint(
            "clinic_id",
            "specialty_id",
            "forecast_date",
            "hour_of_day",
            "model_version",
            name="unique_forecast_cell",
        ),
        CheckConstraint("hour_of_day BETWEEN 0 AND 23", name="hour_in_range"),
        CheckConstraint("predicted_demand >= 0", name="demand_non_negative"),
        Index("ix_forecast_lookup", "clinic_id", "specialty_id", "forecast_date"),
        {"schema": ANALYTICS_SCHEMA},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinic.id", ondelete="CASCADE"), nullable=False
    )
    specialty_id: Mapped[int] = mapped_column(
        ForeignKey("specialty.id", ondelete="CASCADE"), nullable=False
    )
    forecast_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    hour_of_day: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    predicted_demand: Mapped[float] = mapped_column(Numeric(8, 3), nullable=False)
    # Prediction interval. Nullable because not every model produces one.
    lower_bound: Mapped[float | None] = mapped_column(Numeric(8, 3))
    upper_bound: Mapped[float | None] = mapped_column(Numeric(8, 3))

    model_version: Mapped[str] = mapped_column(String(60), nullable=False)
    generated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<Forecast {self.forecast_date} h{self.hour_of_day} "
            f"specialty={self.specialty_id} demand={self.predicted_demand}>"
        )


class Schedule(Base):
    """One optimizer run: its inputs, its objective breakdown, its outcome.

    Storing the individual objective terms rather than only the total is what
    makes the Phase 4 benchmark and the Phase 5 agent explanations possible.
    "Wait time fell 34% but overtime rose 8%" cannot be recovered from a single
    scalar objective value.

    `is_baseline` marks a greedy/FCFS run, so the optimized run and the baseline
    it is measured against are the same shape of record and can be diffed
    directly.
    """

    __tablename__ = "schedule"
    __table_args__ = (
        CheckConstraint("solve_time_ms >= 0", name="solve_time_non_negative"),
        CheckConstraint(
            "total_wait_minutes >= 0 AND total_idle_minutes >= 0 "
            "AND total_overtime_minutes >= 0",
            name="objective_terms_non_negative",
        ),
        Index("ix_schedule_clinic_date", "clinic_id", "schedule_date"),
        {"schema": ANALYTICS_SCHEMA},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinic.id", ondelete="CASCADE"), nullable=False
    )
    schedule_date: Mapped[dt.date] = mapped_column(Date, nullable=False)

    # CP-SAT status: OPTIMAL / FEASIBLE / INFEASIBLE / MODEL_INVALID / UNKNOWN.
    # Recorded verbatim because "the solver returned FEASIBLE, not OPTIMAL,
    # after hitting its time limit" is a materially different claim from
    # "this is the optimal schedule", and the agent must not blur the two.
    solver_status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_baseline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    objective_value: Mapped[float | None] = mapped_column(Numeric(12, 3))
    total_wait_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_idle_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_overtime_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    urgency_penalty: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False, default=0)

    # The exact objective weights used, so an old run stays interpretable after
    # the defaults change. JSONB rather than columns because the weight set is
    # expected to grow, and this is a record of inputs, not a queried dimension.
    weights: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    solve_time_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    entries: Mapped[list[ScheduleEntry]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        kind = "baseline" if self.is_baseline else "optimized"
        return f"<Schedule id={self.id} {self.schedule_date} {kind} {self.solver_status}>"


class ScheduleEntry(Base):
    """One appointment's placement within a Schedule.

    Deliberately does not overwrite `appointment.start_time`. A generated
    schedule is a *proposal*; keeping it separate is what allows the Phase 6
    what-if UI to diff proposed against actual, and allows a run to be discarded
    without touching the transactional record.
    """

    __tablename__ = "schedule_entry"
    __table_args__ = (
        UniqueConstraint("schedule_id", "appointment_id", name="unique_appointment_per_schedule"),
        CheckConstraint("assigned_end > assigned_start", name="end_after_start"),
        CheckConstraint("wait_minutes >= 0", name="wait_non_negative"),
        {"schema": ANALYTICS_SCHEMA},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(
        ForeignKey(f"{ANALYTICS_SCHEMA}.schedule.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # CASCADE: an entry describes a specific appointment and is meaningless
    # without it.
    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointment.id", ondelete="CASCADE"), nullable=False
    )
    doctor_id: Mapped[int] = mapped_column(
        ForeignKey("doctor.id", ondelete="CASCADE"), nullable=False
    )
    room_id: Mapped[int | None] = mapped_column(ForeignKey("room.id", ondelete="SET NULL"))

    assigned_start: Mapped[dt.time] = mapped_column(Time, nullable=False)
    assigned_end: Mapped[dt.time] = mapped_column(Time, nullable=False)
    # Minutes between the patient's requested/arrival time and their assigned
    # start: the per-appointment contribution to objective term w1.
    wait_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    schedule: Mapped[Schedule] = relationship(back_populates="entries")

    def __repr__(self) -> str:
        return (
            f"<ScheduleEntry schedule={self.schedule_id} appt={self.appointment_id} "
            f"{self.assigned_start}-{self.assigned_end} wait={self.wait_minutes}m>"
        )


class DoctorUtilization(Base, TimestampMixin):
    """Per-doctor, per-day summary maintained by a PL/pgSQL trigger.

    This table is never written by application code. The trigger defined in
    migration 0007 keeps it in step with `appointment` on every INSERT, UPDATE,
    and DELETE — including the awkward case of an UPDATE that moves an
    appointment to a different doctor or a different date, which must decrement
    one summary row and increment another.

    Why a trigger rather than a scheduled recomputation or an application-layer
    update: correctness under *any* writer. Bulk loads, migrations, manual
    psql fixes, and the data generator all bypass the ORM, and all of them keep
    this table correct because the guarantee lives in the database.

    Note what is stored: raw counts and booked minutes only. Utilisation as a
    *percentage* needs each doctor's available minutes from `availability`, so
    it is computed at read time. Caching a percentage here would go stale
    whenever a doctor's working hours changed, without any appointment row
    changing to trigger a recount.
    """

    __tablename__ = "doctor_utilization"
    __table_args__ = (
        CheckConstraint(
            "scheduled_count >= 0 AND completed_count >= 0 "
            "AND cancelled_count >= 0 AND no_show_count >= 0",
            name="counts_non_negative",
        ),
        CheckConstraint("booked_minutes >= 0", name="booked_minutes_non_negative"),
        {"schema": ANALYTICS_SCHEMA},
    )

    # Natural composite primary key: the grain *is* (doctor, date), and a
    # surrogate key would allow duplicate rows for the same cell.
    doctor_id: Mapped[int] = mapped_column(
        ForeignKey("doctor.id", ondelete="CASCADE"), primary_key=True
    )
    utilization_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)

    scheduled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancelled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    no_show_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Sum of duration_minutes over slot-occupying appointments (i.e. excluding
    # cancelled), matching the exclusion constraint's definition of "occupied".
    booked_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    doctor: Mapped[Doctor] = relationship()

    def __repr__(self) -> str:
        return (
            f"<DoctorUtilization doctor_id={self.doctor_id} {self.utilization_date} "
            f"booked={self.booked_minutes}m>"
        )
