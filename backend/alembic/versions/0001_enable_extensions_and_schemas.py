"""Enable required extensions and create the analytics schema

Revision ID: 0001_extensions
Revises: <base>
Create Date: 2026-08-21

Prerequisite DDL that every later migration depends on.

`btree_gist` is what makes the doctor no-overlap constraint possible. A GiST
index natively supports overlap (`&&`) on ranges but not equality on a plain
integer; btree_gist adds the missing operator classes so a single exclusion
constraint can combine `doctor_id WITH =` and `range WITH &&`. Without it,
migration 0004 fails with "data type integer has no default operator class
for access method gist".

The `analytics` schema holds derived output (forecasts, generated schedules,
utilisation) and is kept structurally separate from the transactional core in
`public`, so the boundary is visible in the database itself and can be granted
or truncated independently.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_extensions"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("CREATE SCHEMA IF NOT EXISTS analytics")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS analytics CASCADE")
    # btree_gist is intentionally NOT dropped: other databases on the same
    # cluster may rely on it, and dropping an extension is not this
    # migration's business to undo.
