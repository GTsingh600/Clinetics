"""Availability — recurring weekly working windows for a doctor.

Its own table, never fields on `Doctor`, because a doctor has *many* windows and
they change over time. Columns like `monday_start` / `monday_end` would cap the
model at one window per day, which cannot express the most ordinary schedule
there is: 09:00-12:00 and 13:00-17:00 with lunch between.

That two-window pattern is also how **mandatory breaks** are represented. The
break is simply the gap between windows, so the optimizer gets break handling
for free from the same constraint that keeps appointments inside availability.

`effective_from` / `effective_to` make the pattern time-boxed rather than
eternal, so a doctor changing hours in March does not rewrite the history that
explains February's schedule.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, Date, ForeignKey, Index, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin
from app.models.enums import Weekday, weekday_enum

if TYPE_CHECKING:
    from app.models.doctor import Doctor


class Availability(Base, TimestampMixin):
    __tablename__ = "availability"
    __table_args__ = (
        CheckConstraint("end_time > start_time", name="end_after_start"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="effective_range_valid",
        ),
        Index("ix_availability_doctor_weekday", "doctor_id", "weekday"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    doctor_id: Mapped[int] = mapped_column(
        # CASCADE: an availability window has no meaning without its doctor.
        ForeignKey("doctor.id", ondelete="CASCADE"),
        nullable=False,
    )

    weekday: Mapped[Weekday] = mapped_column(weekday_enum, nullable=False)
    start_time: Mapped[dt.time] = mapped_column(Time, nullable=False)
    end_time: Mapped[dt.time] = mapped_column(Time, nullable=False)

    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[dt.date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    doctor: Mapped[Doctor] = relationship(back_populates="availabilities")

    def __repr__(self) -> str:
        return (
            f"<Availability doctor_id={self.doctor_id} {self.weekday.name} "
            f"{self.start_time}-{self.end_time}>"
        )
