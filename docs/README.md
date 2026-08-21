# Clinetics — Learning Docs

Companion documentation for the build. Every phase gets a document that explains
**what was built, why it was built that way, and the general concepts behind it**,
so the repo doubles as a tutorial rather than just an artifact.

## Reading order

| Doc | Covers |
|---|---|
| [phase-0-scaffold.md](./phase-0-scaffold.md) | Repo layout, Docker Compose, Python packaging, FastAPI, SQLAlchemy, Alembic, Celery, Next.js App Router, CI |
| [phase-1-database.md](./phase-1-database.md) | Normalization, the deliberate denormalization, CHECK/EXCLUDE constraints, ON DELETE, native ENUMs, generated columns, index measurement, the PL/pgSQL trigger, schema separation, synthetic data and the validation gate |
| [glossary.md](./glossary.md) | Every term used in the project, defined in one line |
| _phase-2-core-app.md_ | (next) Auth, 3-role RBAC, CRUD, calendar UI, concurrency-safe booking |

## How the project fits together

```
                    ┌──────────────────────────────────────────┐
                    │  Next.js frontend (App Router, RSC)      │
                    │  dashboards · calendar · what-if diffs   │
                    └───────────────────┬──────────────────────┘
                                        │ HTTP / JSON
                    ┌───────────────────▼──────────────────────┐
                    │  FastAPI                                 │
                    │  routes → services → SQLAlchemy          │
                    └──┬──────────────┬───────────────┬────────┘
                       │              │               │
        ┌──────────────▼───┐   ┌──────▼───────┐   ┌───▼─────────────────┐
        │ PostgreSQL 16    │   │ Celery/Redis │   │ Agent (Claude)      │
        │ transactional +  │   │ async jobs   │   │ tool-calling loop   │
        │ analytical tables│   └──────┬───────┘   └───┬─────────────────┘
        └──────────────────┘          │               │ calls tools
                                      │               ▼
                            ┌─────────▼───────────────────────────┐
                            │ PURE PACKAGES (no web, no DB)       │
                            │  forecasting/  — XGBoost/LightGBM   │
                            │  optimizer/    — OR-Tools CP-SAT    │
                            └─────────────────────────────────────┘
```

**The load-bearing idea:** the LLM never decides anything. It picks which tools to
call and explains what came back. Schedules come from CP-SAT; predictions come
from trained models. That separation is what makes the system defensible.
