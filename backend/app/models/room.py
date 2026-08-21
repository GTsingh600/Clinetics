"""Room — physical capacity the optimizer must respect."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.clinic import Clinic


class Room(Base, TimestampMixin):
    __tablename__ = "room"
    __table_args__ = (
        UniqueConstraint("clinic_id", "name", name="unique_room_name_per_clinic"),
        CheckConstraint("capacity > 0", name="capacity_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    clinic_id: Mapped[int] = mapped_column(
        # RESTRICT: a clinic with rooms cannot be deleted out from under them.
        ForeignKey("clinic.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(60), nullable=False)

    # Concurrent occupancy, not seating. Consultation rooms are 1; a shared
    # treatment bay may hold several patients at once. The optimizer enforces
    # this; it is not a database constraint because it is a property of a
    # *schedule*, not of any single row.
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    clinic: Mapped[Clinic] = relationship(back_populates="rooms")

    def __repr__(self) -> str:
        return f"<Room id={self.id} name={self.name!r} capacity={self.capacity}>"
