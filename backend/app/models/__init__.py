"""ORM model registry.

Every model module MUST be imported here. Alembic's autogenerate diffs
`Base.metadata` against the live database; a model that is never imported is
invisible to it and will be silently omitted from migrations.
"""

from __future__ import annotations

from app.core.db import Base
from app.models.analytics import (
    ANALYTICS_SCHEMA,
    DoctorUtilization,
    Forecast,
    Schedule,
    ScheduleEntry,
)
from app.models.appointment import Appointment
from app.models.availability import Availability
from app.models.clinic import Clinic
from app.models.doctor import Doctor, doctor_specialty
from app.models.enums import (
    SLOT_BLOCKING_STATUSES,
    AppointmentStatus,
    Urgency,
    UserRole,
    Weekday,
)
from app.models.patient import Patient
from app.models.room import Room
from app.models.specialty import Specialty
from app.models.user import User

__all__ = [
    "ANALYTICS_SCHEMA",
    "SLOT_BLOCKING_STATUSES",
    "Appointment",
    "AppointmentStatus",
    "Availability",
    "Base",
    "Clinic",
    "Doctor",
    "DoctorUtilization",
    "Forecast",
    "Patient",
    "Room",
    "Schedule",
    "ScheduleEntry",
    "Specialty",
    "Urgency",
    "User",
    "UserRole",
    "Weekday",
    "doctor_specialty",
]
