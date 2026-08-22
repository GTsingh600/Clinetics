"""Shared pytest fixtures.

Two families of test live here:

* **unit** — no database, no network. The ASGI `client` fixture exercises the
  real app in-process without binding a socket.
* **integration** — a real PostgreSQL 16. Not SQLite: this schema depends on
  `btree_gist` exclusion constraints, native ENUMs, generated columns, and a
  PL/pgSQL trigger, none of which SQLite has. Testing against a substitute
  would test a database we never ship.

**Isolation strategy.** The migrations run once per session against a dedicated
`*_test` database. Each test then runs inside a transaction that is rolled back
afterwards, so tests never see each other's rows and the suite needs no cleanup
code. This works precisely because the things under test — CHECK constraints,
the exclusion constraint, the trigger — are all evaluated inside the
transaction, so a rollback undoes them just like any other write.
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.main import create_app

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _to_test_db(url: str) -> str:
    """Point a database URL at the `<name>_test` database.

    Derived rather than configured so a developer cannot accidentally run the
    destructive integration suite against their working database by forgetting
    to set an environment variable.
    """
    base, _, name = url.rpartition("/")
    name = name.split("?", 1)[0]
    if name.endswith("_test"):
        return url
    return f"{base}/{name}_test"


TEST_DATABASE_URL = _to_test_db(settings.database_url)
TEST_DATABASE_URL_SYNC = _to_test_db(settings.database_url_sync)


# --------------------------------------------------------------------------
# Unit-test fixtures
# --------------------------------------------------------------------------
@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """HTTP client wired straight to the ASGI app (no network, no live server)."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --------------------------------------------------------------------------
# Integration-test fixtures
# --------------------------------------------------------------------------
def _ensure_test_database_exists() -> None:
    """Create the `*_test` database if it is not there yet.

    Removes a manual setup step from a clean checkout. Connects to the
    maintenance `postgres` database because you cannot CREATE DATABASE from
    inside the database you are creating, and uses AUTOCOMMIT because
    PostgreSQL forbids CREATE DATABASE inside a transaction block.
    """
    from sqlalchemy import create_engine as _create_engine

    target = TEST_DATABASE_URL_SYNC.rsplit("/", 1)[-1]
    admin_url = TEST_DATABASE_URL_SYNC.rsplit("/", 1)[0] + "/postgres"
    admin = _create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                sa_text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": target}
            ).scalar()
            if not exists:
                conn.execute(sa_text(f'CREATE DATABASE "{target}"'))
    finally:
        admin.dispose()


def _truncate_test_database() -> None:
    """Empty every table once, before the session starts.

    The suite is designed around per-test transactions that roll back, which
    makes it independent of *other tests* but not of data that was already
    COMMITTED into the database before it started. Anything left behind — most
    plausibly a `generate_data.py` run pointed at the test database while
    reproducing CI — then collides with fixtures that create their own clinic
    and specialties, and the failures look like unrelated test bugs.

    Truncating here makes the suite order-independent and self-healing. It is
    safe by construction: `TEST_DATABASE_URL_SYNC` is derived by appending
    `_test`, so this can never point at a working database.

    Tolerates a database that has no tables yet: on a fresh checkout this runs
    before the first migration.
    """
    from sqlalchemy import create_engine as _create_engine

    engine = _create_engine(TEST_DATABASE_URL_SYNC, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            tables = (
                conn.execute(
                    sa_text(
                        "SELECT quote_ident(schemaname) || '.' || quote_ident(tablename) "
                        "FROM pg_tables WHERE schemaname IN ('public', 'analytics') "
                        "AND tablename <> 'alembic_version'"
                    )
                )
                .scalars()
                .all()
            )
            if tables:
                conn.execute(sa_text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def _migrated_database() -> str:
    """Bring the test database to `head` exactly once per session.

    Alembic is invoked as a subprocess rather than through its Python API so the
    test exercises the same code path CI and a developer do — if the migrations
    are broken, this fails the same way `alembic upgrade head` would.
    """
    _ensure_test_database_exists()
    _truncate_test_database()
    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "DATABASE_URL_SYNC": TEST_DATABASE_URL_SYNC},
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            "alembic upgrade head failed against the test database.\n"
            f"Is PostgreSQL running (`docker compose up -d`) and does "
            f"{TEST_DATABASE_URL_SYNC.rsplit('/', 1)[-1]} exist?\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return TEST_DATABASE_URL


@pytest.fixture
async def db_engine(_migrated_database: str):
    """Function-scoped on purpose.

    asyncpg connections are bound to the event loop that created them, and
    pytest-asyncio gives each test its own loop. A session-scoped engine would
    hand a connection from a previous (closed) loop to the next test, which
    surfaces as "attached to a different loop" or "Event loop is closed".

    NullPool means no connection outlives the test. The cost is one connect per
    test — microseconds against a local socket, and worth it for an isolation
    property that cannot silently break.
    """
    engine = create_async_engine(_migrated_database, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_conn(db_engine) -> AsyncGenerator[AsyncConnection, None]:
    """A connection inside a transaction that is always rolled back.

    Yielding the raw connection (as well as the session below) lets constraint
    tests issue plain SQL, which is often clearer than going through the ORM
    when the thing under test is the DDL itself.
    """
    async with db_engine.connect() as conn:
        trans = await conn.begin()
        try:
            yield conn
        finally:
            await trans.rollback()


@pytest.fixture
async def db(db_conn: AsyncConnection) -> AsyncGenerator[AsyncSession, None]:
    """An ORM session bound to the rolled-back transaction.

    `join_transaction_mode="create_savepoint"` keeps the session's own
    commits from ending the outer transaction, so a test can call
    `await db.commit()` — which is necessary to fire deferred constraints and
    the AFTER trigger — while the outer rollback still discards everything.
    """
    async with AsyncSession(
        bind=db_conn,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    ) as session:
        yield session


# --------------------------------------------------------------------------
# Concurrency fixtures
# --------------------------------------------------------------------------
@pytest.fixture
async def committing_sessions(_migrated_database: str):
    """Independent sessions that really COMMIT, for concurrency tests.

    The `db` fixture above wraps everything in one transaction and rolls back,
    which is perfect for constraint tests and useless for race tests: two
    "transactions" sharing one connection cannot contend for a lock, and a
    rollback would hide the very commit whose visibility is under test.

    So this yields a factory producing sessions on separate connections, and
    truncates afterwards because there is no rollback to clean up.
    """
    engine = create_async_engine(_migrated_database, poolclass=NullPool)
    sessions: list[AsyncSession] = []

    def _make() -> AsyncSession:
        session = AsyncSession(bind=engine, expire_on_commit=False)
        sessions.append(session)
        return session

    try:
        yield _make
    finally:
        for session in sessions:
            await session.close()
        async with engine.begin() as conn:
            await conn.execute(
                sa_text(
                    "TRUNCATE analytics.doctor_utilization, appointment, availability, "
                    "doctor_specialty, room, doctor, patient, specialty, clinic, "
                    "refresh_token, user_account RESTART IDENTITY CASCADE"
                )
            )
        await engine.dispose()
