"""Aggregates every v1 router. Routes are registered here, never in main.py."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import appointments, auth, dashboard, directory, health, tasks

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(directory.router)
api_router.include_router(appointments.router)
api_router.include_router(dashboard.router)
api_router.include_router(tasks.router)
