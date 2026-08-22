"""Prediction endpoints, with the access rule they exist to enforce.

The most important test in this file is the one asserting a patient CANNOT read
a no-show prediction — including their own. That is a deliberate product
decision, not an oversight, and without a test it is one refactor away from
quietly disappearing.
"""

from __future__ import annotations

import datetime as dt

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.main import create_app
from app.models import UserRole
from app.services import auth_service, inference_service

pytestmark = pytest.mark.integration

PASSWORD = "a-sufficiently-long-password"
FUTURE = dt.date.today() + dt.timedelta(days=14)


@pytest.fixture
async def api(db: AsyncSession):
    app = create_app()

    async def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def _login(api: AsyncClient, db: AsyncSession, email: str, role: UserRole) -> None:
    await auth_service.register_user(
        db, email=email, password=PASSWORD, full_name="Test", role=role
    )
    await db.flush()
    resp = await api.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text


def _no_show_payload(patient_id: int = 1) -> list[dict]:
    return [
        {
            "patient_id": patient_id,
            "specialty": "cardiology",
            "appointment_date": FUTURE.isoformat(),
            "start_time": "09:00:00",
            "urgency": "routine",
            "is_new_patient": False,
        }
    ]


def _models_loaded() -> bool:
    return inference_service.get_models().no_show is not None


needs_models = pytest.mark.skipif(
    not _models_loaded(),
    reason="no trained artifacts; run scripts/train_models.py",
)


# --------------------------------------------------------------------------
# Access control — the point of the module
# --------------------------------------------------------------------------
async def test_patient_cannot_read_a_no_show_prediction(api: AsyncClient, db: AsyncSession) -> None:
    """A patient must not see their own predicted no-show risk.

    Two reasons, and neither is about permissions in the usual sense:

    * telling someone the system expects them to miss is a self-fulfilling nudge
    * the score is driven partly by their own history, so showing it to them
      turns a statistical estimate into something that reads as an accusation

    The patient dashboard shows factual attendance history and nothing
    predictive. This test is what stops that decision eroding.
    """
    await _login(api, db, "patient@example.com", UserRole.PATIENT)
    resp = await api.post("/api/v1/predictions/no-show", json=_no_show_payload())
    assert resp.status_code == 403


async def test_patient_cannot_read_demand_or_duration(api: AsyncClient, db: AsyncSession) -> None:
    await _login(api, db, "patient2@example.com", UserRole.PATIENT)
    demand = await api.get(
        f"/api/v1/predictions/demand?specialty=cardiology&start_date={FUTURE}&end_date={FUTURE}"
    )
    duration = await api.post(
        "/api/v1/predictions/duration",
        json=[
            {
                "patient_id": 1,
                "specialty": "cardiology",
                "appointment_date": FUTURE.isoformat(),
                "start_time": "09:00:00",
            }
        ],
    )
    assert demand.status_code == 403
    assert duration.status_code == 403


async def test_unauthenticated_requests_are_rejected(api: AsyncClient) -> None:
    resp = await api.post("/api/v1/predictions/no-show", json=_no_show_payload())
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# Behaviour
# --------------------------------------------------------------------------
@needs_models
async def test_admin_gets_a_calibrated_probability(api: AsyncClient, db: AsyncSession) -> None:
    await _login(api, db, "admin@example.com", UserRole.ADMIN)
    resp = await api.post("/api/v1/predictions/no-show", json=_no_show_payload())
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert len(body) == 1
    assert 0.0 <= body[0]["no_show_probability"] <= 1.0
    # The threshold comes from the model card, so serving provably uses the
    # operating point the metrics were reported at.
    assert 0.0 < body[0]["threshold"] < 1.0
    assert isinstance(body[0]["flagged"], bool)


@needs_models
async def test_doctor_may_also_read_predictions(api: AsyncClient, db: AsyncSession) -> None:
    await _login(api, db, "doc@example.com", UserRole.DOCTOR)
    resp = await api.post("/api/v1/predictions/no-show", json=_no_show_payload())
    assert resp.status_code == 200, resp.text


@needs_models
async def test_longer_lead_time_raises_predicted_risk(api: AsyncClient, db: AsyncSession) -> None:
    """An end-to-end check that the strongest modelled effect survives serving.

    Lead time is the dominant predictor by construction. If the API's answer did
    not move with it, something between the request and the model would be
    broken — a feature name mismatch, or a training/serving skew — and no unit
    test of the model alone would catch it.
    """
    await _login(api, db, "admin2@example.com", UserRole.ADMIN)
    soon = _no_show_payload()
    soon[0]["appointment_date"] = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    far = _no_show_payload()
    far[0]["appointment_date"] = (dt.date.today() + dt.timedelta(days=75)).isoformat()

    soon_p = (await api.post("/api/v1/predictions/no-show", json=soon)).json()[0]
    far_p = (await api.post("/api/v1/predictions/no-show", json=far)).json()[0]

    assert (
        far_p["no_show_probability"] > soon_p["no_show_probability"]
    ), "a booking made far in advance should carry more predicted risk"


@needs_models
async def test_urgent_appointments_are_predicted_lower_risk(
    api: AsyncClient, db: AsyncSession
) -> None:
    await _login(api, db, "admin3@example.com", UserRole.ADMIN)
    routine = _no_show_payload()
    emergency = _no_show_payload()
    emergency[0]["urgency"] = "emergency"

    routine_p = (await api.post("/api/v1/predictions/no-show", json=routine)).json()[0]
    emergency_p = (await api.post("/api/v1/predictions/no-show", json=emergency)).json()[0]

    assert emergency_p["no_show_probability"] < routine_p["no_show_probability"]


@needs_models
async def test_demand_forecast_covers_every_clinic_hour(api: AsyncClient, db: AsyncSession) -> None:
    await _login(api, db, "admin4@example.com", UserRole.ADMIN)
    resp = await api.get(
        f"/api/v1/predictions/demand?specialty=dermatology&start_date={FUTURE}&end_date={FUTURE}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert len(body) == 11  # 08:00..18:00 inclusive
    assert all(p["predicted_demand"] >= 0 for p in body)
    assert {p["hour"] for p in body} == set(range(8, 19))


@needs_models
async def test_dermatology_is_forecast_to_peak_later_than_general_practice(
    api: AsyncClient, db: AsyncSession
) -> None:
    """The specialty-specific intra-day shape survives all the way to the API.

    Phase 1 built dermatology as evening-weighted and general practice as
    morning-weighted, and Phase 1's gate proved it is in the data. This checks
    the model learned it and serving reports it.
    """
    await _login(api, db, "admin5@example.com", UserRole.ADMIN)

    async def peak_hour(specialty: str) -> int:
        resp = await api.get(
            f"/api/v1/predictions/demand?specialty={specialty}&start_date={FUTURE}&end_date={FUTURE}"
        )
        assert resp.status_code == 200, resp.text
        return max(resp.json(), key=lambda p: p["predicted_demand"])["hour"]

    assert await peak_hour("dermatology") > await peak_hour("general-practice")


async def test_duration_falls_back_rather_than_failing(api: AsyncClient, db: AsyncSession) -> None:
    """The optimizer always needs a number; an error would be less useful."""
    await _login(api, db, "admin6@example.com", UserRole.ADMIN)
    resp = await api.post(
        "/api/v1/predictions/duration",
        json=[
            {
                "patient_id": 1,
                "specialty": "cardiology",
                "appointment_date": FUTURE.isoformat(),
                "start_time": "09:00:00",
                "is_new_patient": True,
            }
        ],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body[0]["predicted_duration_minutes"] > 0


async def test_status_reports_provenance(api: AsyncClient, db: AsyncSession) -> None:
    """A prediction whose provenance cannot be checked should not be trusted."""
    await _login(api, db, "admin7@example.com", UserRole.ADMIN)
    resp = await api.get("/api/v1/predictions/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "available" in body and "models" in body
    if body["available"] and "no_show" in body["models"]:
        card = body["models"]["no_show"]
        assert card["seed"] is not None
        assert card["threshold_rationale"], "the operating point must carry its reason"


async def test_demand_range_is_bounded(api: AsyncClient, db: AsyncSession) -> None:
    """A 10-year request would build a grid nobody asked for."""
    await _login(api, db, "admin8@example.com", UserRole.ADMIN)
    resp = await api.get(
        "/api/v1/predictions/demand?specialty=cardiology"
        f"&start_date={FUTURE}&end_date={FUTURE + dt.timedelta(days=400)}"
    )
    assert resp.status_code == 422


async def test_reversed_date_range_is_rejected(api: AsyncClient, db: AsyncSession) -> None:
    await _login(api, db, "admin9@example.com", UserRole.ADMIN)
    resp = await api.get(
        "/api/v1/predictions/demand?specialty=cardiology"
        f"&start_date={FUTURE}&end_date={FUTURE - dt.timedelta(days=5)}"
    )
    assert resp.status_code == 422
