"""Clinic — the top-level tenant and the owner of operating hours."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, String, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.doctor import Doctor
    from app.models.room import Room


class Clinic(TimestampMixin, Base):
    __tablename__ = "clinic"
    __table_args__ = (CheckConstraint("closes_at > opens_at", name="closes_after_opens"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)

    # Wall-clock operating hours. Appointments store local date + time rather
    # than an absolute instant, so "09:00" means 09:00 to staff and patients
    # regardless of daylight-saving transitions. The timezone below exists for
    # display and for converting to absolute time when needed, not for storage.
    opens_at: Mapped[dt.time] = mapped_column(Time, nullable=False)
    closes_at: Mapped[dt.time] = mapped_column(Time, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    address: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))

    doctors: Mapped[list[Doctor]] = relationship(back_populates="clinic")
    rooms: Mapped[list[Room]] = relationship(back_populates="clinic")
    appointments: Mapped[list[Appointment]] = relationship(back_populates="clinic")

    def __repr__(self) -> str:
        return f"<Clinic id={self.id} name={self.name!r}>"
