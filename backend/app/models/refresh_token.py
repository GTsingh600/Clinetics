"""Server-side record of issued refresh tokens.

Refresh tokens are long-lived, so unlike access tokens they cannot be
"revoked by expiry". Tracking them makes three things possible:

* **Logout that actually logs out.** Deleting the row invalidates the session
  immediately rather than waiting seven days.
* **Rotation.** Each use issues a new token and marks the old one used.
* **Reuse detection.** If an already-rotated token is presented, the token was
  captured — the legitimate client has since rotated past it. The correct
  response is to revoke the entire *family* (every token descended from that
  login), not just the replayed one, because the attacker may already hold a
  newer token in the same chain.

Only a SHA-256 fingerprint is stored, never the token itself: a database leak
must not hand out live sessions.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class RefreshToken(Base):
    __tablename__ = "refresh_token"
    __table_args__ = (
        Index("ix_refresh_token_family_id", "family_id"),
        Index("ix_refresh_token_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # CASCADE: a deleted account's sessions must not outlive it.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user_account.id", ondelete="CASCADE"), nullable=False
    )
    # SHA-256 hex of the token. Unique so a replay is detectable by lookup.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    jti: Mapped[str] = mapped_column(String(36), nullable=False)
    # Shared by every token descended from one login; the unit of revocation.
    family_id: Mapped[str] = mapped_column(String(36), nullable=False)

    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set when this token is rotated. Presenting a used token is the reuse signal.
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<RefreshToken user_id={self.user_id} fam={self.family_id[:8]} revoked={self.revoked}>"
        )
