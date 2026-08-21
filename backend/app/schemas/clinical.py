"""Schemas for the clinical entities and for booking."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field, model_validator

from app.models.enums import AppointmentStatus, Urgency, Weekday
from app.schemas.common import ORMModel


class SpecialtyOut(ORMModel):
    id: int
    name: str
    slug: str
    default_duration_minutes: int


class RoomOut(ORMModel):
    id: int
    name: str
    capacity: int
    is_active: bool


class DoctorOut(ORMModel):
    id: int
    first_name: str
    last_name: str
    license_number: str
    is_active: bool
    specialties: list[SpecialtyOut] = Field(default_factory=list)


class DoctorSummary(ORMModel):
    """Lightweight doctor reference, for embedding in appointment payloads."""

    id: int
    first_name: str
    last_name: str


class PatientOut(ORMModel):
    id: int
    first_name: str
    last_name: str
    date_of_birth: dt.date
    email: str | None
    phone: str | None


class PatientSummary(ORMModel):
    id: int
    first_name: str
    last_name: str


class AvailabilityOut(ORMModel):
    id: int
    doctor_id: int
    weekday: Weekday
    start_time: dt.time
    end_time: dt.time
    effective_from: dt.date
    effective_to: dt.date | None
    is_active: bool


class AppointmentOut(ORMModel):
    id: int
    doctor_id: int
    patient_id: int
    specialty_id: int
    room_id: int | None
    appointment_date: dt.date
    start_time: dt.time
    end_time: dt.time
    duration_minutes: int
    status: AppointmentStatus
    urgency: Urgency
    is_new_patient: bool
    booked_at: dt.datetime
    doctor: DoctorSummary | None = None
    patient: PatientSummary | None = None
    specialty: SpecialtyOut | None = None


class BookingRequest(BaseModel):
    """A request to book a slot.

    `end_time` is derived server-side from the specialty's default duration when
    omitted, so a client cannot request a 5-minute cardiology consult.
    """

    doctor_id: int
    patient_id: int
    specialty_id: int
    appointment_date: dt.date
    start_time: dt.time
    duration_minutes: int | None = Field(
        default=None, ge=5, le=240, description="Defaults to the specialty's standard duration"
    )
    room_id: int | None = None
    urgency: Urgency = Urgency.ROUTINE
    notes: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _not_in_the_past(self) -> BookingRequest:
        """Cheap client-facing guard.

        The database's `booked_before_start` CHECK is the real guarantee; this
        exists to return a readable 422 instead of a constraint violation.
        """
        if self.appointment_date < dt.date.today():
            raise ValueError("cannot book an appointment in the past")
        return self


class RescheduleRequest(BaseModel):
    appointment_date: dt.date
    start_time: dt.time
    duration_minutes: int | None = Field(default=None, ge=5, le=240)
    room_id: int | None = None


class StatusUpdateRequest(BaseModel):
    status: AppointmentStatus


class SlotOut(BaseModel):
    """One bookable slot in a doctor's day."""

    start_time: dt.time
    end_time: dt.time
    available: bool
    reason: str | None = Field(
        default=None, description="Why the slot is unavailable, when it is not"
    )
