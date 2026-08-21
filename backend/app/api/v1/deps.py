"""Request-scoped dependencies: the current user, and role enforcement.

Authorization lives here as reusable dependencies rather than as `if` statements
inside handlers. That matters for a reason beyond tidiness: a check written into
a handler protects that handler only, and the next endpoint someone adds starts
unprotected by default. A dependency makes the requirement declarative and
visible in the route signature — and it shows up in the OpenAPI schema.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.security import TokenError, decode_token
from app.models import Doctor, Patient, User, UserRole

CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
)


def _read_access_token(request: Request) -> str:
    """Read the access token from its httpOnly cookie.

    A `Authorization: Bearer` header is deliberately NOT accepted as a fallback.
    Supporting both would reintroduce the XSS exposure the cookie exists to
    avoid, because any script could then mint requests with a stolen token — and
    it would silently bypass the CSRF protection, which only guards the cookie
    path.
    """
    token = request.cookies.get(settings.access_cookie_name)
    if not token:
        raise CREDENTIALS_EXCEPTION
    return token


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = _read_access_token(request)
    try:
        payload = decode_token(token, "access")
    except TokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    user = await db.get(User, int(payload["sub"]))
    if user is None:
        raise CREDENTIALS_EXCEPTION
    if not user.is_active:
        # 403, not 401: the credentials were valid, the account is disabled.
        # A 401 would prompt the client to retry logging in, which cannot help.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is disabled")

    # The token carries a role claim so most requests need no extra query, but
    # it is re-read from the database here. If an admin demotes someone
    # mid-session, the demotion takes effect immediately rather than lingering
    # until their access token expires. Privilege revocation must not be lazy.
    return user


def require_role(*allowed: UserRole) -> Callable[..., Awaitable[User]]:
    """Dependency factory enforcing that the caller holds one of `allowed`.

    @router.get("/admin-only", dependencies=[Depends(require_role(UserRole.ADMIN))])
    """

    async def _dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires role: {', '.join(r.value for r in allowed)}",
            )
        return user

    return _dependency


require_admin = require_role(UserRole.ADMIN)
require_staff = require_role(UserRole.ADMIN, UserRole.DOCTOR)


async def get_current_doctor(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Doctor:
    """The Doctor record for the logged-in user.

    A user with the `doctor` role but no linked Doctor row is a data problem,
    not an auth problem, so it is a 404 rather than a 403.
    """
    if user.role is not UserRole.DOCTOR:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="requires role: doctor")
    from sqlalchemy import select

    doctor = await db.scalar(select(Doctor).where(Doctor.user_id == user.id))
    if doctor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no doctor record is linked to this account",
        )
    return doctor


async def get_current_patient(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Patient:
    if user.role is not UserRole.PATIENT:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="requires role: patient")
    from sqlalchemy import select

    patient = await db.scalar(select(Patient).where(Patient.user_id == user.id))
    if patient is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no patient record is linked to this account",
        )
    return patient


async def assert_may_view_patient(
    patient_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Object-level authorization for patient data.

    Role checks alone are not enough here. A `patient` is authorized to read
    *their own* record, not the patient table — checking only the role would let
    any logged-in patient enumerate every other patient by id, which is the
    single most common authorization bug in applications like this one.
    """
    if user.role in (UserRole.ADMIN, UserRole.DOCTOR):
        return
    from sqlalchemy import select

    own = await db.scalar(select(Patient.id).where(Patient.user_id == user.id))
    if own != patient_id:
        # 404 rather than 403: a 403 would confirm the record exists, letting a
        # caller probe for valid ids.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
