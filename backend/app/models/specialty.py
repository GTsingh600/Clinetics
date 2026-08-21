"""Specialty — a first-class entity, never a string column on Doctor.

A doctor may hold several specialties and a specialty is held by many doctors,
so the relationship is many-to-many through `doctor_specialty`. Storing it as
text on `Doctor` would make "which doctors can take a dermatology appointment?"
a string match, and would leave typos indistinguishable from real values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.doctor import Doctor


class Specialty(Base, TimestampMixin):
    __tablename__ = "specialty"
    __table_args__ = (
        CheckConstraint("default_duration_minutes > 0", name="duration_positive"),
        CheckConstraint("default_duration_minutes <= 480", name="duration_within_working_day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)

    # Planning default used by the generator and as a fallback when an
    # appointment is created without an explicit duration.
    default_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    description: Mapped[str | None] = mapped_column(String(255))

    doctors: Mapped[list[Doctor]] = relationship(
        secondary="doctor_specialty", back_populates="specialties", viewonly=True
    )

    def __repr__(self) -> str:
        return f"<Specialty id={self.id} name={self.name!r}>"
