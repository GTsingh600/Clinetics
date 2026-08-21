"""The booking routes end to end, through HTTP.

These exist because of a bug the other suites structurally could not catch. The
race tests call `booking_service` directly, and the auth tests never book, so
nothing exercised a *successful* `POST /appointments` — and the success path was
broken: serialising `AppointmentOut` lazy-loaded the doctor/patient/specialty
relationships, which on an async session raises `MissingGreenlet`. The row was
written and the client got a 500.

The lesson generalises: testing the service layer and testing authorization are
both necessary and neither covers response serialisation. That only happens when
a request goes through the whole stack.
"""

from __future__ import annotations

import datetime as dt

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.main import create_app
from app.models import Availability, Patient, UserRole, Weekday
from app.services import auth_service
from tests.integration.factories import make_clinic, make_doctor, make_patient, make_specialty

pytestmark = pytest.mark.integration

PASSWORD = "a-sufficiently-long-password"
# A Tuesday well in the future, so "not in the past" rules never interfere.
BOOK_DATE = dt.date.today() + dt.timedelta(days=(1 - dt.date.today().weekday()) % 7 + 14)


@pytest.fixture
async def api(db: AsyncSession):
    app = create_app()

    async def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
async def world(db: AsyncSession):
    """A clinic with one doctor who works the booking day, and one patient."""
    clinic = await make_clinic(db)
    specialty = await make_specialty(db, "Cardiology")
    doctor = await make_doctor(db, clinic)
    await db.execute(
        text(
            "INSERT INTO doctor_specialty (doctor_id, specialty_id, is_primary) "
            "VALUES (:d, :s, true)"
        ),
        {"d": doctor.id, "s": specialty.id},
    )
    db.add(
        Availability(
            doctor_id=doctor.id,
            weekday=Weekday(BOOK_DATE.isoweekday()),
            start_time=dt.time(9, 0),
            end_time=dt.time(12, 0),
            effective_from=dt.date(2020, 1, 1),
            is_active=True,
        )
    )
    patient = await make_patient(db)

    admin = await auth_service.register_user(
        db,
        email="booker@example.com",
        password=PASSWORD,
        full_name="Booker",
        role=UserRole.ADMIN,
    )
    await db.flush()
    return {
        "clinic": clinic,
        "doctor": doctor,
        "patient": patient,
        "specialty": specialty,
        "admin": admin,
    }


async def _login(api: AsyncClient, email: str = "booker@example.com") -> None:
    resp = await api.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text


def _payload(world: dict, start: str = "09:00:00", **over: object) -> dict:
    body = {
        "doctor_id": world["doctor"].id,
        "patient_id": world["patient"].id,
        "specialty_id": world["specialty"].id,
        "appointment_date": BOOK_DATE.isoformat(),
        "start_time": start,
        "duration_minutes": 30,
    }
    body.update(over)
    return body


async def test_booking_returns_201_with_nested_objects(api: AsyncClient, world: dict) -> None:
    """THE REGRESSION TEST.

    Asserts the nested doctor/patient/specialty are actually present, not merely
    that the status is 201 — the bug produced a 500 precisely while serialising
    them, so a status-only assertion would have been weaker than it looks.
    """
    await _login(api)
    resp = await api.post("/api/v1/appointments", json=_payload(world))
    assert resp.status_code == 201, resp.text

    body = resp.json()
    assert body["duration_minutes"] == 30
    assert body["status"] == "scheduled"
    assert body["doctor"]["id"] == world["doctor"].id
    assert body["patient"]["id"] == world["patient"].id
    assert body["specialty"]["slug"] == "cardiology"


async def test_double_booking_returns_409_not_500(api: AsyncClient, world: dict) -> None:
    """The loser of the slot race must get a clean, actionable status."""
    await _login(api)
    first = await api.post("/api/v1/appointments", json=_payload(world))
    assert first.status_code == 201, first.text

    second = await api.post("/api/v1/appointments", json=_payload(world))
    assert second.status_code == 409, second.text
    assert "just taken" in second.json()["detail"]


async def test_back_to_back_bookings_are_allowed(api: AsyncClient, world: dict) -> None:
    await _login(api)
    assert (
        await api.post("/api/v1/appointments", json=_payload(world, "09:00:00"))
    ).status_code == 201
    resp = await api.post("/api/v1/appointments", json=_payload(world, "09:30:00"))
    assert resp.status_code == 201, resp.text


async def test_booking_outside_availability_is_422(api: AsyncClient, world: dict) -> None:
    """13:00 is inside clinic hours but outside this doctor's 09:00-12:00 window."""
    await _login(api)
    resp = await api.post("/api/v1/appointments", json=_payload(world, "13:00:00"))
    assert resp.status_code == 422
    assert "availability" in resp.json()["detail"]


async def test_booking_in_the_past_is_rejected(api: AsyncClient, world: dict) -> None:
    await _login(api)
    resp = await api.post(
        "/api/v1/appointments",
        json=_payload(world, appointment_date=(dt.date.today() - dt.timedelta(days=1)).isoformat()),
    )
    assert resp.status_code == 422


async def test_specialty_mismatch_is_422(api: AsyncClient, world: dict, db: AsyncSession) -> None:
    """The doctor must hold the requested specialty, checked via the junction table."""
    other = await make_specialty(db, "Dermatology")
    await _login(api)
    resp = await api.post("/api/v1/appointments", json=_payload(world, specialty_id=other.id))
    assert resp.status_code == 422
    assert "specialty" in resp.json()["detail"]


async def test_cancel_frees_the_slot_for_rebooking(api: AsyncClient, world: dict) -> None:
    """Ties the API behaviour to the exclusion constraint's partial WHERE clause."""
    await _login(api)
    first = await api.post("/api/v1/appointments", json=_payload(world))
    assert first.status_code == 201
    appointment_id = first.json()["id"]

    cancelled = await api.post(f"/api/v1/appointments/{appointment_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    again = await api.post("/api/v1/appointments", json=_payload(world))
    assert again.status_code == 201, "the cancelled slot must be rebookable"


async def test_reschedule_moves_the_appointment(api: AsyncClient, world: dict) -> None:
    await _login(api)
    first = await api.post("/api/v1/appointments", json=_payload(world))
    appointment_id = first.json()["id"]

    resp = await api.post(
        f"/api/v1/appointments/{appointment_id}/reschedule",
        json={"appointment_date": BOOK_DATE.isoformat(), "start_time": "11:00:00"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["start_time"] == "11:00:00"
    assert resp.json()["doctor"]["id"] == world["doctor"].id


async def test_failed_reschedule_leaves_the_original_intact(api: AsyncClient, world: dict) -> None:
    """The cancel-then-rebook must be atomic.

    If the new time is invalid the whole transaction rolls back, so the patient
    keeps the slot they already had rather than losing it to a half-applied move.
    """
    await _login(api)
    first = await api.post("/api/v1/appointments", json=_payload(world))
    appointment_id = first.json()["id"]

    resp = await api.post(
        f"/api/v1/appointments/{appointment_id}/reschedule",
        json={"appointment_date": BOOK_DATE.isoformat(), "start_time": "16:00:00"},
    )
    assert resp.status_code == 422

    still_there = await api.get(f"/api/v1/appointments/{appointment_id}")
    assert still_there.status_code == 200
    assert still_there.json()["status"] == "scheduled"
    assert still_there.json()["start_time"] == "09:00:00"


async def test_slots_endpoint_reflects_bookings(api: AsyncClient, world: dict) -> None:
    """The calendar must agree with what booking will accept."""
    await _login(api)
    before = await api.get(
        f"/api/v1/appointments/slots?doctor_id={world['doctor'].id}&date={BOOK_DATE}"
    )
    assert before.status_code == 200
    free_before = sum(1 for s in before.json() if s["available"])

    await api.post("/api/v1/appointments", json=_payload(world))

    after = await api.get(
        f"/api/v1/appointments/slots?doctor_id={world['doctor'].id}&date={BOOK_DATE}"
    )
    free_after = sum(1 for s in after.json() if s["available"])
    # A 30-minute booking consumes two 15-minute slots.
    assert free_after == free_before - 2


async def test_patient_cannot_book_for_someone_else(
    api: AsyncClient, world: dict, db: AsyncSession
) -> None:
    other_user = await auth_service.register_user(
        db,
        email="otherpatient@example.com",
        password=PASSWORD,
        full_name="Other",
        role=UserRole.PATIENT,
    )
    other = Patient(
        first_name="Other",
        last_name="Person",
        date_of_birth=dt.date(1992, 2, 2),
        user_id=other_user.id,
    )
    db.add(other)
    await db.flush()

    await _login(api, "otherpatient@example.com")
    resp = await api.post("/api/v1/appointments", json=_payload(world))
    assert resp.status_code == 403
    assert "themselves" in resp.json()["detail"]


async def test_list_appointments_serialises_nested_objects(api: AsyncClient, world: dict) -> None:
    """The list route has the same lazy-load exposure as the detail route."""
    await _login(api)
    await api.post("/api/v1/appointments", json=_payload(world))

    resp = await api.get("/api/v1/appointments")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["doctor"]["last_name"] == world["doctor"].last_name
