"""Shared pytest fixtures.

Phase 0 keeps this minimal: an ASGI-transport client that exercises the real app
in-process without binding a socket. Database fixtures arrive with the models in
Phase 1.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """HTTP client wired straight to the ASGI app (no network, no live server)."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
