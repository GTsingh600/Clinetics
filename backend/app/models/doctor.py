"""Doctor and the Doctor <-> Specialty junction table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
)
from sqlalchemy import (
    Boolean as SABoolean,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.availability import Availability
    from app.models.clinic import Clinic
    from app.models.specialty import Specialty


# Junction table for the many-to-many. Declared as a Core Table rather than an
# ORM class because it carries only the relationship plus one flag; there is no
# behaviour to hang off it.
#
# ON DELETE choices differ on the two sides on purpose:
#   doctor_id    CASCADE  - the link is meaningless once the doctor is gone
#   specialty_id RESTRICT - a specialty still assigned to doctors must not be
#                           deleted; that would silently strip qualifications
doctor_specialty = Table(
    "doctor_specialty",
    Base.metadata,
    Column("doctor_id", Integer, ForeignKey("doctor.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "specialty_id",
        Integer,
        ForeignKey("specialty.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("is_primary", SABoolean, nullable=False, server_default="false"),
    # At most one primary specialty per doctor. A partial unique index is the
    # right tool: a plain UNIQUE(doctor_id, is_primary) would also forbid a
    # doctor having two *non*-primary specialties.
    Index(
        "uq_doctor_specialty_one_primary",
        "doctor_id",
        unique=True,
        postgresql_where=Column("is_primary"),
    ),
)


class Doctor(Base, TimestampMixin):
    __tablename__ = "doctor"

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(
        ForeignKey("clinic.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # 0..1 link to a login. SET NULL: revoking portal access must never delete
    # the clinical record or its appointment history.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL"), unique=True
    )

    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    license_number: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    clinic: Mapped[Clinic] = relationship(back_populates="doctors")
    specialties: Mapped[list[Specialty]] = relationship(
        secondary=doctor_specialty, back_populates="doctors"
    )
    availabilities: Mapped[list[Availability]] = relationship(
        back_populates="doctor", cascade="all, delete-orphan"
    )
    appointments: Mapped[list[Appointment]] = relationship(back_populates="doctor")

    @property
    def full_name(self) -> str:
        return f"Dr. {self.first_name} {self.last_name}"

    def __repr__(self) -> str:
        return f"<Doctor id={self.id} name={self.full_name!r}>"
