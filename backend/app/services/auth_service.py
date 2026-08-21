"""Authentication: registration, login, refresh rotation, logout.

All of it lives here rather than in the route handlers, so the same logic is
reachable from a Celery task or a management command. The routes only translate
between HTTP and these calls.

The refresh-rotation design is the part worth reading closely; see
`rotate_refresh_token`.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.models import RefreshToken, User, UserRole

log = logging.getLogger(__name__)


class AuthError(Exception):
    """Authentication failed. Deliberately carries no detail about *why*."""


class InactiveUserError(AuthError):
    pass


class EmailAlreadyRegisteredError(Exception):
    pass


async def register_user(
    db: AsyncSession, *, email: str, password: str, full_name: str, role: UserRole
) -> User:
    existing = await db.scalar(select(User).where(User.email == email.lower()))
    if existing is not None:
        raise EmailAlreadyRegisteredError(email)

    user = User(
        email=email.lower(),
        hashed_password=hash_password(password),
        full_name=full_name,
        role=role,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


async def authenticate(db: AsyncSession, *, email: str, password: str) -> User:
    """Verify credentials.

    Two details that matter for security:

    1. **The same error for every failure.** Unknown email and wrong password
       raise the identical `AuthError`, so responses cannot be used to
       enumerate which addresses have accounts.
    2. **The hash is verified even when the user does not exist.** Otherwise the
       "no such user" path returns in microseconds while the "wrong password"
       path spends ~250ms in bcrypt, and that timing difference is itself an
       account-enumeration oracle.
    """
    user = await db.scalar(select(User).where(User.email == email.lower()))

    if user is None:
        # Dummy verification against a real hash, purely to equalise timing.
        verify_password(password, _DUMMY_HASH)
        raise AuthError("invalid credentials")

    if not verify_password(password, user.hashed_password):
        raise AuthError("invalid credentials")

    if not user.is_active:
        raise InactiveUserError("account is disabled")

    return user


# Precomputed once at import so the dummy path costs the same as a real verify.
_DUMMY_HASH = hash_password("dummy-password-for-constant-time-comparison")


async def issue_session(db: AsyncSession, user: User) -> tuple[str, str, dt.datetime]:
    """Start a new session: one access token and a new refresh-token family."""
    family_id = str(uuid.uuid4())
    access, _, _ = create_access_token(user.id, user.role.value)
    refresh, jti, refresh_expires = create_refresh_token(user.id, family_id)

    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(refresh),
            jti=jti,
            family_id=family_id,
            expires_at=refresh_expires,
        )
    )
    await db.flush()
    return access, refresh, refresh_expires


async def rotate_refresh_token(
    db: AsyncSession, raw_refresh: str
) -> tuple[User, str, str, dt.datetime]:
    """Exchange a refresh token for a fresh pair, rotating the old one out.

    **Reuse detection.** Each refresh token may be redeemed exactly once. If a
    token that has already been rotated is presented again, there are only two
    explanations: the token was captured and replayed, or it was replayed by a
    client that never received the rotated response. Both are handled the same
    way — revoke the *entire family*.

    Revoking the family rather than just the replayed token is the important
    part. An attacker who stole a token has, by the time of the replay, possibly
    already rotated it and holds a newer one in the same chain; revoking only
    the presented token would leave the attacker's session alive and log out the
    victim. Killing the family logs out both, and the legitimate user simply
    signs in again.
    """
    try:
        payload = decode_token(raw_refresh, "refresh")
    except TokenError as exc:
        raise AuthError(str(exc)) from exc

    token_hash = hash_token(raw_refresh)
    stored = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))

    if stored is None:
        raise AuthError("refresh token not recognised")

    if stored.revoked or stored.used_at is not None:
        log.warning(
            "refresh token reuse detected for user_id=%s family=%s; revoking family",
            stored.user_id,
            stored.family_id,
        )
        await _revoke_family(db, stored.family_id)
        raise AuthError("refresh token has already been used")

    if stored.expires_at <= dt.datetime.now(dt.UTC):
        raise AuthError("refresh token expired")

    user = await db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise AuthError("user is not active")

    # Mark the presented token spent, then issue its successor in the same family.
    stored.used_at = dt.datetime.now(dt.UTC)

    access, _, _ = create_access_token(user.id, user.role.value)
    new_refresh, jti, expires = create_refresh_token(user.id, stored.family_id)
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(new_refresh),
            jti=jti,
            family_id=stored.family_id,
            expires_at=expires,
        )
    )
    await db.flush()
    return user, access, new_refresh, expires


async def revoke_session(db: AsyncSession, raw_refresh: str | None) -> None:
    """Log out. Revokes the whole family so every descendant token dies too.

    Tolerates a missing or unparseable token: logging out must always succeed
    from the caller's point of view, and failing here would leave a user unable
    to clear a session they no longer trust.
    """
    if not raw_refresh:
        return
    stored = await db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_refresh))
    )
    if stored is not None:
        await _revoke_family(db, stored.family_id)
        await db.flush()


async def _revoke_family(db: AsyncSession, family_id: str) -> None:
    await db.execute(
        update(RefreshToken).where(RefreshToken.family_id == family_id).values(revoked=True)
    )


async def purge_expired_tokens(db: AsyncSession) -> int:
    """Delete refresh tokens that expired more than a day ago.

    Housekeeping only. Expired tokens are already rejected on presentation; this
    stops the table growing without bound.
    """
    from sqlalchemy import delete
    from sqlalchemy.engine import CursorResult

    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    result = cast(
        CursorResult[Any],
        await db.execute(delete(RefreshToken).where(RefreshToken.expires_at < cutoff)),
    )
    return int(result.rowcount or 0)
