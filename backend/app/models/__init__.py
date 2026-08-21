"""ORM model registry.

Every model module MUST be imported here. Alembic's autogenerate diffs
`Base.metadata` against the live database; a model that is never imported is
invisible to it and will be silently omitted from migrations.

Models arrive in Phase 1.
"""

from __future__ import annotations

from app.core.db import Base

__all__ = ["Base"]
