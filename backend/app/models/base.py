"""Shared model conventions: primary keys, timestamps, naming.

Two things here matter beyond convenience:

1. **The naming convention** (defined in `app/core/db.py`). Without it PostgreSQL invents constraint names and
   Alembic emits migrations containing those generated names, which differ
   between databases. Every constraint then becomes awkward to drop or alter in
   a later migration. Fixing the naming scheme up front means
   `uq_appointment_doctor_id_...` is predictable and referenceable forever.

2. **Server-side timestamp defaults.** `server_default=func.now()` makes the
   *database* fill the value. A Python-side default would be wrong for rows
   created by a migration, by the data generator's bulk COPY, or by any SQL that
   does not go through the ORM.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


class TimestampMixin:
    """Adds `created_at` / `updated_at`, both maintained by the database."""

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        doc="Row creation time, set by the database.",
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        doc="Last modification time. Note: `onupdate` fires on ORM updates; bulk "
        "SQL updates bypass it, which is acceptable for an audit hint.",
    )
