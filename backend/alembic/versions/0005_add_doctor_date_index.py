"""Add the (doctor_id, appointment_date) composite index

Revision ID: 0005_appt_index
Revises: 0004_no_overlap
Create Date: 2026-08-21

The index behind the calendar's hottest query.

Column order is not arbitrary. A btree can only range-scan on its trailing
column, after equality on the leading ones. The dominant query is
"appointments for this doctor between two dates", which is equality on
doctor_id and a range on appointment_date, so (doctor_id, appointment_date)
is the correct order. Reversed, the doctor filter could not be used as a
seek predicate.

Kept in its own revision so `scripts/explain_index.py` can measure the same
query with and without it and produce a reproducible before/after, rather
than a screenshot.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_appt_index"
down_revision: str | None = "0004_no_overlap"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_appointment_doctor_id_appointment_date",
        "appointment",
        ["doctor_id", "appointment_date"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_appointment_doctor_id_appointment_date", table_name="appointment")
