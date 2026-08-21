"""Auth routes: register, login, refresh, logout, me.

Tokens travel as httpOnly cookies and never appear in a response body, so no
JavaScript on the page can read them. That closes the XSS token-theft path, and
opens a CSRF one — which is why `SameSite` is set and why unsafe methods are
additionally guarded by the CSRF middleware.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user
from app.core.config import settings
from app.core.db import get_db
from app.models import User
from app.schemas.auth import LoginRequest, RegisterRequest, SessionOut, UserOut
from app.schemas.common import Message
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_PATH = "/api/v1/auth/refresh"


def _set_auth_cookies(
    response: Response, access: str, refresh: str, refresh_expires: dt.datetime
) -> None:
    """Attach both tokens as httpOnly cookies.

    The two differ on purpose:

    * `SameSite=lax` on access — the app makes ordinary top-level navigations,
      and `strict` would drop the cookie when a user follows a link in from
      outside, logging them out for no security gain.
    * `SameSite=strict` and a narrow `path` on refresh — it is only ever sent to
      the refresh endpoint, so it is not attached to the hundreds of other
      requests an app makes, shrinking the surface for it to leak through.
    """
    response.set_cookie(
        settings.access_cookie_name,
        access,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
        domain=settings.cookie_domain,
        path="/",
    )
    response.set_cookie(
        settings.refresh_cookie_name,
        refresh,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=int((refresh_expires - dt.datetime.now(dt.UTC)).total_seconds()),
        domain=settings.cookie_domain,
        path=REFRESH_PATH,
    )


def _clear_auth_cookies(response: Response) -> None:
    # Path and domain must match how the cookie was set, or the browser keeps it.
    response.delete_cookie(settings.access_cookie_name, path="/", domain=settings.cookie_domain)
    response.delete_cookie(
        settings.refresh_cookie_name, path=REFRESH_PATH, domain=settings.cookie_domain
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)) -> User:
    """Self-service signup. The schema forbids requesting the admin role."""
    try:
        user = await auth_service.register_user(
            db,
            email=payload.email,
            password=payload.password,
            full_name=payload.full_name,
            role=payload.role,
        )
    except auth_service.EmailAlreadyRegisteredError as exc:
        # This does leak that an address is registered. Signup cannot avoid it:
        # the alternative is silently succeeding and emailing the real owner,
        # which needs mail infrastructure that is out of scope here.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="that email is already registered"
        ) from exc
    await db.commit()
    return user


@router.post("/login", response_model=SessionOut)
async def login(
    payload: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)
) -> dict[str, User]:
    try:
        user = await auth_service.authenticate(db, email=payload.email, password=payload.password)
    except auth_service.InactiveUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="account is disabled"
        ) from exc
    except auth_service.AuthError as exc:
        # Identical response for unknown email and wrong password.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
        ) from exc

    access, refresh, refresh_expires = await auth_service.issue_session(db, user)
    await db.commit()
    _set_auth_cookies(response, access, refresh, refresh_expires)
    return {"user": user}


@router.post("/refresh", response_model=SessionOut)
async def refresh(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> dict[str, User]:
    """Exchange the refresh cookie for a new token pair.

    Rotation happens on every call, and presenting an already-rotated token
    revokes the whole family — see `auth_service.rotate_refresh_token`.
    """
    raw = request.cookies.get(settings.refresh_cookie_name)
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="no refresh token")
    try:
        user, access, new_refresh, expires = await auth_service.rotate_refresh_token(db, raw)
    except auth_service.AuthError as exc:
        await db.commit()  # persist the family revocation on reuse detection
        _clear_auth_cookies(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    await db.commit()
    _set_auth_cookies(response, access, new_refresh, expires)
    return {"user": user}


@router.post("/logout", response_model=Message)
async def logout(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> Message:
    """Always succeeds. A user clearing a session they distrust must not be
    blocked by a malformed or already-expired token."""
    await auth_service.revoke_session(db, request.cookies.get(settings.refresh_cookie_name))
    await db.commit()
    _clear_auth_cookies(response)
    return Message(detail="logged out")


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user
