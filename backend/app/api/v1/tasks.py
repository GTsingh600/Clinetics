"""Async job endpoints.

The pattern: enqueue and return 202 with a task id, then let the client poll.
An optimizer solve or a 30-day reconciliation can take far longer than a request
should hold a connection open.
"""

from __future__ import annotations

from typing import Any

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, Query, status

from app.api.v1.deps import require_admin
from app.workers.celery_app import celery_app
from app.workers.tasks.reconcile import reconcile_utilization

router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Depends(require_admin)])


@router.post("/reconcile-utilization", status_code=status.HTTP_202_ACCEPTED)
async def trigger_reconciliation(
    days: int = Query(default=30, ge=1, le=365),
    dry_run: bool = Query(
        default=True,
        description="Report drift without rewriting. Auto-repair hides the underlying bug.",
    ),
) -> dict[str, Any]:
    """Audit the utilization trigger against a full recount. Admin only."""
    async_result = reconcile_utilization.delay(days=days, dry_run=dry_run)
    return {"task_id": async_result.id, "status": "queued"}


@router.get("/{task_id}")
async def task_status(task_id: str) -> dict[str, Any]:
    """Poll a task.

    `result` is only populated once the task succeeded; a failed task exposes
    its state but not the exception text, which can contain internals.
    """
    result = AsyncResult(task_id, app=celery_app)
    payload: dict[str, Any] = {"task_id": task_id, "state": result.state}
    if result.successful():
        payload["result"] = result.result
    elif result.failed():
        payload["error"] = "task failed; see server logs"
    return payload
