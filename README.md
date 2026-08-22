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
| **1 — Database** | Models, constraints, EXCLUDE, trigger, synthetic data + validation gate | ✅ **Done & verified** |
| **2 — Core app** | Auth + 3-role RBAC, CRUD, calendar UI, concurrency-safe booking | ✅ **Done & verified** |
| **3 — Forecasting** | No-show classifier, demand & duration models, eval harness | ✅ **Done & verified** |
| 4 — Optimizer | CP-SAT model, greedy baseline, benchmark | ⬜ Next |
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

# 2. Backend  (uv sync installs the exact versions pinned in uv.lock)
cd backend
uv sync --extra ml --group dev --frozen
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
#   → http://localhost:8000/api/v1/health
#   → http://localhost:8000/docs

# 3. Frontend (new terminal)
cd frontend
npm install
npm run dev                   # → http://localhost:3000
```

`uv run` works identically on every OS. If you prefer `make`: `make up`,
`make install`, `make api`, `make web`, `make test`, `make check`.

Changed a dependency in `pyproject.toml`? Run `make lock` (`uv lock`) and commit
the updated `uv.lock` — CI installs with `--frozen` and will fail on a stale lock.

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
- [Phase 1 — Database](./docs/phase-1-database.md) — normalization, the deliberate
  denormalization, EXCLUDE constraints and why app-level checking cannot be
  correct, ON DELETE as design, generated columns, index measurement, the
  PL/pgSQL trigger, synthetic data and the validation gate
- [Phase 2 — Core app](./docs/phase-2-core-app.md) — cookie vs bearer sessions,
  bcrypt's 72-byte limit, refresh rotation and reuse detection, CSRF without a
  double-submit token, object-level authorization, **the two booking races and
  why they need different fixes**, server components and the cookie problem
- [Phase 3 — Forecasting](./docs/phase-3-forecasting.md) — prediction-time
  leakage and the three ways to get it wrong, rolling-origin validation, the
  missing-zeros trap, why baselines are the result, choosing an operating point
  from cost, calibration vs ranking, and **shipping the simpler model when it wins**
- [Design system](./docs/design-system.md) — palette, typography, tokens
- [Glossary](./docs/glossary.md) — every term in the project, defined

---

## Database (Phase 1)

The schema is the graded core of this project, so the rules that matter live in
PostgreSQL rather than in application code:

- **A double-booked doctor is unrepresentable.** A `btree_gist` `EXCLUDE`
  constraint rejects overlapping appointments at write time, closing the
  time-of-check/time-of-use race that no amount of careful application code can
  fix. Half-open `'[)'` bounds keep back-to-back slots legal; a partial
  `WHERE (status <> 'cancelled')` clause stops a cancellation burning the slot.
- **19 CHECK constraints, 20 foreign keys, zero left as default `NO ACTION`** —
  `RESTRICT` where clinical history must survive, `CASCADE` where a child row is
  meaningless alone, `SET NULL` where a reference can be lost but the row cannot.
- **One PL/pgSQL trigger** maintains a per-doctor/per-day utilisation summary in
  O(1) per write, correctly handling the case naive implementations break on: an
  UPDATE that moves an appointment to another doctor or date must decrement one
  cell and increment another.
- **One deliberate, documented denormalization**: the doctor's specialty snapshotted
  onto `Appointment` at booking time.
- **Analytical tables live in a separate `analytics` schema**, so derived output
  is distinguishable from source-of-truth in the database itself.
- **6 migrations, each one concern**, fully reversible (`downgrade base` →
  `upgrade head` verified), with `alembic check` clean.

```bash
make seed         # generate synthetic data
make validate     # THE DATA GATE - fails if the data lacks learnable structure
make explain      # EXPLAIN ANALYZE the composite index, with and without
make demo-users   # one login per role, linked to the busiest seeded records
make train        # train the no-show, demand and duration models
make eval         # THE MODEL GATE - rolling-origin CV against the baselines
```

### Synthetic data

> ⚠️ Synthetic, and openly so. The value is that it contains **deliberately
> learnable structure** — a model trained on noise would report metrics that
> mean nothing.

No-show probability comes from a logistic model over lead time, day of week,
hour, urgency, new-patient status, and a per-patient latent propensity (so a
patient's own history genuinely predicts their future). Demand is Poisson with
weekday/weekend, Monday-peak, seasonal, and **specialty-specific intra-day**
structure. All parameters are documented in the generator's docstring.

`scripts/validate_data.py` then proves it, on 36,817 appointments:

| Property | Measured |
|---|---|
| No-show vs lead time | **5.4% → 40.2%** (7.5×), Spearman rho=+0.21, buckets monotonic (rho=+1.00) |
| Patient behaviour persists | rho=+0.25 across 3,162 patients' split histories |
| Weekday vs weekend demand | 36.8/day vs 9.5/day |
| Dermatology peak hour | **17:00** (vs general practice 09:00) |
| New-patient duration | +7.6 to +8.3 min in every specialty |
| Index (36,817 rows) | **16.7× faster**; Seq Scan → Bitmap Index Scan |

![No-show rate vs lead time](./backend/reports/no_show_vs_lead_time.png)

---

## Application (Phase 2)

Three roles — `patient`, `doctor`, `admin` — with a session model chosen for what
it exposes you to rather than for convenience.

**Sessions are httpOnly cookies**, never a token in a response body, so page
JavaScript cannot read one. A `Authorization: Bearer` fallback is deliberately
refused: it would reintroduce the XSS exposure the cookie exists to prevent.
A short access token (30 min) pairs with a rotating refresh token; replaying an
already-rotated token revokes the **entire token family**, because by then an
attacker may hold a newer token in the same chain.

**CSRF is handled by origin checking**, not a double-submit token — a
double-submit token must be readable by JavaScript to be echoed back, and an XSS
that can read it can forge requests anyway. The `Origin` header cannot be set by
page script at all.

**Authorization is object-level where roles are not enough.** Both Alice and Bob
hold the `patient` role, so a role check alone would let either read the other;
patient access is checked against the record, and "not yours" returns 404 rather
than 403 so ids cannot be probed. List queries are scoped in the SQL, not
filtered after loading.

### The concurrency work

There are **two** races on the booking path, and the interesting finding is that
the obvious one is already solved:

| Race | Why | Fix |
|---|---|---|
| Same doctor, same slot | Phase 1's `EXCLUDE` constraint makes the corrupt state unrepresentable | Nothing needed for correctness — the app just has to return a clean **409**, not a 500 |
| **Room capacity** | "At most N overlapping appointments in this room" is a property of a *set* of rows, so no CHECK or EXCLUDE can express it. A real lost update. | **`SELECT ... FOR UPDATE`** on the room row, used as a mutex keyed on the room |

The test suite demonstrates both halves of the requirement:

```
test_room_capacity_race_WITHOUT_lock_reproduces_overbooking  PASSED  ← the bug
test_room_capacity_race_WITH_lock_holds_the_limit            PASSED  ← the fix
test_locks_on_different_rooms_do_not_serialise               PASSED  ← not over-locked
```

Interleaving is forced with `asyncio.Barrier` rather than left to timing, and the
tests run on genuinely separate, committing connections — two sessions sharing
one connection cannot contend for a lock, which would make every assertion a
tautology.

### Frontend

Next.js App Router, server components by default. Only five components are
client-side: the query provider, login form, logout button, booking calendar, and
the charts. Everything else — shell, navigation, all three dashboards, the week
calendar — ships zero JavaScript.

Design tokens from [docs/design-system.md](./docs/design-system.md) are wired
into Tailwind v4's `@theme`, so components reference `bg-primary` and
`rounded-card` rather than hex values.

---

## Forecasting (Phase 3)

Three models — no-show, demand, duration — with an evaluation harness that
**exits non-zero** if a model fails to beat the baseline it is measured against.

**Validation is rolling-origin, never random.** Appointments are ordered in
time; a random split trains on the future and tests on the past, which raises the
reported score while lowering real accuracy. Four expanding-window folds give a
mean *and* a spread, so a lucky cutoff shows up as variance instead of as a
result.

**Prediction happens at booking time**, which fixes what counts as leakage. The
patient-history feature — the most predictive one, by construction — may only use
appointments that had already *taken place* when the row was booked. Not booked
earlier: an appointment booked in January for June has no outcome in February.
[16 tests](./backend/tests/unit/test_no_leakage.py) pin that boundary, including
the subtle case.

### The result worth reading

Two of the three models **lost to their baselines**, and the response was to ship
the simpler estimator:

| Model | Model score | Baseline | Outcome |
|---|---|---|---|
| No-show | PR-AUC **0.320** | base rate 0.212 · logistic 0.323 | logistic wins → **logistic ships** |
| Demand | MAE **0.4817** | profile mean 0.4817 | tie → **profile ships** (4/4 folds) |
| Duration | MAE **5.31 min** | specialty mean 5.70 min | **6.9% better** → model ships |

Both are cases where the baseline is close to the *true* generating process — the
no-show data comes from a logistic model, and demand is essentially a
weekday-by-hour profile. Rather than tune until the complicated model won, both
now select between candidates on **training data** (`TimeSeriesSplit`, never the
evaluation folds), and the harness prints which one was chosen so a tie cannot be
misread as a win.

### The operating point

The classifier's threshold comes from an explicit cost, not from 0.5:

| Missed no-show costs | threshold | precision | recall |
|---|---|---|---|
| 1× an over-book | 0.55 | 0.500 | 0.001 |
| 2× | 0.33 | 0.374 | 0.207 |
| 3× | 0.28 | 0.331 | 0.433 |
| 5× | 0.17 | 0.260 | 0.881 |

At 1:1 the optimal policy is to flag almost nothing — an honest finding, and the
reason all four rows are published rather than just the one that was shipped.

### Is ROC-AUC 0.66 any good?

Measured rather than argued. `make ceiling` re-simulates the generative process
keeping the true probability behind each label, and scores it:

| | ROC-AUC | PR-AUC |
|---|---|---|
| **Bayes ceiling** (knows the true `p`) | **0.757** | 0.477 |
| ceiling ignoring patient history | 0.659 | 0.344 |
| **our model** | **0.670** | 0.316 |

The no-show is a `Bernoulli(p)` draw, so **the hard ceiling is 0.757, not 1.0** —
the model captures **66% of the signal available above chance**. Patient history
adds little (0.670 with, 0.664 without) because patients average 3.8 prior
visits, so the latent propensity is estimated from very few observations; both
the encoding and the shrinkage constant were checked and neither is the cause.

The useful part is falsifiability: a model reporting 0.95 on this data would be
evidence of **label leakage, not skill**, which is precisely what the 16 leakage
tests guard against.

![precision/recall trade-off](./backend/reports/metrics/no_show_precision_recall.png)

Probabilities are **calibrated** (isotonic, Brier reported alongside AUC), because
the optimizer sums them to get an expected no-show count and summing uncalibrated
scores produces a number with no meaning.

Prediction endpoints are **staff-only**: a patient cannot read their own no-show
score. It is a self-fulfilling nudge and a fairness problem, and there is a test
so the decision cannot erode.

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
