# Clinetics

[![CI](https://github.com/GTsingh600/Clinetics/actions/workflows/ci.yml/badge.svg)](https://github.com/GTsingh600/Clinetics/actions/workflows/ci.yml)

**AI-powered clinic operations.** Forecast appointment demand, generate optimal
schedules with constrained optimization, and expose both through a tool-using LLM
agent that answers natural-language operational questions and runs what-if
simulations.

```
Forecast  →  Optimize  →  Agent orchestrates & explains  →  Simulate "what if"
```

> **Design principle:** the LLM **never** makes scheduling decisions. It interprets
> intent, calls tools, and explains results. Every schedule comes from an OR-Tools
> CP-SAT model; every prediction comes from a trained ML model. That separation is
> what makes the system's outputs defensible.

> ⚠️ **All data in this project is synthetic.** No real patient data is used,
> stored, or derived from. The generator produces deliberately correlated
> distributions so the ML layer has genuinely learnable structure — see
> [docs/](./docs/). This is a portfolio project, not a medical device, and it makes
> no clinical claims.

---

## Status

| Phase | Scope | State |
|---|---|---|
| **0 — Scaffold** | Repo, Docker Compose, FastAPI + Next.js skeletons, Alembic, CI | ✅ **Done & verified** |
| 1 — Database | Models, constraints, EXCLUDE, trigger, synthetic data + validation gate | ⬜ Next |
| 2 — Core app | Auth + 3-role RBAC, CRUD, calendar UI, concurrency-safe booking | ⬜ |
| 3 — Forecasting | No-show classifier, demand & duration models, eval harness | ⬜ |
| 4 — Optimizer | CP-SAT model, greedy baseline, benchmark | ⬜ |
| 5 — Agent | Tool schemas, tool-calling loop, grounded explanations | ⬜ |
| 6 — Simulation | What-if endpoint, before/after diff UI | ⬜ |
| 7 — Rigor | Load test, concurrency writeup, demo fallback, full README | ⬜ |
| 8 — Deploy | Managed Postgres, backend host, Vercel frontend | ⬜ |

---

## Quick start

**Prerequisites:** Docker Desktop, Python 3.12, Node 22+.

```bash
git clone https://github.com/GTsingh600/Clinetics.git
cd Clinetics
cp .env.example .env          # fill in ANTHROPIC_API_KEY when you reach Phase 5

# 1. Infrastructure
docker compose up -d          # Postgres 16 + Redis 7
docker compose ps             # both should report "healthy"

# 2. Backend
cd backend
uv venv --python 3.12         # or: python -m venv .venv
uv pip install -e ".[ml]" --group dev
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python -m uvicorn app.main:app --reload
#   → http://localhost:8000/api/v1/health
#   → http://localhost:8000/docs

# 3. Frontend (new terminal)
cd frontend
npm install
npm run dev                   # → http://localhost:3000
```

On Linux/macOS/WSL replace `.venv/Scripts/python` with `.venv/bin/python`, or just
use `make` (`make up`, `make api`, `make web`, `make test`, `make check`).

---

## Architecture

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

`forecasting/` and `optimizer/` are forbidden from importing FastAPI, SQLAlchemy,
Celery, or `app.*`. This is enforced in CI by
[`backend/scripts/check_purity.py`](./backend/scripts/check_purity.py), which parses
each module's AST and fails the build on a forbidden import. Purity is what makes
the optimizer benchmark reproducible and its tests fast.

---

## Tech stack

| Layer | Choice |
|---|---|
| Frontend | Next.js 16 (App Router) · TypeScript · Tailwind 4 · TanStack Query 5 · Recharts |
| Backend | FastAPI · Pydantic v2 |
| ORM / Migrations | SQLAlchemy 2.0 (typed `Mapped[]`) · Alembic |
| Database | PostgreSQL 16 (`btree_gist`, PL/pgSQL) |
| Async | Celery · Redis |
| ML | XGBoost / LightGBM · scikit-learn |
| Optimization | Google OR-Tools (CP-SAT) |
| LLM | Anthropic Claude — Haiku 4.5 default, Sonnet 5 for hard multi-hop |
| Infra | Docker Compose · GitHub Actions |

---

## Repository layout

```
backend/
  app/            web layer — FastAPI, services, ORM models, agent, Celery
  forecasting/    PURE ML package (Phase 3)
  optimizer/      PURE CP-SAT package (Phase 4)
  alembic/        migration history — each schema change is its own migration
  scripts/        data generation, benchmarks, eval, architecture checks
  tests/          pytest; every constraint and optimizer invariant gets a test
frontend/src/     Next.js App Router — server components by default
docs/             phase-by-phase explanations of what was built and why
```

---

## Documentation

The [`docs/`](./docs/) directory explains each phase as a tutorial — what was built,
why it was built that way, and the underlying concepts.

- [Phase 0 — Scaffold](./docs/phase-0-scaffold.md) — Docker, packaging, ASGI,
  SQLAlchemy sessions, Alembic, Celery, RSC, CI gates
- [Glossary](./docs/glossary.md) — every term in the project, defined

---

## Development

```bash
make help      # list all commands
make up        # Postgres + Redis
make api       # API with hot reload
make worker    # Celery worker
make test      # pytest
make lint      # ruff + black + mypy + architecture purity
make check     # everything CI runs, locally
```

CI runs on every push: backend (ruff, black, mypy, purity, migrations, pytest
against real Postgres 16), frontend (eslint, tsc, build), and Docker image builds.

---

## License

Not yet licensed. Synthetic data only; no clinical use.
