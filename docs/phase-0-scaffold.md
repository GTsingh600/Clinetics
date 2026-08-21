# Phase 0 — Scaffold

> **Goal of this phase:** produce an empty-but-correct skeleton where every layer
> is wired, verified, and enforced by automation — before a single feature exists.
>
> **Why bother?** Because every later phase inherits these decisions. Fixing a bad
> migration setup in Phase 4 means rewriting migrations. Getting the async DB
> session wrong makes every concurrency test in Phase 2 meaningless. Phase 0 is
> cheap; Phase 0 mistakes are not.

---

## Table of contents

1. [The shape of the repo](#1-the-shape-of-the-repo)
2. [Containers: Docker and Docker Compose](#2-containers-docker-and-docker-compose)
3. [Python packaging: pyproject, uv, virtual environments](#3-python-packaging-pyproject-uv-virtual-environments)
4. [Configuration and the 12-factor rule](#4-configuration-and-the-12-factor-rule)
5. [FastAPI: ASGI, app factory, dependency injection](#5-fastapi-asgi-app-factory-dependency-injection)
6. [SQLAlchemy 2.0: engines, pools, sessions](#6-sqlalchemy-20-engines-pools-sessions)
7. [Alembic: migrations as a version-controlled schema](#7-alembic-migrations-as-a-version-controlled-schema)
8. [Celery and Redis: work that outlives a request](#8-celery-and-redis-work-that-outlives-a-request)
9. [Next.js App Router: server vs client components](#9-nextjs-app-router-server-vs-client-components)
10. [TanStack Query: server state is not UI state](#10-tanstack-query-server-state-is-not-ui-state)
11. [Architecture fitness functions](#11-architecture-fitness-functions)
12. [CI: what each gate actually buys you](#12-ci-what-each-gate-actually-buys-you)
13. [A real bug we hit, and what it teaches](#13-a-real-bug-we-hit-and-what-it-teaches)
14. [Verification log](#14-verification-log)
15. [Commands cheat-sheet](#15-commands-cheat-sheet)

---

## 1. The shape of the repo

```
HospitalScheduler/
├─ docker-compose.yml     # local infrastructure (Postgres, Redis)
├─ Makefile               # one-word commands for every routine task
├─ .env.example           # every env var, documented, no real secrets
├─ .github/workflows/     # CI
├─ docs/                  # these documents
├─ backend/
│  ├─ app/                # WEB LAYER — may import FastAPI + SQLAlchemy
│  │  ├─ main.py          #   app factory only, no routes, no logic
│  │  ├─ core/            #   config, database engine, security primitives
│  │  ├─ models/          #   SQLAlchemy ORM tables
│  │  ├─ schemas/         #   Pydantic request/response models
│  │  ├─ services/        #   ALL business logic lives here
│  │  ├─ api/v1/          #   thin HTTP handlers that call services
│  │  ├─ agent/           #   LLM tool-calling loop (Phase 5)
│  │  └─ workers/         #   Celery app + background tasks
│  ├─ forecasting/        # PURE — no web, no DB imports (Phase 3)
│  ├─ optimizer/          # PURE — no web, no DB imports (Phase 4)
│  ├─ alembic/            # migration history
│  ├─ scripts/            # data generation, benchmarks, eval, checks
│  └─ tests/
└─ frontend/
   └─ src/{app,components,lib,types}/
```

### Concept: layering

A **layered architecture** means each layer may only talk to the one below it.

```
HTTP request → api/ (parse & validate) → services/ (decide) → models/ (persist)
```

Why it matters here:

- **Route handlers stay trivial.** A handler containing an `if` about business
  rules is a handler you cannot reuse from a Celery task or an agent tool. In this
  project the *same* "book an appointment" logic must be callable from an HTTP
  route, a background job, and an agent tool. Only a service layer allows that.
- **Testing gets cheap.** Services are plain Python functions; testing them needs
  no HTTP client and no running server.

### Concept: the purity rule

`forecasting/` and `optimizer/` sit **outside** `app/` and are forbidden from
importing `fastapi`, `sqlalchemy`, `celery`, or `app.*`.

Think of them as mathematical functions:

```
optimize(appointments, doctors, rooms, constraints) -> Schedule
```

Data in, result out, no I/O. Three concrete payoffs:

1. **Unit tests run in milliseconds** — no database to spin up.
2. **The benchmark stays honest.** To claim "CP-SAT beats greedy by X%", both must
   run on identical inputs. If the optimizer fetched its own data, run-to-run
   variation would contaminate the comparison.
3. **Reusability.** The same module runs in a notebook, a script, a Celery task, or CI.

This rule is *enforced*, not merely documented — see [§11](#11-architecture-fitness-functions).

---

## 2. Containers: Docker and Docker Compose

### Concept: image vs container

- An **image** is a read-only filesystem snapshot plus metadata — a *class*.
- A **container** is a running instance of an image — an *object*.

`postgres:16-alpine` is an image. `clinetics-db` is a container made from it.
"Alpine" means it is built on Alpine Linux — a minimal distro, so the image is
~80MB instead of ~400MB.

### Concept: why containerize the database at all

Without Docker you install Postgres on your OS, and now your machine, a teammate's
machine, and CI all run subtly different versions with different extensions
available. Container images pin that exactly: **CI and your laptop run
byte-identical Postgres 16.15.**

This matters *more* than usual here, because the schema depends on Postgres-specific
features no other database emulates:

- `EXCLUDE` constraints with `btree_gist` (prevents double-booking a doctor **in
  the database**, not in application code)
- PL/pgSQL triggers
- range types

Testing against SQLite would mean testing a database you never ship.

### Concept: Docker Compose

Compose describes a multi-container setup declaratively in one YAML file, so
`docker compose up -d` replaces a page of `docker run` flags. Key pieces used here:

**Volumes — persistence.** Container filesystems die with the container. A *named
volume* is storage that outlives it:

```yaml
volumes:
  - pgdata:/var/lib/postgresql/data
```

`docker compose down` keeps your data; `docker compose down -v` deletes it (that is
what `make nuke` does when you want a clean slate).

**Healthchecks — readiness, not just liveness.** A container can be *running* while
the service inside is still starting. Our db healthcheck runs a real query:

```yaml
test: ["CMD-SHELL", "pg_isready ... && psql ... -c 'SELECT 1' -q"]
```

`pg_isready` alone returns success during first-boot `initdb`, before the database
accepts real connections — a classic source of flaky startup. Combined with:

```yaml
depends_on:
  db: {condition: service_healthy}
```

the API container waits for a *genuinely usable* database, not merely a started one.

**Networking.** Compose puts services on a shared network where **the service name
is the hostname**. Inside the network the database is `db:5432`; from your host
machine it is `localhost:5432` via the published port. That is why the compose file
overrides `DATABASE_URL` for the `api` service — the correct URL differs depending
on which side of the network boundary you are on.

**Profiles.** Services tagged `profiles: ["app"]` do not start by default:

```bash
docker compose up -d               # Postgres + Redis only
docker compose --profile app up    # + API, worker, frontend
```

Rationale: day-to-day you want the API on your host with hot reload (edit a file,
uvicorn restarts in about a second) while only stateful services live in containers.
The `app` profile exists so CI and a demo machine can start everything in one command.

### Concept: multi-stage Docker builds

`backend/Dockerfile` and `frontend/Dockerfile` both use multiple `FROM` stages.
Build tools (compilers, dev dependencies) are needed to *build* but not to *run*.
A multi-stage build compiles in one stage and copies only the artifacts into a clean
final stage — smaller images and a smaller attack surface.

Also note the layer-caching trick:

```dockerfile
COPY pyproject.toml ./     # dependency manifest first
RUN uv pip install --system ".[ml]"
COPY . .                   # source code last
```

Docker caches each layer. Because source is copied *after* dependencies are
installed, editing a `.py` file does not invalidate the dependency layer, and
rebuilds take seconds instead of minutes.

---

## 3. Python packaging: pyproject, uv, virtual environments

### Concept: virtual environments

A venv is an isolated Python installation. Without one, `pip install` writes into
your system Python and Project A's `numpy 1.x` fights Project B's `numpy 2.x`.
Ours lives at `backend/.venv/` and is gitignored — it is *derived*, fully
reconstructible from `pyproject.toml`.

### Concept: `pyproject.toml`

The modern standard (PEP 621) for describing a Python project. One file replaces
`setup.py`, `requirements.txt`, `setup.cfg`, `.flake8`, `pytest.ini`, and
`mypy.ini`. Ours holds metadata, dependencies, **and** configuration for black,
ruff, mypy, and pytest.

### Concept: dependency groups

Three tiers, deliberately:

| Tier | Contains | Why separate |
|---|---|---|
| `dependencies` | FastAPI, SQLAlchemy, Celery, anthropic | needed to *run* the API |
| `[project.optional-dependencies].ml` | numpy, pandas, XGBoost, LightGBM, OR-Tools, matplotlib | huge (hundreds of MB); a lint job does not need them |
| `[dependency-groups].dev` | pytest, ruff, black, mypy, locust | never ship to production |

Installing: `uv pip install -e ".[ml]" --group dev`.

### Concept: editable install (`-e`)

A normal install *copies* your code into `site-packages`, so edits do not take
effect until you reinstall. `-e` installs a link to your source tree instead:
`import app.main` resolves to your working files and edits are live. Standard for
the project you are actively developing.

### Concept: why `uv`

`uv` is a Rust-based replacement for pip/venv — typically 10–100× faster, with a
real resolver.

### Concept: the lockfile

`pyproject.toml` states *constraints* (`fastapi>=0.115`). `uv.lock` records the
*exact* version of all 131 resolved packages, transitive dependencies included.

Why it matters: without a lock, `>=0.115` means CI installs whatever is newest on
the day it runs. A dependency ships a regression and your build breaks with no
change on your side — and worse, your laptop and production silently run different
code. `uv sync --frozen` installs exactly what the lock pins and *fails* if the
lock is stale relative to `pyproject.toml`.

The lock is committed and used in all three places — local, CI, and the Docker
image — so all three are provably identical. Change a dependency → run
`uv lock` (`make lock`) → commit the updated lock.

### The build-system problem we hit

The first `uv pip install -e .` failed. Cause: we use a **flat layout** with three
top-level packages (`app/`, `forecasting/`, `optimizer/`), and setuptools'
auto-discovery refuses to guess when several candidates exist. Fixed by declaring
them:

```toml
[tool.setuptools.packages.find]
include = ["app*", "forecasting*", "optimizer*"]
```

The `*` suffix matters — it includes subpackages like `app.core`, not just `app`.

---

## 4. Configuration and the 12-factor rule

**Rule: configuration lives in the environment, never in code.** Same image,
different env vars → dev, staging, production. Nothing secret is ever committed.

`app/core/config.py` implements this with `pydantic-settings`:

```python
class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://..."
    access_token_expire_minutes: int = 60
```

Three things happen for free:

1. **Type coercion.** Env vars are always strings; `ACCESS_TOKEN_EXPIRE_MINUTES=60`
   becomes `int` 60.
2. **Validation at startup.** A malformed value crashes the process on boot rather
   than surfacing as a mystery 500 on the first request that touches it.
   *Fail fast, fail loud.*
3. **One source of truth.** No `os.environ.get(...)` scattered through the codebase.

`@lru_cache` on `get_settings()` means the `.env` file is parsed once per process.

### `.env` vs `.env.example`

- `.env` — real values, **gitignored**. This repo is public; a leaked key is a real key.
- `.env.example` — every variable with a safe placeholder, **committed**. It is
  documentation: a new contributor runs `cp .env.example .env` and knows exactly
  what to fill in.

---

## 5. FastAPI: ASGI, app factory, dependency injection

### Concept: WSGI vs ASGI

Older Python web apps (Flask, Django pre-3.0) speak **WSGI**: one request occupies
one worker thread from start to finish. When a handler waits on the database, that
thread is blocked and idle.

**ASGI** is the async successor. On `await db.execute(...)`, the event loop suspends
that coroutine and runs another request's code on the same thread. For an I/O-bound
API — which is nearly all of them — this raises concurrent throughput enormously at
the same memory cost.

That is why the whole stack is async: `asyncpg`, `AsyncSession`, `async def`
handlers. Mixing a *blocking* call into an async handler is the classic mistake — it
stalls the entire event loop, not just that one request.

### Concept: the application factory

`create_app()` builds and returns the app rather than configuring a module-level
global. This lets each test construct a fresh, independently-configured instance,
so no state leaks between tests.

### Concept: lifespan

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    ...                      # startup
    yield
    await engine.dispose()   # shutdown
```

Everything before `yield` runs once at startup, everything after at shutdown.
Disposing the engine returns pooled connections cleanly instead of leaving the
database to time them out.

### Concept: dependency injection

```python
async def readiness(db: AsyncSession = Depends(get_db)):
```

The handler *declares what it needs*; FastAPI supplies it. `get_db` is a generator
dependency — code before `yield` runs before the handler, code after `yield` runs
after, guaranteed even on exception. That gives correct per-request resource
lifecycle without a single `try/finally` in the handler.

Crucially, dependencies are **overridable in tests**
(`app.dependency_overrides[get_db] = fake`), which is how Phase 2 will test RBAC
without minting real JWTs.

### Concept: liveness vs readiness

Two endpoints answering two different questions:

| Endpoint | Question | Touches DB? | If it fails |
|---|---|---|---|
| `/health` | Is this process alive? | No | orchestrator restarts the container |
| `/health/ready` | Can it serve traffic? | Yes | load balancer stops routing to it |

Conflating them is a real outage pattern: if liveness checked the database, a
30-second Postgres blip would cause Kubernetes to kill and restart every API pod,
turning a brief dependency hiccup into a full outage. Note also that readiness
*reports* failure as a 503 body; it never raises.

### Concept: OpenAPI

FastAPI derives a machine-readable API spec from your type hints and Pydantic
models. You get free interactive docs at `/docs`, and a schema the frontend can
generate types from. Disabled in production, where it would advertise your entire
API surface.

Our test `test_openapi_schema_is_generated` exists because building the schema
forces FastAPI to walk every route and model — it catches registration and Pydantic
errors that would otherwise only appear when one specific endpoint is hit.

---

## 6. SQLAlchemy 2.0: engines, pools, sessions

### Concept: ORM

An **Object-Relational Mapper** maps database rows to Python objects. You write
`doctor.appointments` instead of a JOIN. The tradeoff is that it can hide expensive
queries behind ordinary attribute access — which is why Phase 1 includes
`EXPLAIN ANALYZE` work: you must still know what SQL you actually generate.

### Concept: the connection pool

Opening a TCP connection to Postgres and authenticating costs milliseconds — a
disaster per-request. A **pool** keeps connections open and lends them out:

```python
pool_size=10,        # steady-state connections
max_overflow=20,     # burst capacity, closed when idle
pool_pre_ping=True,  # test a connection before lending it
```

`pool_pre_ping` sends a trivial query before handing over a connection. Without it,
a connection silently killed by a firewall, a proxy idle-timeout, or a database
restart gets handed to a request and fails. It costs one round-trip and removes an
entire class of intermittent production errors.

### Concept: the session and the unit of work

A `Session` is a **unit of work**: it tracks the objects you changed and flushes
them as one transaction on `commit()`. `get_db` yields one session per request and
rolls back on exception.

Two deliberate settings:

- **`expire_on_commit=False`.** By default SQLAlchemy expires objects after commit,
  so the next attribute access re-queries the database. In async code that re-query
  happens *outside* the awaited context and raises. Disabling it keeps committed
  objects usable for building the response.
- **Committing is the service layer's job, not the dependency's.** If `get_db`
  auto-committed on success, a service performing three writes could not make them
  atomic. Ownership of the transaction boundary belongs to whoever knows the
  business operation. This becomes critical in Phase 2's concurrency-safe booking.

### Concept: two drivers, one database

```
DATABASE_URL       = postgresql+asyncpg://...   # the app (async)
DATABASE_URL_SYNC  = postgresql+psycopg://...   # Alembic (sync)
```

Same database, different client libraries. Alembic's runner is synchronous; forcing
an async driver through it means bolting on an event loop for zero benefit.

---

## 7. Alembic: migrations as a version-controlled schema

### Concept: why migrations exist

Your ORM models describe the schema you *want*. The database has the schema it
*has*. A **migration** is a versioned, reviewable script that moves one to the
other — `git` for your database.

The naive alternative, `Base.metadata.create_all()`, creates missing tables but
**cannot alter existing ones**, so it is useless the moment you have real data.

### Concept: the revision graph

Each migration records `revision` (its id) and `down_revision` (its parent), forming
a linked list:

```
None ──► a1b2 (create tables) ──► c3d4 (add index) ──► e5f6 (add trigger)
```

Alembic stores the current position in an `alembic_version` table inside your
database, so it always knows which migrations still need to run.

**The unbreakable rule: never edit a migration that has already been applied
anywhere.** Your database records that it ran revision `c3d4`; if you change what
`c3d4` does, your schema and your history disagree permanently. Fix forward — write
a new migration.

### Concept: autogenerate and its limits

`alembic revision --autogenerate` diffs `Base.metadata` against the live database
and writes the difference. It reliably detects tables, columns, indexes, and simple
constraints. It **does not** detect triggers, PL/pgSQL functions, `EXCLUDE`
constraints, or extensions — all of which Phase 1 needs. Those get hand-written
`op.execute("...")` calls. Autogenerate is a first draft you always review.

We enabled two non-default flags in `env.py`:

```python
compare_type=True,             # detect a column's type changing
compare_server_default=True,   # detect a DEFAULT changing
```

Both are off by default and both cause silently missed migrations.

### Concept: the side-effect import

```python
import app.models  # noqa: F401
```

Autogenerate can only see tables registered on `Base.metadata`, and a model
registers itself when its module is *imported*. A model file nobody imports is
invisible — its table silently never appears in a migration. Hence the rule:
**every model module must be imported in `app/models/__init__.py`.**

### Concept: transactional DDL

Postgres can roll back schema changes. If step 3 of a 5-step migration fails, the
whole migration rolls back and you keep a consistent schema. (MySQL cannot do this —
a failed migration leaves you half-migrated, to fix by hand.) One more reason this
project is Postgres-specific on purpose.

### Concept: `alembic check` in CI

`alembic check` fails if the models differ from what the migrations produce — i.e.
somebody edited a model and forgot to generate a migration. Without this gate,
"works on my machine, fails on deploy" is inevitable.

---

## 8. Celery and Redis: work that outlives a request

### Concept: why background jobs

An HTTP request should finish in milliseconds. A CP-SAT solve over a full clinic
week can take 30+ seconds. Holding a connection open that long means browser
timeouts, blocked workers, and no way to report progress.

The pattern:

```
POST /schedules/optimize  ──► enqueue task, return 202 + task_id   (instant)
worker picks up task      ──► solves, writes result to Postgres
GET /tasks/{id}           ──► poll status: PENDING / STARTED / SUCCESS
```

### Concept: broker vs result backend

- **Broker** (`redis://.../1`) — the queue. Producers push, workers pop.
- **Result backend** (`redis://.../2`) — where return values are stored for later retrieval.

Separate Redis *databases* (`/1`, `/2`) so a `FLUSHDB` on one never wipes the other.

### Configuration worth understanding

```python
task_time_limit=600         # hard kill after 10 min
task_soft_time_limit=540    # raise a catchable exception at 9 min
worker_prefetch_multiplier=1
```

The soft limit lets a task save partial work before the hard kill.

`prefetch_multiplier=1` matters for uneven workloads: by default a worker grabs a
*batch* of tasks up front, so a worker that happens to receive four 30-second solves
sits on them while another worker idles. Setting it to 1 means "take one task,
finish it, then take another" — fair dispatch.

`--pool=solo` on Windows: Celery's default `prefork` pool relies on `fork()`, which
Windows does not have.

---

## 9. Next.js App Router: server vs client components

### Concept: React Server Components (RSC)

The single biggest idea in modern Next.js. In the App Router, **components are
server components by default**. They render on the server; their JavaScript is never
sent to the browser.

```tsx
// Server component — this can await a database or an API directly
export default async function SystemStatus() {
  const result = await probe();
  return <div>{result.health.service}</div>;
}
```

Why this is a big deal:

- **Less JavaScript.** Rendering-only code ships zero bytes to the client.
- **No client-side fetch waterfall.** The server fetches and sends finished HTML.
- **Secrets stay server-side.** A server component can hold an API key safely.

A component becomes a **client component** only when it opts in with `"use client"`,
which it must do to use `useState`, `useEffect`, event handlers, or browser APIs.

Project convention: **server components by default, client components only where
interactivity genuinely requires it.**

### Concept: why `Providers` must be a client component

`src/lib/providers.tsx` starts with `"use client"` because TanStack Query holds
mutable cache state in React context — impossible on the server. Note how it is
constructed:

```tsx
const [queryClient] = useState(() => new QueryClient({ ... }));
```

Creating the client inside `useState` rather than at module scope is a **security**
decision, not a style one. A module-level client on a server rendering many users'
requests would be shared — one user's cached data could leak into another user's
response. `useState` guarantees one client per session.

### Concept: static vs dynamic rendering

Next.js prerenders pages at build time when it can. We initially got:

```
○ /   (Static)  prerendered as static content
```

which would have **frozen the backend health status at build time** — the page would
forever show whatever the API said during `npm run build`. Fixed with:

```tsx
export const dynamic = "force-dynamic";
```

plus `cache: "no-store"` in the fetch wrapper. Now:

```
ƒ /   (Dynamic)  server-rendered on demand
```

General rule: static for content, dynamic for live operational data. Getting this
wrong produces confidently-displayed stale numbers — the worst failure mode for a
dashboard.

### Concept: the typed API client

`src/lib/api.ts` is the only module that knows the backend base URL and error shape.
Centralizing it means one place to add auth headers in Phase 2, one place to change
on deploy, and a custom `ApiError` carrying the status code so callers can
distinguish a 404 from a 500. No component ever hand-rolls a `fetch`.

`NEXT_PUBLIC_` prefix: Next.js only exposes env vars with that prefix to browser
code. Everything else stays server-only. Never prefix a secret.

---

## 10. TanStack Query: server state is not UI state

### Concept: the distinction

**UI state** — is the modal open? — is owned by your app; `useState` handles it.
**Server state** — the appointment list — is owned by the *server*. Your copy is a
cache that can go stale, needs refetching, and is shared across components.

Hand-rolling this with `useEffect` + `useState` means reimplementing caching,
deduplication, retries, loading/error states, and invalidation — badly — in every
component.

```tsx
staleTime: 60_000,          // forecasts do not change second-to-second
retry: 1,
refetchOnWindowFocus: false,
```

`staleTime` is how long a cached result counts as fresh. The default is 0 (refetch
on every mount). Sixty seconds fits this domain: a demand forecast is a batch
artifact, not a live ticker, and refetching it on every tab focus wastes an
expensive query.

---

## 11. Architecture fitness functions

### Concept

A **fitness function** is an automated test that verifies an *architectural*
property rather than a behavioural one. Rules that are only written down decay;
rules that fail CI do not.

`backend/scripts/check_purity.py` enforces the purity rule from §1. It parses each
module in `forecasting/` and `optimizer/` with Python's `ast` module and fails on a
forbidden import:

```python
FORBIDDEN_ROOTS = {"fastapi", "sqlalchemy", "alembic", "celery",
                   "app", "asyncpg", "psycopg", "redis", "anthropic"}
```

**Why `ast` and not a regex?** A regex matching `sqlalchemy` would also fire on the
word inside a docstring, a comment, or a string literal. `ast` parses the code into
a real syntax tree, so only genuine `Import` / `ImportFrom` nodes are inspected — no
false positives, and no sneaking past it with unusual formatting.

It runs in two places: as a CI step, and as a pytest test
(`tests/unit/test_architecture.py`), so a violation fails locally too.

---

## 12. CI: what each gate actually buys you

Every push runs three parallel jobs. Each gate exists because of a specific class of
bug:

| Gate | Catches |
|---|---|
| `ruff check` | unused imports, undefined names, bug-prone patterns, import order |
| `black --check` | formatting drift (ends all formatting debate; diffs stay meaningful) |
| `mypy` | type errors — passing `None` where a `str` is required, etc. |
| `check_purity.py` | architectural erosion of the pure packages |
| `alembic upgrade head` | a migration that does not apply to a clean database |
| `alembic check` | a model changed without a matching migration |
| `pytest --cov` | behavioural regressions |
| `tsc --noEmit` | frontend type errors |
| `next build` | build-time failures and accidental static/dynamic changes |
| `docker build` | a Dockerfile that broke — before you find out during a demo |

### Concept: service containers

```yaml
services:
  postgres:
    image: postgres:16-alpine
    options: --health-cmd "pg_isready ..." --health-interval 5s
```

GitHub Actions can start containers alongside your job. This is what lets CI test
against **real Postgres 16** rather than a stand-in. Given that this schema leans on
`btree_gist` and PL/pgSQL, testing on anything else would be testing a different
product.

### Concept: concurrency cancellation

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
```

Push three commits quickly and the first two runs are cancelled. Saves CI minutes
and gets you feedback on the commit you actually care about.

---

## 13. A real bug we hit, and what it teaches

Phase 0 was "finished" and tests passed. Then every Alembic command died:

```
pydantic_settings.exceptions.SettingsError: error parsing value for field
"cors_origins" from source "DotEnvSettingsSource"
```

**Root cause.** pydantic-settings treats `list[str]` as a *complex* type and tries
to `json.loads()` the raw environment value **before** any validator runs. Our
`.env` contained `CORS_ORIGINS=http://localhost:3000`, which is not valid JSON, so
it raised during settings construction — and since `alembic/env.py` imports
settings, every migration command failed at import time.

**Why the existing test missed it.** The test constructed `Settings(cors_origins=...)`
directly. That uses the *init* source, which never JSON-decodes. The dotenv and
environment sources take a different code path — the exact one that was broken.

**The fix.** Opt that field out of decoding so the raw string reaches the validator:

```python
from pydantic_settings import NoDecode

cors_origins: Annotated[list[str], NoDecode] = Field(...)
```

**Three transferable lessons:**

1. **Test the path production uses.** A test exercising a different code path than
   reality gives false confidence. We added
   `test_cors_origins_parses_from_environment`, which sets a real env var.
2. **Import-time failures have a wide blast radius.** Because config is imported by
   everything, one bad field broke a tool that has nothing to do with CORS.
3. **This is exactly what Phase 0 is for.** Found in an empty scaffold it cost ten
   minutes. Found in Phase 4 on top of a stack of migrations, it costs an afternoon.

### Two more bugs that only CI could find

Everything passed locally, then the first CI run failed twice over. Both failures
share one root cause worth internalizing: **your machine is dirty and CI is clean.**
Your working directory accumulates generated files that a fresh checkout does not have.

**Failure 1 — `Cannot find name 'LayoutProps'`.** Next.js 16 *generates* global
types (`LayoutProps`, `PageProps`) into `.next/types` during a build. That directory
is gitignored, so on a clean checkout `tsc --noEmit` ran before anything generated
them. It passed locally purely because we had already run `npm run build`.

Reproduced it with `rm -rf .next && npx tsc --noEmit`, then fixed it by running the
sanctioned generator first:

```yaml
- name: Generate Next.js route types
  run: npx next typegen
- name: TypeScript
  run: npx tsc --noEmit
```

**Failure 2 — `No file matched [**/uv.lock]`.** The `setup-uv` cache step needs a
lockfile, and we had none because `uv pip install` does not write one.

The lazy fix was to point the cache glob at `pyproject.toml`. We took the better
one: generate a real `uv.lock`, commit it, and switch CI and Docker to
`uv sync --frozen`. A CI complaint surfaced a genuine reproducibility gap — worth
fixing properly rather than silencing.

**The general lesson:** a green local run proves your code works *given your
machine's accumulated state*. Only a clean checkout proves it works at all. If you
want to check something before pushing, delete the generated directories first —
`.next/`, `.venv/`, `__pycache__/` — and try again.

---

## 14. Verification log

Phase 0 is not "done" because files exist. Every claim below was executed:

| Check | Result |
|---|---|
| `docker compose config` | valid |
| `docker compose up -d` | `db:healthy redis:healthy` |
| Postgres version | **16.15** |
| `btree_gist` available (needed for Phase 1 EXCLUDE) | **1.7** ✅ |
| `plpgsql` available (needed for Phase 1 trigger) | present ✅ |
| `redis-cli ping` | `PONG` |
| `alembic upgrade head` | applied, no errors |
| `alembic check` | `No new upgrade operations detected.` |
| `pytest` | **6 passed** |
| `ruff check .` | `All checks passed!` |
| `black --check .` | clean |
| `mypy app forecasting optimizer scripts` | `Success: no issues found in 18 source files` |
| `check_purity.py` | `purity OK` |
| `GET /api/v1/health` | `{"status":"ok","service":"Clinetics","environment":"local"}` |
| `GET /api/v1/health/ready` (real DB query) | `{"status":"ready","checks":{"database":"ok"}}` HTTP 200 |
| Celery round-trip through Redis | task dispatched → returned `pong` ✅ |
| `npx tsc --noEmit` | clean |
| `npm run lint` | clean |
| `npm run build` | success, `/` correctly **dynamic** |
| `npx next typegen` on a clean `.next/` | route types generated, `tsc` clean |
| `uv lock` | 131 packages pinned |
| `docker build ./backend` | image built from the lockfile |
| Container run + `GET /api/v1/health/ready` | reached Postgres, returned `ready` |
| Container `HEALTHCHECK` | reports **healthy** |
| GitHub Actions | see the badge/run list on the repo |

Confirming `btree_gist` and `plpgsql` *now* matters: both are hard requirements of
the Phase 1 schema. Discovering a missing extension while writing migrations would
be a much worse time to find out.

---

## 15. Commands cheat-sheet

```bash
# Infrastructure
docker compose up -d          # start Postgres + Redis
docker compose ps             # check health
docker compose down           # stop (keeps data)
docker compose down -v        # stop AND delete data

# Backend (from backend/)
.venv/Scripts/python -m uvicorn app.main:app --reload   # API at :8000, docs at /docs
.venv/Scripts/python -m pytest                          # tests
.venv/Scripts/python -m alembic upgrade head            # apply migrations
.venv/Scripts/python -m alembic revision --autogenerate -m "add doctor table"
.venv/Scripts/python -m black . && .venv/Scripts/python -m ruff check --fix .

# Frontend (from frontend/)
npm run dev        # :3000
npm run build
npx tsc --noEmit

# Or, with make (Git Bash / WSL)
make up  |  make api  |  make test  |  make lint  |  make check
```

---

## What Phase 1 builds on this

Now that the skeleton is verified, Phase 1 adds the database:

1. SQLAlchemy models — `Clinic`, `Doctor`, `Patient`, `Appointment`, `Availability`,
   `Specialty`, `Room`, plus the derived `Forecast` and `Schedule` tables
2. The initial migration
3. Constraint migrations — CHECKs, explicit `ON DELETE` behaviour, and the
   `btree_gist` `EXCLUDE` constraint that makes double-booking **impossible at the
   database level**
4. An index on `(doctor_id, appointment_date)`, with `EXPLAIN ANALYZE` before/after
5. A PL/pgSQL trigger maintaining a `doctor_utilization` summary table
6. The synthetic data generator, with deliberately learnable correlations
7. **The validation gate** — plots proving no-show rate really does correlate with
   lead time. If the correlation is not visible, the generator is wrong, and no ML
   work may proceed until it is fixed.
