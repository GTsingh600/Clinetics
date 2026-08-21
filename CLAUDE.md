# CLAUDE.md — Clinic Scheduling Agent

> Save this at the repo root. Claude Code reads it automatically on every session.

---

## Project

**Clinic Scheduling Agent** — an AI-powered clinic operations system that forecasts appointment demand, generates optimal schedules via constrained optimization, and exposes both through a tool-using LLM agent that answers natural-language operational queries and runs what-if simulations.

**Core pipeline (one spine, not parallel modules):**
```
Forecast → Optimize → Agent orchestrates & explains → Simulate "what if"
```

**Non-negotiable design principle:** the LLM **never** makes scheduling decisions. It interprets intent, calls tools, and explains results. All actual scheduling comes from the OR-Tools optimizer; all predictions come from trained ML models.

This is a portfolio project. Its purpose is to demonstrate four distinct competencies: database engineering, full-stack systems, constrained optimization, and genuine multi-step agentic tool use. Code quality and defensibility matter more than feature count.

---

## Tech stack (fixed — do not substitute without asking)

| Layer | Choice |
|---|---|
| Frontend | Next.js (App Router) + TypeScript + Tailwind + TanStack Query + Recharts |
| Backend | FastAPI + Pydantic v2 |
| ORM / Migrations | SQLAlchemy 2.0 (typed, `Mapped[]` style) + Alembic |
| Database | PostgreSQL 16 (Docker locally, Neon/Supabase for deploy) |
| Async | Celery + Redis |
| ML | XGBoost / LightGBM, scikit-learn |
| Optimization | Google OR-Tools (CP-SAT) |
| LLM | Anthropic Claude (Haiku 4.5 default, Sonnet 5 for hard multi-hop) via native tool calling |
| Infra | Docker Compose, GitHub Actions CI |

---

## Domain model (do not expand)

```
Clinic · Doctor · Patient · Appointment · Availability · Specialty · Room · Forecast · Schedule
```

**Explicitly out of scope — do not build these even if they seem natural:**
billing, invoices, prescriptions, pharmacy, full EHR, medical document storage, insurance integration, autonomous diagnosis, notifications beyond a minimal stub, more than 3 user roles.

If a task seems to require one of these, stop and ask rather than scaffolding it.

**Roles: exactly 3** — `patient`, `doctor`, `admin`. Admin is the primary agent user.

---

## Database requirements (this is a graded part of the project)

Schema design must demonstrably show database skill:

- **3NF** with **one deliberate, documented denormalization**: store the doctor's specialty on `Appointment` as of booking time. Add a comment in the model explaining the read-performance rationale.
- `Doctor ↔ Specialty` is **many-to-many** → junction table. Never a string column.
- `Availability` is its own table (doctor + weekday + time window), never fields on `Doctor`.
- `Forecast` and `Schedule` are **derived/analytical** tables, kept structurally separate from the transactional core.
- **Constraints that do real work:**
  - CHECK: appointment within clinic hours; `duration_minutes > 0`; `end_time > start_time`
  - FK with explicit, intentional `ON DELETE` behavior on every relationship
  - EXCLUDE constraint (btree_gist) preventing overlapping appointments for the same doctor — this is preferred over app-level checking
- **Index** on `(doctor_id, appointment_date)`; include `EXPLAIN ANALYZE` before/after in the writeup.
- **One PL/pgSQL trigger**: maintain a `doctor_utilization` summary table on appointment insert/update/delete.
- Every schema change is its **own Alembic migration**. Never edit a migration that has been applied. The migration history is a visible artifact of the project.

---

## Synthetic data (critical — the project's credibility depends on this)

The data is synthetic and must be **openly labeled as such** in the README. It must also contain **genuinely learnable structure**, otherwise the ML layer is meaningless.

The generator (`scripts/generate_data.py`) must produce:
- **No-show probability** correlated with: lead time (longer booking → higher no-show), day of week, time of day, and the patient's own historical no-show rate. Must NOT be i.i.d. random.
- **Demand** with realistic seasonality: weekday/weekend split, Monday-morning peaks, specialty-specific patterns, evening peaks for dermatology.
- **Consultation duration** varying by specialty and whether the patient is new vs. returning.
- Use `Faker` for names/contact fields; custom logic for all correlated distributions.
- Document every distribution and its parameters in a docstring at the top of the script.

**Validation gate:** after generating, produce plots of no-show rate vs. lead time and demand vs. hour-of-week. If the intended correlations are not visible, the generator is wrong — fix it before proceeding to any ML work.

---

## Optimizer spec (the technical centerpiece)

CP-SAT model. Be prepared to justify every weight and constraint.

**Objective:**
```
minimize  w1·(total expected patient wait time)
        + w2·(doctor idle time)
        + w3·(overtime beyond clinic hours)
        + w4·(urgency penalty for delayed high-priority patients)
```
Weights configurable, defaults documented with rationale.

**Constraints:**
- No overlapping appointments per doctor
- All appointments within clinic operating hours
- Doctor specialty must match appointment specialty
- Room capacity never exceeded
- Urgent patients scheduled before non-urgent where feasible
- Appointment duration must fit the allocated slot
- Mandatory doctor breaks respected

**Also required:** a greedy/FCFS baseline scheduler in the same interface, plus a benchmark script reporting the optimizer's improvement over it (% reduction in average wait time, doctor idle time, overtime). That number goes on the resume — it must be real and reproducible.

---

## Agent spec

**Tools:**
```
forecast_demand(specialty, date_range)
predict_wait_time(doctor_id, date)
get_doctor_availability(doctor_id?, date_range)
optimize_schedule(date, constraints_override?)
simulate_scenario(date, demand_multiplier?, doctor_unavailable?)
```

**Hard requirement — no intent→function lookup tables.** The agent must decide tool sequencing itself. These queries must work without hardcoded chains:
- *"What if Dr. Sharma is out tomorrow?"* → availability → re-forecast load distribution → re-optimize
- *"How much extra capacity do we need next week?"* → forecast → compare against availability → quantify gap
- *"Why is the 5 PM slot predicted to have long waits?"* → forecast + wait-time prediction → grounded explanation

**Grounded explanations only.** Every explanation must cite the specific forecast values, constraint violations, or optimizer outputs that produced it. Never generate plausible-sounding reasoning that isn't traceable to a tool result. If the data doesn't support an explanation, say so.

---

## Engineering rigor (do not skip — these are the differentiators)

1. **Concurrency safety.** The booking path must be correct under concurrent writes (two patients, same last slot; or a manual booking while the optimizer regenerates). Use proper transaction isolation / `SELECT ... FOR UPDATE`. Write a test that reproduces the race *without* the fix and passes *with* it. Document both in the README.
2. **Eval harness.** Precision/recall/confusion matrix for no-show classification; MAE/RMSE for demand and duration forecasting. Committed as a reproducible script, not screenshots.
3. **Load numbers.** Locust or k6 run; record p95 API latency and optimizer solve time at realistic N. Real figures only.
4. **Demo resilience.** A cached/scripted fallback path so a live demo survives an LLM API rate-limit or outage.

---

## Code conventions

- Python: type hints everywhere, `ruff` + `black`, Pydantic v2 schemas separate from ORM models.
- Services layer between routes and DB — no business logic in route handlers.
- The optimizer and forecasting modules must be **pure and independently testable**, with no FastAPI or DB imports. They take data in, return results out.
- Frontend: server components by default, client components only where interactivity requires it.
- `pytest` for backend; every constraint and every optimizer invariant gets a test.
- Secrets in `.env`, never committed. Provide `.env.example`.
- Conventional commits.

---

## Build order

Work in phases. **Do not jump ahead** — later phases depend on earlier ones being correct.

**Phase 0 — Scaffold:** repo structure, `docker-compose.yml` (Postgres + Redis), FastAPI skeleton, Next.js skeleton, Alembic init, CI workflow.

**Phase 1 — Database:** SQLAlchemy models → initial migration → constraints/index/trigger migrations → synthetic data generator → validation plots. *Do not proceed until the validation gate passes.*

**Phase 2 — Core app:** auth + 3-role RBAC → CRUD APIs → per-role dashboards and calendar UI → Celery/Redis async jobs → concurrency-safe booking path + race test.

**Phase 3 — Forecasting:** feature engineering → train no-show classifier and demand/duration models → eval harness with committed metrics → inference service.

**Phase 4 — Optimizer:** CP-SAT model → greedy baseline → benchmark script → service wrapper.

**Phase 5 — Agent:** tool schemas → tool-calling loop → single-hop queries working → multi-hop chained queries reliable → grounded explanations.

**Phase 6 — Simulation:** what-if endpoint (demand multiplier, doctor unavailability) → before/after diff UI.

**Phase 7 — Rigor pass:** load test → concurrency writeup → demo fallback path → README (data disclosure, ERD, architecture diagram, eval numbers, benchmark results).

**Phase 8 — Deploy:** Neon/Supabase Postgres → backend on Render/Railway/Fly → frontend on Vercel → full end-to-end demo run-through.

---

## How I want you to work

- At the start of each phase, propose the file structure and key design decisions **before** writing code. Wait for my confirmation.
- Ask before adding any dependency not listed above.
- Ask before expanding the domain model or adding a fourth role.
- Prefer small, reviewable commits over large drops.
- When a design decision has a real trade-off, state the alternatives and your recommendation rather than silently picking one.
- If something in this file conflicts with what I ask in chat, flag the conflict instead of guessing.
