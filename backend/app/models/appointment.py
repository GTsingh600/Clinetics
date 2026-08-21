"""Appointment — the transactional centre of the schema.

Three design points carry most of the weight here.

**1. Time is stored as local date + wall-clock times, not as an instant.**

    appointment_date DATE, start_time TIME, end_time TIME

A clinic's 09:00 slot is 09:00 to the staff and the patient on both sides of a
daylight-saving change; storing an absolute `timestamptz` would silently shift
it by an hour. It also gives `appointment_date` as a real column, which the
required `(doctor_id, appointment_date)` index needs, and it lets the
no-overlap constraint build a range with `appointment_date + start_time`, an
immutable expression (`date + time -> timestamp`) and therefore legal inside an
exclusion constraint.

The limitation this accepts, stated plainly: the model assumes one clinic-local
timezone per clinic and cannot represent an appointment spanning midnight.
Neither is a real constraint for outpatient scheduling.

**2. `specialty_id` is this schema's single deliberate denormalization.**

See the column comment below.

**3. Overlap prevention lives in the database, not the application.**

The `EXCLUDE USING gist` constraint added in migration 0005 makes a
double-booked doctor *unrepresentable*. Application-level checking cannot do
this: between a `SELECT` that finds the slot free and the `INSERT` that takes
it, another transaction can insert the same slot. The database constraint has
no such window. Phase 2's concurrency test demonstrates exactly that race.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin
from app.models.enums import (
    AppointmentStatus,
    Urgency,
    appointment_status_enum,
    urgency_enum,
)

if TYPE_CHECKING:
    from app.models.clinic import Clinic
    from app.models.doctor import Doctor
    from app.models.patient import Patient
    from app.models.room import Room
    from app.models.specialty import Specialty

# Outer bounds any appointment must fall inside, enforced as a CHECK.
#
# Why not the clinic's own opens_at/closes_at? A PostgreSQL CHECK constraint may
# only reference columns of its own row -- it cannot join to `clinic`. Enforcing
# the exact per-clinic window therefore requires either a trigger or the service
# layer, and the project budget is one trigger, spent on doctor_utilization.
#
# So the split is: the database guarantees no appointment can ever exist outside
# these absolute bounds (catching corrupt data from any source, including bulk
# loads that bypass the app), and the service layer plus the CP-SAT model
# enforce each clinic's actual hours.
EARLIEST_APPOINTMENT_TIME = "06:00:00"
LATEST_APPOINTMENT_TIME = "22:00:00"


class Appointment(Base, TimestampMixin):
    __tablename__ = "appointment"
    __table_args__ = (
        CheckConstraint("end_time > start_time", name="end_after_start"),
        CheckConstraint("duration_minutes > 0", name="duration_positive"),
        CheckConstraint(
            f"start_time >= TIME '{EARLIEST_APPOINTMENT_TIME}' "
            f"AND end_time <= TIME '{LATEST_APPOINTMENT_TIME}'",
            name="within_clinic_day_bounds",
        ),
        # An appointment cannot be booked after it starts.
        CheckConstraint(
            "booked_at <= (appointment_date + start_time) AT TIME ZONE 'UTC'",
            name="booked_before_start",
        ),
        # The required composite index. Order matters: doctor_id is the equality
        # predicate and appointment_date the range predicate, and a btree can
        # only range-scan on the trailing column. (doctor_id, appointment_date)
        # serves "this doctor's next two weeks"; the reverse would not.
        Index("ix_appointment_doctor_id_appointment_date", "doctor_id", "appointment_date"),
        # Supports clinic-wide day views and the forecasting aggregates, which
        # slice by date and specialty without naming a doctor.
        Index("ix_appointment_date_specialty", "appointment_date", "specialty_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- Relationships -----------------------------------------------------
    # ON DELETE is explicit and intentional on every FK:
    #   clinic/doctor/patient -> RESTRICT: never silently destroy clinical
    #       history. Deleting an entity with appointments must be a conscious
    #       act that deals with them first.
    #   room -> SET NULL: a decommissioned room must not delete the
    #       appointments that happened in it; they simply lose the location.
    #   specialty -> RESTRICT: the snapshot below must stay resolvable.
    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinic.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    doctor_id: Mapped[int] = mapped_column(
        ForeignKey("doctor.id", ondelete="RESTRICT"), nullable=False
    )
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patient.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    room_id: Mapped[int | None] = mapped_column(
        ForeignKey("room.id", ondelete="SET NULL"), index=True
    )

    # --- The one deliberate denormalization --------------------------------
    # DENORMALIZATION (deliberate, and the only one in this schema).
    #
    # This is derivable at booking time by joining
    #   appointment -> doctor -> doctor_specialty -> specialty
    # so storing it duplicates information. Two reasons it is worth it:
    #
    # 1. READ PERFORMANCE. Slicing appointments by specialty is the hottest
    #    read path in the system -- demand forecasting groups by
    #    (specialty, date, hour), and every operational dashboard filters by
    #    it. Without this column each of those queries needs a two-hop join
    #    through a many-to-many junction, on the largest table in the schema.
    #    With it they are a single index scan on
    #    ix_appointment_date_specialty.
    #
    # 2. HISTORICAL ACCURACY. It records the specialty *as of booking time*.
    #    A doctor who later adds or drops a specialty would otherwise rewrite
    #    the meaning of every past appointment, and the forecasting models
    #    would train on a history that never happened.
    #
    # The cost is the usual one: it can drift from the doctor's current
    # specialties. That is not a bug here -- divergence is the intended
    # semantics, since this column is a snapshot rather than a mirror.
    specialty_id: Mapped[int] = mapped_column(
        ForeignKey("specialty.id", ondelete="RESTRICT"), nullable=False
    )

    # --- Timing ------------------------------------------------------------
    appointment_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    start_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    end_time: Mapped[dt.time] = mapped_column(Time, nullable=False)

    # Generated and stored by the database, so it can never disagree with
    # start/end the way a hand-maintained column eventually would. The required
    # `duration_minutes > 0` CHECK still applies to it.
    duration_minutes: Mapped[int] = mapped_column(
        Integer,
        Computed("(EXTRACT(EPOCH FROM (end_time - start_time)) / 60)::int", persisted=True),
        nullable=False,
    )

    # When the booking was made. `appointment_date - booked_at` is the lead
    # time, the strongest single predictor of a no-show in the generator's model
    # and the first feature Phase 3 engineers.
    booked_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # --- State -------------------------------------------------------------
    status: Mapped[AppointmentStatus] = mapped_column(
        appointment_status_enum, nullable=False, default=AppointmentStatus.SCHEDULED
    )
    urgency: Mapped[Urgency] = mapped_column(urgency_enum, nullable=False, default=Urgency.ROUTINE)
    # Drives the duration model: first visits run longer than follow-ups.
    is_new_patient: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(String(500))

    # --- ORM relationships -------------------------------------------------
    clinic: Mapped[Clinic] = relationship(back_populates="appointments")
    doctor: Mapped[Doctor] = relationship(back_populates="appointments")
    patient: Mapped[Patient] = relationship(back_populates="appointments")
    room: Mapped[Room | None] = relationship()
    specialty: Mapped[Specialty] = relationship()

    @property
    def lead_time_days(self) -> int:
        """Days between booking and appointment. Never negative (CHECK-enforced)."""
        return (self.appointment_date - self.booked_at.date()).days

    @property
    def occupies_slot(self) -> bool:
        """Whether this row blocks its time on the doctor's calendar.

        Mirrors the partial WHERE clause on the exclusion constraint.
        """
        return self.status is not AppointmentStatus.CANCELLED

    def __repr__(self) -> str:
        return (
            f"<Appointment id={self.id} doctor_id={self.doctor_id} "
            f"{self.appointment_date} {self.start_time}-{self.end_time} "
            f"status={self.status.value}>"
        )
