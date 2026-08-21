# `optimizer/` — pure CP-SAT package

**Purity rule (enforced in CI by `scripts/check_purity.py`):** this package must
never import `fastapi`, `sqlalchemy`, `app.*`, or `celery`.

The scheduler is a function: `(appointments, doctors, rooms, constraints) -> Schedule`.
No I/O. That is what lets the benchmark script compare the CP-SAT model against
the greedy baseline deterministically, and what lets every optimizer invariant
be unit-tested in milliseconds.

Populated in Phase 4.
