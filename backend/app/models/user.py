"""User — authentication identity, distinct from the clinical entities.

`Doctor` and `Patient` are domain records; a `User` is a login. They are kept
separate because the mapping is neither total nor symmetric: an `admin` has no
doctor or patient row at all, and a doctor or patient record can exist for
someone who has never been given portal access (created by reception, imported
from another system).

The link is therefore a nullable, unique FK on the clinical side — a 0..1 to
0..1 relationship. `ON DELETE SET NULL` means deleting a login never destroys
clinical history; it only revokes access.

Password hashing itself is Phase 2. This table only reserves the column.
"""

from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import TimestampMixin
from app.models.enums import UserRole, user_role_enum


class User(Base, TimestampMixin):
    __tablename__ = "user_account"  # "user" is reserved in PostgreSQL

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(user_role_enum, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    full_name: Mapped[str | None] = mapped_column(String(120))

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r} role={self.role.value}>"
