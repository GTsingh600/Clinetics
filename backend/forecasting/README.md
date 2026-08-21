# `forecasting/` — pure ML package

**Purity rule (enforced in CI by `scripts/check_purity.py`):** this package must
never import `fastapi`, `sqlalchemy`, `app.*`, or `celery`.

It takes plain data in (DataFrames, dataclasses) and returns plain results out.
That makes every model independently testable without a database or a running
API, and keeps the training code reusable from a notebook, a script, or a task.

Populated in Phase 3.
