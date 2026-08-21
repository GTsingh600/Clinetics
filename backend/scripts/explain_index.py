"""Measure what the (doctor_id, appointment_date) index actually buys.

Produces a reproducible before/after rather than a screenshot. The "before"
measurement is taken by dropping the index *inside a transaction* and rolling
back, which works because PostgreSQL has transactional DDL — the index is gone
for the duration of the measurement and restored by the ROLLBACK, with no
window in which the real schema is missing it.

The alternative, `SET enable_indexscan = off`, is worse: it tells the planner to
avoid index scans rather than removing the option, so the resulting plan is not
the plan you would genuinely get without the index.

    uv run python scripts/explain_index.py
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings

INDEX_NAME = "ix_appointment_doctor_id_appointment_date"
# The other index that can partly serve this query. It must be dropped too to
# see what the table costs with no relevant index at all -- otherwise the
# "without" measurement is really "with a worse index", which understates the
# comparison and quietly misreports what is being measured.
FALLBACK_INDEX = "ix_appointment_date_specialty"
REPEATS = 7

# The query the calendar view issues constantly: one doctor, a date range.
# Equality on doctor_id, range on appointment_date -- exactly the access
# pattern the column order of the index was chosen for.
QUERY = """
SELECT id, appointment_date, start_time, end_time, status
FROM appointment
WHERE doctor_id = :doctor_id
  AND appointment_date BETWEEN :start_date AND :end_date
ORDER BY appointment_date, start_time
"""


def explain(conn: Connection, params: dict) -> tuple[float, str, int]:
    """Return (execution_ms, top node type, rows) for one EXPLAIN ANALYZE run."""
    sql = text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {QUERY}")
    raw = conn.execute(sql, params).scalar_one()
    plan = raw[0]["Plan"] if isinstance(raw, list) else json.loads(raw)[0]["Plan"]
    exec_ms = raw[0]["Execution Time"] if isinstance(raw, list) else 0.0

    def node_types(node: dict) -> list[str]:
        out = [node["Node Type"]]
        for child in node.get("Plans", []):
            out.extend(node_types(child))
        return out

    return exec_ms, " -> ".join(node_types(plan)), int(plan.get("Actual Rows", 0))


def measure(conn: Connection, params: dict) -> tuple[float, str, int]:
    """Median of REPEATS runs; the first run pays cache-warming costs."""
    results = [explain(conn, params) for _ in range(REPEATS)]
    times = [r[0] for r in results]
    return statistics.median(times), results[-1][1], results[-1][2]


def main() -> int:
    engine = create_engine(settings.database_url_sync, future=True)

    with engine.connect() as conn:
        total = conn.execute(text("SELECT count(*) FROM appointment")).scalar_one()
        row = conn.execute(
            text(
                "SELECT doctor_id, min(appointment_date), max(appointment_date) "
                "FROM appointment GROUP BY doctor_id ORDER BY count(*) DESC LIMIT 1"
            )
        ).one()
        doctor_id, min_date, max_date = row
        # A realistic calendar window: roughly a fortnight, not the whole span.
        params = {
            "doctor_id": doctor_id,
            "start_date": min_date,
            "end_date": min_date + (max_date - min_date) / 26,
        }

        conn.execute(text("ANALYZE appointment"))

        # A: production schema, composite index present.
        with_ms, with_plan, rows = measure(conn, params)

        # SQLAlchemy has already autobegun a transaction on this connection, so
        # both DROPs join it and the single rollback below undoes them.
        # PostgreSQL's transactional DDL is what makes this safe.

        # B: composite index gone, planner falls back to the date/specialty one.
        conn.execute(text(f"DROP INDEX {INDEX_NAME}"))
        conn.execute(text("ANALYZE appointment"))
        fallback_ms, fallback_plan, _ = measure(conn, params)

        # C: no usable index at all -- the true cost of scanning the table.
        conn.execute(text(f"DROP INDEX {FALLBACK_INDEX}"))
        conn.execute(text("ANALYZE appointment"))
        noindex_ms, noindex_plan, _ = measure(conn, params)

        conn.rollback()

        # Prove the rollback restored both.
        still_there = conn.execute(
            text("SELECT count(*) FROM pg_indexes WHERE indexname IN (:a, :b)"),
            {"a": INDEX_NAME, "b": FALLBACK_INDEX},
        ).scalar_one()

    vs_fallback = fallback_ms / with_ms if with_ms > 0 else float("inf")
    vs_none = noindex_ms / with_ms if with_ms > 0 else float("inf")

    print("=" * 78)
    print("EXPLAIN ANALYZE: (doctor_id, appointment_date) composite index")
    print("=" * 78)
    print(f"table rows          : {total:,}")
    print(f"query               : one doctor, {params['start_date']} .. {params['end_date']}")
    print(f"rows returned       : {rows}")
    print(f"median of           : {REPEATS} runs")
    print("-" * 78)
    print(f"C  no usable index   {noindex_ms:9.3f} ms   {noindex_plan}")
    print(f"B  fallback index    {fallback_ms:9.3f} ms   {fallback_plan}")
    print(f"A  composite index   {with_ms:9.3f} ms   {with_plan}")
    print("-" * 78)
    print(f"A vs C (no index)   : {vs_none:6.1f}x faster")
    print(f"A vs B (fallback)   : {vs_fallback:6.1f}x faster")
    print(f"indexes restored    : {'yes' if still_there == 2 else 'NO -- INVESTIGATE'}")
    print("=" * 78)

    if still_there != 2:
        print("ERROR: the indexes were not restored by the rollback.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
