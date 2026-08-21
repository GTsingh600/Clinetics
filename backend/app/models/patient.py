"""Patient.

Note what is *not* stored here: the patient's historical no-show rate. It is a
model feature, but caching it on the row would make it a denormalization that
goes stale on every appointment outcome, and this schema is allowed exactly one
deliberate denormalization (on `Appointment`). Phase 3 computes it from
appointment history at feature-engineering time.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.appointment import Appointment


class Patient(Base, TimestampMixin):
    __tablename__ = "patient"
    __table_args__ = (CheckConstraint("date_of_birth <= CURRENT_DATE", name="dob_not_in_future"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL"), unique=True
    )

    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    date_of_birth: Mapped[dt.date] = mapped_column(Date, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(254), index=True)

    appointments: Mapped[list[Appointment]] = relationship(back_populates="patient")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def __repr__(self) -> str:
        return f"<Patient id={self.id} name={self.full_name!r}>"
