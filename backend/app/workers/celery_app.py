"""Celery application.

Phase 0 defines the wiring only; real tasks arrive in Phase 2 (async jobs) and
Phase 4 (long-running optimizer solves).
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "clinetics",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks.reconcile"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # Optimizer solves are long; a hard cap stops a pathological model from
    # pinning a worker forever.
    task_time_limit=600,
    task_soft_time_limit=540,
    worker_prefetch_multiplier=1,  # fair dispatch for uneven task durations
)


# Periodic schedule, run by `celery beat`. Nightly at 02:30 UTC: after the
# clinic day is over, so the recount is not racing live writes.
celery_app.conf.beat_schedule = {
    "reconcile-utilization-nightly": {
        "task": "clinetics.reconcile_utilization",
        "schedule": crontab(hour=2, minute=30),
        "kwargs": {"days": 30, "dry_run": True},
    },
}


@celery_app.task(name="clinetics.ping")
def ping() -> str:
    """Smoke-test task proving broker + worker round-trip works."""
    return "pong"
