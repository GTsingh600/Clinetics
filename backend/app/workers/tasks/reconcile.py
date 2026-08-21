"""Nightly audit of the doctor_utilization trigger.

The Phase 1 trigger maintains `analytics.doctor_utilization` incrementally: each
write applies a delta rather than recounting the day. That is what makes it O(1)
per write, and it is also its weakness — incremental state can drift, and once
it does, nothing in the normal write path ever corrects it.

Causes of drift are real, not hypothetical: a migration that touches
`appointment` with the trigger disabled, a `TRUNCATE` (which fires no row-level
triggers at all), a bug in a future change to the trigger function, or a restore
from a backup taken mid-transaction.

So this task periodically recomputes the summary from scratch and compares. It
is the independent check on the trigger *in production*, where `test_trigger.py`
only checks it in CI.

`dry_run=True` by default: the task reports drift and does not silently rewrite
history. Auto-correcting by default would hide the underlying bug, which is the
thing actually worth knowing about.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import Task
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.workers.celery_app import celery_app

log = logging.getLogger(__name__)

# Recount straight from `appointment`, mirroring the definitions the trigger
# uses. If these two ever disagree in intent, the trigger is the bug.
RECOUNT_SQL = """
WITH recount AS (
    SELECT
        doctor_id,
        appointment_date AS utilization_date,
        COUNT(*) FILTER (WHERE status = 'scheduled') AS scheduled_count,
        COUNT(*) FILTER (WHERE status = 'completed') AS completed_count,
        COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled_count,
        COUNT(*) FILTER (WHERE status = 'no_show')   AS no_show_count,
        COALESCE(SUM(duration_minutes) FILTER (WHERE status <> 'cancelled'), 0) AS booked_minutes
    FROM appointment
    WHERE appointment_date >= CURRENT_DATE - make_interval(days => :days)
    GROUP BY doctor_id, appointment_date
)
SELECT
    COALESCE(r.doctor_id, u.doctor_id)               AS doctor_id,
    COALESCE(r.utilization_date, u.utilization_date) AS utilization_date,
    COALESCE(r.scheduled_count, 0)  AS expected_scheduled,
    COALESCE(u.scheduled_count, 0)  AS actual_scheduled,
    COALESCE(r.completed_count, 0)  AS expected_completed,
    COALESCE(u.completed_count, 0)  AS actual_completed,
    COALESCE(r.cancelled_count, 0)  AS expected_cancelled,
    COALESCE(u.cancelled_count, 0)  AS actual_cancelled,
    COALESCE(r.no_show_count, 0)    AS expected_no_show,
    COALESCE(u.no_show_count, 0)    AS actual_no_show,
    COALESCE(r.booked_minutes, 0)   AS expected_minutes,
    COALESCE(u.booked_minutes, 0)   AS actual_minutes
-- FULL OUTER JOIN, not LEFT: it must also catch summary rows that exist for a
-- (doctor, date) with no appointments at all, which a LEFT JOIN would miss.
FROM recount r
FULL OUTER JOIN analytics.doctor_utilization u
  ON u.doctor_id = r.doctor_id AND u.utilization_date = r.utilization_date
WHERE u.utilization_date IS NULL
   OR u.utilization_date >= CURRENT_DATE - make_interval(days => :days)
"""

REPAIR_SQL = """
INSERT INTO analytics.doctor_utilization AS du (
    doctor_id, utilization_date, scheduled_count, completed_count,
    cancelled_count, no_show_count, booked_minutes, created_at, updated_at
) VALUES (
    :doctor_id, :utilization_date, :scheduled_count, :completed_count,
    :cancelled_count, :no_show_count, :booked_minutes, now(), now()
)
ON CONFLICT (doctor_id, utilization_date) DO UPDATE SET
    scheduled_count = EXCLUDED.scheduled_count,
    completed_count = EXCLUDED.completed_count,
    cancelled_count = EXCLUDED.cancelled_count,
    no_show_count   = EXCLUDED.no_show_count,
    booked_minutes  = EXCLUDED.booked_minutes,
    updated_at      = now()
"""


@celery_app.task(name="clinetics.reconcile_utilization", bind=True)
def reconcile_utilization(self: Task, days: int = 30, dry_run: bool = True) -> dict[str, Any]:
    """Compare the trigger's incremental values against a full recount.

    Returns a summary the caller can poll for; drift rows are also logged at
    WARNING so they reach normal log aggregation without anyone watching a task
    result.

    Uses a synchronous engine: Celery workers here run a plain (non-async) pool,
    so opening an event loop per task would add machinery for no benefit.
    """
    engine = create_engine(settings.database_url_sync, future=True)
    drift: list[dict[str, Any]] = []

    try:
        with Session(engine) as session:
            rows = session.execute(text(RECOUNT_SQL), {"days": days}).mappings().all()
            checked = len(rows)

            for row in rows:
                mismatched = {
                    field: {"expected": row[f"expected_{field}"], "actual": row[f"actual_{field}"]}
                    for field in (
                        "scheduled",
                        "completed",
                        "cancelled",
                        "no_show",
                        "minutes",
                    )
                    if row[f"expected_{field}"] != row[f"actual_{field}"]
                }
                if not mismatched:
                    continue

                entry = {
                    "doctor_id": row["doctor_id"],
                    "date": str(row["utilization_date"]),
                    "fields": mismatched,
                }
                drift.append(entry)
                log.warning("doctor_utilization drift: %s", entry)

                if not dry_run:
                    session.execute(
                        text(REPAIR_SQL),
                        {
                            "doctor_id": row["doctor_id"],
                            "utilization_date": row["utilization_date"],
                            "scheduled_count": row["expected_scheduled"],
                            "completed_count": row["expected_completed"],
                            "cancelled_count": row["expected_cancelled"],
                            "no_show_count": row["expected_no_show"],
                            "booked_minutes": row["expected_minutes"],
                        },
                    )

            if not dry_run and drift:
                session.commit()
    finally:
        engine.dispose()

    result = {
        "checked_cells": checked,
        "drift_count": len(drift),
        "repaired": (not dry_run) and bool(drift),
        "drift": drift[:50],  # cap the payload; the full set is in the logs
        "days": days,
    }
    log.info(
        "reconcile_utilization: %d cells checked, %d drifted, repaired=%s",
        checked,
        len(drift),
        result["repaired"],
    )
    return result
