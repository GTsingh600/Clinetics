"""Closed value sets, backed by native PostgreSQL ENUM types.

`StrEnum` (3.11+) rather than `(str, enum.Enum)`: same behaviour, but
`str(member)` returns the value rather than `'ClassName.MEMBER'`, which is
what you want when a member is interpolated into a log line or a query.

Native enums (rather than VARCHAR + CHECK) were chosen deliberately: they are
compact on disk, comparison is by ordinal, and an invalid value cannot reach the
table at all.

The cost is migration friction, and it is worth stating plainly:

* Adding a value needs `ALTER TYPE ... ADD VALUE`. Before PostgreSQL 12 that
  could not run inside a transaction at all; it can now, but the new value is
  not usable until that transaction commits, so a migration must not add a
  value and then immediately insert a row using it.
* Removing or renaming a value has no direct DDL. It requires creating a new
  type, converting every column that uses the old one, and dropping the old
  type.

`values_callable` below makes the database store the enum *values*
("no_show"), not the Python member *names* ("NO_SHOW"), which is SQLAlchemy's
default. Without it the stored strings would be uppercase and every hand-written
SQL query in a migration or report would have to match that.
"""

from __future__ import annotations

import enum
from typing import Final

from sqlalchemy import Enum as SAEnum


class UserRole(enum.StrEnum):
    """Exactly three roles. Admin is the primary agent user."""

    PATIENT = "patient"
    DOCTOR = "doctor"
    ADMIN = "admin"


class AppointmentStatus(enum.StrEnum):
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class Urgency(enum.StrEnum):
    """Drives the urgency penalty term in the optimizer objective."""

    ROUTINE = "routine"
    URGENT = "urgent"
    EMERGENCY = "emergency"


class Weekday(enum.IntEnum):
    """ISO-8601 weekday numbering: Monday = 1 ... Sunday = 7.

    IntEnum rather than str so `Weekday(dt.isoweekday())` works directly and
    ordering comparisons behave. Stored as a native enum of the lowercase names.
    """

    MONDAY = 1
    TUESDAY = 2
    WEDNESDAY = 3
    THURSDAY = 4
    FRIDAY = 5
    SATURDAY = 6
    SUNDAY = 7


def _values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.name.lower() for member in enum_cls]


# Reusable column types. Defining them once means the enum type is created in
# the database exactly once and every column references the same type.
user_role_enum: Final = SAEnum(
    UserRole,
    name="user_role",
    values_callable=lambda e: [m.value for m in e],
    native_enum=True,
)

appointment_status_enum: Final = SAEnum(
    AppointmentStatus,
    name="appointment_status",
    values_callable=lambda e: [m.value for m in e],
    native_enum=True,
)

urgency_enum: Final = SAEnum(
    Urgency,
    name="urgency",
    values_callable=lambda e: [m.value for m in e],
    native_enum=True,
)

weekday_enum: Final = SAEnum(
    Weekday,
    name="weekday",
    values_callable=_values,
    native_enum=True,
)

# Statuses that still occupy a slot on the doctor's calendar. A cancelled
# appointment must NOT block the time, or cancelling would permanently burn the
# slot; every other status did consume it. This drives the partial WHERE clause
# on the no-overlap exclusion constraint.
SLOT_BLOCKING_STATUSES: Final[tuple[AppointmentStatus, ...]] = (
    AppointmentStatus.SCHEDULED,
    AppointmentStatus.COMPLETED,
    AppointmentStatus.NO_SHOW,
)
