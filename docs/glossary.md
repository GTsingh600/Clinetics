# Glossary

Every term used in this project, defined in one or two lines. Skim it once; come
back when a word in a commit message or a doc does not land.

## Architecture

| Term | Meaning |
|---|---|
| **Layered architecture** | Each layer talks only to the one below: `api → services → models`. Keeps business logic reusable from HTTP, Celery, and agent tools alike. |
| **Service layer** | Where business logic lives. Plain Python functions — no HTTP objects, no framework types in the signature. |
| **Pure module** | A module with no I/O and no framework imports. Data in, result out. Here: `forecasting/` and `optimizer/`. |
| **Fitness function** | An automated test of an *architectural* property (e.g. "the optimizer imports no DB code") rather than of behaviour. |
| **Monorepo** | Backend and frontend in one repository, so a change touching both is one atomic commit and one CI run. |
| **Denormalization** | Deliberately duplicating data to make reads faster. This project has exactly one: the doctor's specialty stored on `Appointment` as of booking time. |

## Python & packaging

| Term | Meaning |
|---|---|
| **venv** | Isolated Python install so projects cannot break each other's dependencies. |
| **`pyproject.toml`** | PEP 621 standard project manifest — metadata, dependencies, and tool config in one file. |
| **Editable install (`pip install -e .`)** | Installs a link to your source tree instead of a copy, so edits take effect immediately. |
| **Extras (`.[ml]`)** | Optional dependency sets, installed on demand. Keeps the API image free of hundreds of MB of ML libraries. |
| **`uv`** | Rust-based, much faster drop-in replacement for pip and venv. |
| **`ruff`** | Extremely fast linter; replaces flake8, isort, pyupgrade, and more. |
| **`black`** | Opinionated auto-formatter. Ends formatting arguments and keeps diffs meaningful. |
| **`mypy`** | Static type checker. Catches type errors before runtime. |
| **`ast`** | Python's built-in parser. Turns source into a syntax tree — how `check_purity.py` inspects imports without regex false positives. |

## Web & API

| Term | Meaning |
|---|---|
| **WSGI** | Older sync Python web interface: one request blocks one thread end to end. |
| **ASGI** | Async successor to WSGI. While one request awaits I/O, the event loop runs another. |
| **uvicorn** | The ASGI server that actually runs FastAPI. |
| **Event loop** | The scheduler that interleaves coroutines on one thread. A blocking call inside it stalls *everything*. |
| **Application factory** | `create_app()` returning a fresh app instance, so tests get isolated instances. |
| **Lifespan** | Startup/shutdown hook. Used here to dispose the DB connection pool cleanly. |
| **Dependency injection** | Handlers declare what they need (`Depends(get_db)`); the framework supplies it and manages its lifecycle. |
| **Liveness probe** | "Is this process alive?" Must not touch dependencies, or a DB blip triggers pod restarts. |
| **Readiness probe** | "Can this process serve traffic?" May check dependencies; reports 503 rather than raising. |
| **OpenAPI** | Machine-readable API spec FastAPI generates from your type hints. Powers `/docs`. |
| **Pydantic** | Runtime data validation from type hints. v2 has a Rust core and is very fast. |
| **CORS** | Browser rule restricting cross-origin requests. The API must explicitly allow the frontend's origin. |
| **RBAC** | Role-Based Access Control. Here exactly three roles: `patient`, `doctor`, `admin`. |
| **JWT** | Signed token carrying identity claims, so the server needs no session store. |

## Database

| Term | Meaning |
|---|---|
| **ORM** | Maps rows to objects. Convenient, but can hide expensive queries behind attribute access. |
| **Engine** | SQLAlchemy's connection factory and pool owner. One per application. |
| **Connection pool** | Reuses open DB connections instead of paying handshake cost per request. |
| **`pool_pre_ping`** | Tests a pooled connection before lending it, so a silently-dropped connection never reaches a request. |
| **Session** | A *unit of work*: tracks changed objects, flushes them as one transaction on commit. |
| **`expire_on_commit=False`** | Keeps ORM objects readable after commit. Required in async code, where a lazy re-query would raise. |
| **Transaction** | All-or-nothing group of statements. |
| **Isolation level** | How much concurrent transactions can see of each other. Central to Phase 2's booking race. |
| **`SELECT ... FOR UPDATE`** | Row-level lock. Two concurrent bookings for the last slot serialize instead of both succeeding. |
| **Migration** | Versioned, reviewable script that changes the schema. `git` for your database. |
| **Revision graph** | Linked list of migrations via `down_revision`. Alembic tracks position in `alembic_version`. |
| **Autogenerate** | Alembic diffing models against the live DB. Catches tables/columns/indexes — **not** triggers, EXCLUDE constraints, or extensions. |
| **Transactional DDL** | Postgres can roll back schema changes; a failed migration leaves nothing half-applied. |
| **3NF** | Third normal form. Every non-key column depends on the key, the whole key, and nothing but the key. |
| **Junction table** | The extra table that implements a many-to-many relationship (here: `Doctor ↔ Specialty`). |
| **CHECK constraint** | Row-level rule the database enforces, e.g. `duration_minutes > 0`. |
| **EXCLUDE constraint** | Postgres constraint forbidding rows whose ranges overlap. Makes double-booking a doctor *impossible in the database*. |
| **`btree_gist`** | Postgres extension letting a GiST index mix scalar equality (`doctor_id`) with range overlap — required by that EXCLUDE constraint. |
| **`ON DELETE CASCADE / RESTRICT / SET NULL`** | What happens to child rows when a parent is deleted. Choosing deliberately per relationship is part of the design. |
| **Index** | Lookup structure that turns a table scan into a seek. Costs write speed and disk. |
| **`EXPLAIN ANALYZE`** | Shows the query plan and *actual* execution time. The evidence that an index did something. |
| **Trigger** | Function the database runs automatically on insert/update/delete. Used here to maintain `doctor_utilization`. |
| **PL/pgSQL** | Postgres's procedural language, used to write that trigger. |

## Async & infrastructure

| Term | Meaning |
|---|---|
| **Celery** | Distributed task queue for work too slow for a request cycle. |
| **Broker** | The queue itself (Redis DB 1). Producers push tasks, workers pop them. |
| **Result backend** | Where task return values are stored (Redis DB 2) for later polling. |
| **`prefetch_multiplier`** | How many tasks a worker reserves at once. `1` = fair dispatch for uneven task durations. |
| **Soft/hard time limit** | Soft raises a catchable exception (save partial work); hard kills the task. |
| **Docker image** | Read-only filesystem snapshot — the *class*. |
| **Container** | A running instance of an image — the *object*. |
| **Volume** | Storage that outlives a container, so your database survives a restart. |
| **Healthcheck** | Command Docker runs to decide whether a service is genuinely usable. |
| **Multi-stage build** | Build in one stage, copy only artifacts into a clean final stage. Smaller, safer images. |
| **Compose profile** | Tag that keeps a service out of the default `up`. Here: infra by default, app services opt-in. |
| **Service container (CI)** | A container GitHub Actions runs alongside your job — how CI tests against real Postgres. |

## Frontend

| Term | Meaning |
|---|---|
| **App Router** | Next.js routing model where the file tree defines routes and components are server-rendered by default. |
| **React Server Component** | Renders on the server; its JavaScript never reaches the browser. The default. |
| **Client component** | Opts in with `"use client"`. Needed for state, effects, event handlers, browser APIs. |
| **Hydration** | Attaching React event handlers to server-rendered HTML in the browser. |
| **Static rendering** | Page built once at build time. Wrong for live data — it freezes values into the bundle. |
| **Dynamic rendering** | Page rendered per request (`export const dynamic = "force-dynamic"`). Right for operational dashboards. |
| **`NEXT_PUBLIC_`** | Prefix that exposes an env var to browser code. Never use it for a secret. |
| **Server state** | Data owned by the server; your copy is a cache. What TanStack Query manages. |
| **UI state** | Data owned by your component (is the modal open?). What `useState` manages. |
| **`staleTime`** | How long a cached query result counts as fresh before a refetch is triggered. |
| **Query invalidation** | Marking cached data stale after a mutation so dependent views refetch. |

## ML & optimization

| Term | Meaning |
|---|---|
| **Feature engineering** | Turning raw rows into model inputs (lead time, hour of week, patient no-show history). |
| **Classifier** | Model predicting a category. Here: will this patient no-show? |
| **Precision** | Of the no-shows you predicted, how many actually were. |
| **Recall** | Of the actual no-shows, how many you caught. |
| **Confusion matrix** | The 2×2 table of true/false positives/negatives behind both numbers. |
| **MAE / RMSE** | Mean absolute / root-mean-square error. RMSE punishes large misses harder. |
| **Leakage** | Training on information unavailable at prediction time. Produces great metrics and a useless model. |
| **Constraint programming** | Declare variables, constraints, and an objective; the solver searches. |
| **CP-SAT** | Google OR-Tools' constraint solver. The scheduling engine here. |
| **Decision variable** | What the solver chooses — e.g. the start time of an appointment. |
| **Hard constraint** | Must hold. A solution violating it is invalid (no overlapping appointments). |
| **Soft constraint** | Preferred; violation costs objective points (minimize overtime). |
| **Objective function** | The weighted sum being minimized: wait time, idle time, overtime, urgency penalty. |
| **Feasible / infeasible** | Whether any assignment satisfies all hard constraints at all. |
| **Baseline** | The simple alternative you must beat. Here: greedy/FCFS. Without it, "optimal" is unfalsifiable. |

## Agent

| Term | Meaning |
|---|---|
| **Tool calling** | The model returns a structured request to run one of your functions, then reasons over the result. |
| **Tool schema** | JSON Schema describing a tool's name, purpose, and parameters — how the model knows what it can call. |
| **Multi-hop query** | A question needing several chained tool calls where later inputs depend on earlier outputs. |
| **Grounded explanation** | Reasoning traceable to specific tool outputs. The opposite of plausible-sounding invention. |
| **Agentic loop** | call model → run requested tool → feed result back → repeat until the model answers. |
