"""Authentication and role-based access control.

Authorization tests are worth more than authentication tests. A broken login is
obvious the first time anyone tries it; a missing authorization check is
invisible until someone reads another patient's record. So most of what follows
asserts that requests are *refused*.
"""

from __future__ import annotations

import datetime as dt

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.main import create_app
from app.models import Patient, User, UserRole
from app.services import auth_service

pytestmark = pytest.mark.integration

PASSWORD = "a-sufficiently-long-password"


@pytest.fixture
async def api(db: AsyncSession):
    """An HTTP client whose requests run inside the test's rolled-back session.

    Overriding `get_db` is what makes this work: the app under test writes
    through the same transaction the fixture rolls back, so API tests need no
    cleanup and cannot leak rows into each other.
    """
    app = create_app()

    async def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def make_user(db: AsyncSession, email: str, role: UserRole = UserRole.PATIENT) -> User:
    user = await auth_service.register_user(
        db, email=email, password=PASSWORD, full_name="Test User", role=role
    )
    await db.flush()
    return user


async def login(client: AsyncClient, email: str, password: str = PASSWORD):
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


# --------------------------------------------------------------------------
# Registration and login
# --------------------------------------------------------------------------
async def test_register_then_login_sets_httponly_cookies(api: AsyncClient) -> None:
    resp = await api.post(
        "/api/v1/auth/register",
        json={
            "email": "newuser@example.com",
            "password": PASSWORD,
            "full_name": "New User",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "patient"
    # The password must never come back, in any form.
    assert "password" not in resp.text.lower()

    resp = await login(api, "newuser@example.com")
    assert resp.status_code == 200, resp.text

    # No token in the body: that is the point of httpOnly.
    body = resp.json()
    assert "token" not in resp.text.lower()
    assert body["user"]["email"] == "newuser@example.com"

    cookies = {c.name: c for c in resp.cookies.jar}
    assert settings.access_cookie_name in cookies
    assert settings.refresh_cookie_name in cookies
    raw = resp.headers.get_list("set-cookie")
    assert any("httponly" in h.lower() for h in raw), raw
    assert any("samesite=strict" in h.lower() for h in raw), "refresh cookie must be strict"


async def test_admin_cannot_be_self_registered(api: AsyncClient) -> None:
    """Privilege escalation via the signup form is the classic version of this bug."""
    resp = await api.post(
        "/api/v1/auth/register",
        json={
            "email": "sneaky@example.com",
            "password": PASSWORD,
            "full_name": "Sneaky",
            "role": "admin",
        },
    )
    assert resp.status_code == 422, resp.text


async def test_short_password_rejected(api: AsyncClient) -> None:
    resp = await api.post(
        "/api/v1/auth/register",
        json={"email": "weak@example.com", "password": "short", "full_name": "Weak"},
    )
    assert resp.status_code == 422


async def test_duplicate_email_rejected(api: AsyncClient, db: AsyncSession) -> None:
    await make_user(db, "dupe@example.com")
    resp = await api.post(
        "/api/v1/auth/register",
        json={"email": "dupe@example.com", "password": PASSWORD, "full_name": "Dupe"},
    )
    assert resp.status_code == 409


async def test_wrong_password_and_unknown_email_are_indistinguishable(
    api: AsyncClient, db: AsyncSession
) -> None:
    """Identical responses, so the API cannot be used to enumerate accounts."""
    await make_user(db, "real@example.com")

    wrong_password = await login(api, "real@example.com", "definitely-not-it")
    unknown_email = await login(api, "ghost@example.com", PASSWORD)

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()


async def test_disabled_account_cannot_log_in(api: AsyncClient, db: AsyncSession) -> None:
    user = await make_user(db, "disabled@example.com")
    user.is_active = False
    await db.flush()

    resp = await login(api, "disabled@example.com")
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Session behaviour
# --------------------------------------------------------------------------
async def test_unauthenticated_request_is_rejected(api: AsyncClient) -> None:
    assert (await api.get("/api/v1/auth/me")).status_code == 401


async def test_bearer_header_is_not_accepted(api: AsyncClient, db: AsyncSession) -> None:
    """Only the cookie is honoured.

    Accepting a `Authorization: Bearer` fallback would reintroduce the XSS
    exposure the httpOnly cookie exists to prevent, and would bypass the CSRF
    origin check, which only guards the cookie path.
    """
    user = await make_user(db, "bearer@example.com")
    access, _, _ = await auth_service.issue_session(db, user)
    await db.flush()

    resp = await api.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert resp.status_code == 401


async def test_logout_clears_cookies_and_revokes_the_session(
    api: AsyncClient, db: AsyncSession
) -> None:
    await make_user(db, "bye@example.com")
    await login(api, "bye@example.com")
    assert (await api.get("/api/v1/auth/me")).status_code == 200

    resp = await api.post("/api/v1/auth/logout")
    assert resp.status_code == 200
    assert (await api.get("/api/v1/auth/me")).status_code == 401


async def test_refresh_rotates_the_token(api: AsyncClient, db: AsyncSession) -> None:
    await make_user(db, "rotate@example.com")
    await login(api, "rotate@example.com")
    before = api.cookies.get(settings.refresh_cookie_name)

    resp = await api.post("/api/v1/auth/refresh")
    assert resp.status_code == 200, resp.text
    after = api.cookies.get(settings.refresh_cookie_name)

    assert after is not None
    assert after != before, "the refresh token must change on every use"


async def test_reusing_a_rotated_refresh_token_revokes_the_family(
    api: AsyncClient, db: AsyncSession
) -> None:
    """Reuse detection.

    Replaying an already-rotated refresh token means it was captured. Revoking
    only that token would leave an attacker who has since rotated it still
    logged in; revoking the whole family ends both sessions, and the legitimate
    user simply signs in again.
    """
    user = await make_user(db, "stolen@example.com")
    _, stolen_refresh, _ = await auth_service.issue_session(db, user)
    await db.flush()

    # The legitimate client rotates once.
    user2, _, fresh_refresh, _ = await auth_service.rotate_refresh_token(db, stolen_refresh)
    assert user2.id == user.id

    # The attacker replays the old one.
    with pytest.raises(auth_service.AuthError):
        await auth_service.rotate_refresh_token(db, stolen_refresh)

    # ...which must also kill the token the legitimate client is now holding.
    with pytest.raises(auth_service.AuthError):
        await auth_service.rotate_refresh_token(db, fresh_refresh)

    revoked = await db.scalar(
        text("SELECT bool_and(revoked) FROM refresh_token WHERE user_id = :u").bindparams(u=user.id)
    )
    assert revoked is True, "every token in the family must be revoked"


# --------------------------------------------------------------------------
# RBAC
# --------------------------------------------------------------------------
async def test_patient_cannot_list_all_patients(api: AsyncClient, db: AsyncSession) -> None:
    """The endpoint that would leak the entire patient roster."""
    await make_user(db, "nosy@example.com", UserRole.PATIENT)
    await login(api, "nosy@example.com")

    resp = await api.get("/api/v1/patients")
    assert resp.status_code == 403


async def test_admin_can_list_patients(api: AsyncClient, db: AsyncSession) -> None:
    await make_user(db, "boss@example.com", UserRole.ADMIN)
    await login(api, "boss@example.com")

    resp = await api.get("/api/v1/patients")
    assert resp.status_code == 200


async def test_patient_cannot_read_another_patients_record(
    api: AsyncClient, db: AsyncSession
) -> None:
    """Object-level authorization, not just role-level.

    Both callers hold the `patient` role, so a role check alone would let either
    read the other. This is the most common authorization bug in applications
    shaped like this one.
    """
    alice_user = await make_user(db, "alice@example.com", UserRole.PATIENT)
    bob_user = await make_user(db, "bob@example.com", UserRole.PATIENT)

    alice = Patient(
        first_name="Alice",
        last_name="A",
        date_of_birth=dt.date(1990, 1, 1),
        user_id=alice_user.id,
    )
    bob = Patient(
        first_name="Bob", last_name="B", date_of_birth=dt.date(1991, 1, 1), user_id=bob_user.id
    )
    db.add_all([alice, bob])
    await db.flush()

    await login(api, "alice@example.com")

    own = await api.get(f"/api/v1/patients/{alice.id}")
    assert own.status_code == 200, own.text

    other = await api.get(f"/api/v1/patients/{bob.id}")
    # 404, not 403: a 403 would confirm the record exists and let a caller probe
    # for valid ids.
    assert other.status_code == 404


async def test_patient_cannot_set_appointment_status(api: AsyncClient, db: AsyncSession) -> None:
    """`no_show` is the label the Phase 3 classifier trains on.

    Letting the subject of the prediction write their own outcome would corrupt
    the training data, quite apart from being wrong operationally.
    """
    await make_user(db, "selfmark@example.com", UserRole.PATIENT)
    await login(api, "selfmark@example.com")

    resp = await api.patch("/api/v1/appointments/1/status", json={"status": "completed"})
    assert resp.status_code == 403


async def test_patient_cannot_reach_admin_dashboard(api: AsyncClient, db: AsyncSession) -> None:
    await make_user(db, "peek@example.com", UserRole.PATIENT)
    await login(api, "peek@example.com")
    assert (await api.get("/api/v1/dashboard/admin")).status_code == 403


async def test_doctor_cannot_reach_admin_dashboard(api: AsyncClient, db: AsyncSession) -> None:
    await make_user(db, "doc@example.com", UserRole.DOCTOR)
    await login(api, "doc@example.com")
    assert (await api.get("/api/v1/dashboard/admin")).status_code == 403


async def test_patient_cannot_trigger_admin_tasks(api: AsyncClient, db: AsyncSession) -> None:
    await make_user(db, "tasker@example.com", UserRole.PATIENT)
    await login(api, "tasker@example.com")
    resp = await api.post("/api/v1/tasks/reconcile-utilization")
    assert resp.status_code == 403


async def test_doctor_role_without_a_doctor_record_is_404_not_403(
    api: AsyncClient, db: AsyncSession
) -> None:
    """A data gap, not an authorization failure — and the codes should say so."""
    await make_user(db, "orphan@example.com", UserRole.DOCTOR)
    await login(api, "orphan@example.com")
    assert (await api.get("/api/v1/dashboard/doctor")).status_code == 404


async def test_role_change_takes_effect_immediately(api: AsyncClient, db: AsyncSession) -> None:
    """Privilege revocation must not wait for the access token to expire.

    The role is embedded in the token for speed, but `get_current_user` re-reads
    it from the database, so a demotion applies on the very next request.
    """
    user = await make_user(db, "demote@example.com", UserRole.ADMIN)
    await login(api, "demote@example.com")
    assert (await api.get("/api/v1/patients")).status_code == 200

    user.role = UserRole.PATIENT
    await db.flush()

    assert (await api.get("/api/v1/patients")).status_code == 403


async def test_deactivated_mid_session_is_locked_out(api: AsyncClient, db: AsyncSession) -> None:
    user = await make_user(db, "revoke@example.com", UserRole.ADMIN)
    await login(api, "revoke@example.com")
    assert (await api.get("/api/v1/auth/me")).status_code == 200

    user.is_active = False
    await db.flush()

    resp = await api.get("/api/v1/auth/me")
    # 403 rather than 401: the credentials are valid, the account is disabled,
    # and re-authenticating cannot help.
    assert resp.status_code == 403


async def test_doctor_sees_only_their_own_appointments(api: AsyncClient, db: AsyncSession) -> None:
    """Scoping is applied to the query, not filtered from the results."""
    from tests.integration.factories import (
        base_fixtures,
        make_appointment,
        make_doctor,
    )

    clinic, doctor_a, patient, specialty = await base_fixtures(db)
    doctor_b = await make_doctor(db, clinic, license_number="LIC-SCOPE", first_name="Other")

    user = await make_user(db, "scoped@example.com", UserRole.DOCTOR)
    doctor_a.user_id = user.id
    await db.flush()

    await make_appointment(db, clinic=clinic, doctor=doctor_a, patient=patient, specialty=specialty)
    await make_appointment(
        db,
        clinic=clinic,
        doctor=doctor_b,
        patient=patient,
        specialty=specialty,
        start=dt.time(11, 0),
        end=dt.time(11, 30),
    )

    await login(api, "scoped@example.com")
    resp = await api.get("/api/v1/appointments")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert all(item["doctor_id"] == doctor_a.id for item in body["items"])


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------
async def test_cross_origin_post_is_rejected(api: AsyncClient, db: AsyncSession) -> None:
    """The cookie the browser attaches automatically is what makes CSRF possible."""
    await make_user(db, "csrf@example.com")
    await login(api, "csrf@example.com")

    resp = await api.post("/api/v1/auth/logout", headers={"Origin": "https://evil.example.com"})
    assert resp.status_code == 403
    assert "cross-origin" in resp.json()["detail"]


async def test_same_origin_post_is_allowed(api: AsyncClient, db: AsyncSession) -> None:
    await make_user(db, "sameorigin@example.com")
    await login(api, "sameorigin@example.com")

    resp = await api.post("/api/v1/auth/logout", headers={"Origin": settings.cors_origins[0]})
    assert resp.status_code == 200


async def test_cross_origin_get_is_allowed(api: AsyncClient, db: AsyncSession) -> None:
    """Safe methods are not blocked; CSRF concerns state changes."""
    await make_user(db, "safeget@example.com")
    await login(api, "safeget@example.com")

    resp = await api.get("/api/v1/auth/me", headers={"Origin": "https://evil.example.com"})
    assert resp.status_code == 200
