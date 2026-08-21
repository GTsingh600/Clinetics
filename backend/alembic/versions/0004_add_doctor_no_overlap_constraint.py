"""Prevent overlapping appointments per doctor at the database level

Revision ID: 0004_no_overlap
Revises: 0003_analytics
Create Date: 2026-08-21

Makes a double-booked doctor structurally impossible.

This is the constraint the project's concurrency-safety claim rests on.
Checking for a clash in application code cannot be made correct: between the
SELECT that finds a slot free and the INSERT that claims it, a concurrent
transaction can claim the same slot. Both commit; the doctor is double-booked.
An exclusion constraint is evaluated by the index at write time, so there is
no such window regardless of isolation level or interleaving.

Two details carry real weight:

* `'[)'` bounds — half-open. Back-to-back appointments (10:00-10:30 and
  10:30-11:00) share the instant 10:30. With inclusive `'[]'` bounds they
  would be rejected as overlapping, which would make normal scheduling
  impossible. Half-open ranges are the correct model for time intervals.

* `WHERE (status <> 'cancelled')` — a partial constraint. Without it,
  cancelling an appointment would leave its row still blocking the slot, so
  the time could never be rebooked. Every other status did genuinely consume
  the slot, so only 'cancelled' is excused.

`appointment_date + start_time` is a `date + time -> timestamp` addition,
which is IMMUTABLE and therefore legal in an index expression. That is the
reason the schema stores local date and time separately rather than a single
timestamptz.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_no_overlap"
down_revision: str | None = "0003_analytics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE appointment
        ADD CONSTRAINT excl_appointment_doctor_no_overlap
        EXCLUDE USING gist (
            doctor_id WITH =,
            tsrange(
                appointment_date + start_time,
                appointment_date + end_time,
                '[)'
            ) WITH &&
        )
        WHERE (status <> 'cancelled')
        """)


def downgrade() -> None:
    op.execute(
        "ALTER TABLE appointment DROP CONSTRAINT IF EXISTS " "excl_appointment_doctor_no_overlap"
    )
