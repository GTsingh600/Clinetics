"""Phase 0 smoke tests: the app boots, config loads, routes mount."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings, get_settings


async def test_health_returns_ok(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "Clinetics"


async def test_openapi_schema_is_generated(client: AsyncClient) -> None:
    """Catches route-registration and Pydantic model errors that only surface
    when FastAPI builds the schema."""
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    assert "/api/v1/health" in resp.json()["paths"]


def test_settings_are_cached() -> None:
    assert get_settings() is get_settings()


def test_cors_origins_accepts_comma_separated_string() -> None:
    s = Settings(cors_origins="http://a.test, http://b.test")  # type: ignore[arg-type]
    assert s.cors_origins == ["http://a.test", "http://b.test"]


def test_cors_origins_parses_from_environment(monkeypatch: object) -> None:
    """Regression test.

    The first version of Settings declared `cors_origins: list[str]` without
    `NoDecode`. Constructing `Settings(cors_origins=...)` directly worked, but
    reading `CORS_ORIGINS` from the environment blew up with a SettingsError,
    because pydantic-settings JSON-decodes complex types before validators run.
    Every `alembic` command failed as a result. This test exercises the env
    source specifically, which the init-argument test above cannot.
    """
    import os

    from app.core.config import Settings

    old = os.environ.get("CORS_ORIGINS")
    os.environ["CORS_ORIGINS"] = "http://localhost:3000,http://127.0.0.1:3000"
    try:
        s = Settings(_env_file=None)  # type: ignore[call-arg]
        assert s.cors_origins == ["http://localhost:3000", "http://127.0.0.1:3000"]
    finally:
        if old is None:
            os.environ.pop("CORS_ORIGINS", None)
        else:
            os.environ["CORS_ORIGINS"] = old
