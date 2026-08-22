# Clinetics — Learning Docs

Companion documentation for the build. Every phase gets a document that explains
**what was built, why it was built that way, and the general concepts behind it**,
so the repo doubles as a tutorial rather than just an artifact.

## Reading order

| Doc | Covers |
|---|---|
| [phase-0-scaffold.md](./phase-0-scaffold.md) | Repo layout, Docker Compose, Python packaging, FastAPI, SQLAlchemy, Alembic, Celery, Next.js App Router, CI |
| [phase-1-database.md](./phase-1-database.md) | Normalization, the deliberate denormalization, CHECK/EXCLUDE constraints, ON DELETE, native ENUMs, generated columns, index measurement, the PL/pgSQL trigger, schema separation, synthetic data and the validation gate |
| [phase-2-core-app.md](./phase-2-core-app.md) | Cookie vs bearer sessions, bcrypt's 72-byte limit, refresh rotation and reuse detection, CSRF, object-level authorization, the two booking races and their two different fixes, server components and cookies |
| [phase-3-forecasting.md](./phase-3-forecasting.md) | Prediction-time leakage, rolling-origin validation, the missing-zeros trap, baselines, operating points, calibration, and shipping the simpler model when it wins |
| [phase-4-optimizer.md](./phase-4-optimizer.md) | CP-SAT model, why breaks came free from the schema, the objective and every weight, overbooking vs the database constraint, the 0% result and what it meant, the load sweep, and the waiting-time regression the simulator caught |
| [design-system.md](./design-system.md) | Palette, typography, tokens, component patterns |
| [glossary.md](./glossary.md) | Every term used in the project, defined in one line |
| _phase-5-agent.md_ | (next) Tool schemas, tool-calling loop, grounded explanations |

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
