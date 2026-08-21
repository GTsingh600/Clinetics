"""Aggregates every v1 router. Routes are registered here, never in main.py."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import health

api_router = APIRouter()
api_router.include_router(health.router)
