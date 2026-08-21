"""Database engine, session factory, and the declarative Base.

Async SQLAlchemy 2.0. Two URLs exist on purpose:

* `database_url`      -> asyncpg   : used by the running application
* `database_url_sync` -> psycopg   : used by Alembic, which runs synchronously

`get_db` is the FastAPI dependency. It yields one session per request and
rolls back on any exception so a failed request can never leave a half-applied
transaction behind. Commits are the *service layer's* responsibility, not the
dependency's — that keeps a multi-step service call atomic.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# Deterministic constraint names. Without this PostgreSQL generates its own,
# they differ per database, and a later migration cannot reliably drop or alter
# a constraint by name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

engine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_pre_ping=True,  # transparently drops connections killed by the DB/proxy
    pool_size=10,
    max_overflow=20,
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # keep ORM objects usable after commit (needed for responses)
    autoflush=False,
)


class Base(DeclarativeBase):
    """Declarative base for every ORM model.

    Alembic autogenerate reads `Base.metadata`, so every model module must be
    imported by `app/models/__init__.py` or its table will be silently missing
    from generated migrations.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped session."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
