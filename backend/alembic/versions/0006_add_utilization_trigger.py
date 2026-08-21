"""Maintain analytics.doctor_utilization with a PL/pgSQL trigger

Revision ID: 0006_util_trigger
Revises: 0005_appt_index
Create Date: 2026-08-21

Keeps a per-doctor, per-day summary of appointment counts and booked minutes
in step with `appointment` on every write.

**Why a trigger rather than application code or a nightly job.**

The summary must be correct no matter who writes. The synthetic data generator
bulk-inserts, migrations backfill, and an operator occasionally fixes a row in
psql — none of those go through the ORM. A guarantee that lives in the database
holds for all of them; one that lives in a service layer holds only for traffic
that happens to pass through it. A nightly recomputation would additionally
leave the table wrong for up to a day, which is useless for a dashboard.

**The delta approach, and the case that makes it interesting.**

Rather than recounting a whole day on every write (an aggregate over an
ever-growing table), the trigger applies a *delta*: it subtracts the OLD row's
contribution and adds the NEW row's. This makes each write O(1).

The awkward case is an UPDATE that moves an appointment to a different doctor
or a different date. A naive implementation updates "the" summary row and
silently corrupts two of them — the old cell keeps a contribution that left,
and the new cell never receives it. Because the subtract step keys off
`OLD.doctor_id, OLD.appointment_date` and the add step off `NEW.doctor_id,
NEW.appointment_date`, moves are handled by construction. `test_trigger.py`
covers exactly this.

**Cancelled appointments** contribute to `cancelled_count` but not to
`booked_minutes`, matching the definition of "occupies a slot" used by the
exclusion constraint in 0004. The two must agree, or utilisation would count
time the calendar considers free.

**AFTER, not BEFORE**: the row must already have passed every CHECK and the
exclusion constraint before it is counted. Returning NULL is correct for an
AFTER FOR EACH ROW trigger — the return value is ignored.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_util_trigger"
down_revision: str | None = "0005_appt_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


APPLY_DELTA_FN = """
CREATE OR REPLACE FUNCTION analytics.apply_utilization_delta(
    p_doctor_id  INTEGER,
    p_date       DATE,
    p_scheduled  INTEGER,
    p_completed  INTEGER,
    p_cancelled  INTEGER,
    p_no_show    INTEGER,
    p_minutes    INTEGER
) RETURNS VOID AS $$
BEGIN
    -- Upsert. The row for a (doctor, date) cell may not exist yet on the first
    -- appointment of that day, so INSERT ... ON CONFLICT handles both create
    -- and accumulate in one statement, with no read-then-write race.
    INSERT INTO analytics.doctor_utilization AS du (
        doctor_id, utilization_date,
        scheduled_count, completed_count, cancelled_count, no_show_count,
        booked_minutes, created_at, updated_at
    )
    VALUES (
        p_doctor_id, p_date,
        GREATEST(p_scheduled, 0), GREATEST(p_completed, 0),
        GREATEST(p_cancelled, 0), GREATEST(p_no_show, 0),
        GREATEST(p_minutes, 0), now(), now()
    )
    ON CONFLICT (doctor_id, utilization_date) DO UPDATE SET
        -- GREATEST(..., 0) defends the counts_non_negative CHECK. Reaching zero
        -- here would mean the summary had already drifted; clamping keeps the
        -- table readable instead of failing every subsequent write.
        scheduled_count = GREATEST(du.scheduled_count + p_scheduled, 0),
        completed_count = GREATEST(du.completed_count + p_completed, 0),
        cancelled_count = GREATEST(du.cancelled_count + p_cancelled, 0),
        no_show_count   = GREATEST(du.no_show_count   + p_no_show,   0),
        booked_minutes  = GREATEST(du.booked_minutes  + p_minutes,   0),
        updated_at      = now();
END;
$$ LANGUAGE plpgsql;
"""

SYNC_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION analytics.appointment_utilization_sync()
RETURNS TRIGGER AS $$
BEGIN
    -- Remove the OLD row's contribution (UPDATE and DELETE).
    IF (TG_OP = 'UPDATE' OR TG_OP = 'DELETE') THEN
        PERFORM analytics.apply_utilization_delta(
            OLD.doctor_id,
            OLD.appointment_date,
            CASE WHEN OLD.status = 'scheduled' THEN -1 ELSE 0 END,
            CASE WHEN OLD.status = 'completed' THEN -1 ELSE 0 END,
            CASE WHEN OLD.status = 'cancelled' THEN -1 ELSE 0 END,
            CASE WHEN OLD.status = 'no_show'   THEN -1 ELSE 0 END,
            CASE WHEN OLD.status = 'cancelled' THEN 0
                 ELSE -OLD.duration_minutes END
        );
    END IF;

    -- Add the NEW row's contribution (INSERT and UPDATE). Keying off NEW's
    -- doctor and date is what makes a cross-doctor or cross-date move correct.
    IF (TG_OP = 'INSERT' OR TG_OP = 'UPDATE') THEN
        PERFORM analytics.apply_utilization_delta(
            NEW.doctor_id,
            NEW.appointment_date,
            CASE WHEN NEW.status = 'scheduled' THEN 1 ELSE 0 END,
            CASE WHEN NEW.status = 'completed' THEN 1 ELSE 0 END,
            CASE WHEN NEW.status = 'cancelled' THEN 1 ELSE 0 END,
            CASE WHEN NEW.status = 'no_show'   THEN 1 ELSE 0 END,
            CASE WHEN NEW.status = 'cancelled' THEN 0
                 ELSE NEW.duration_minutes END
        );
    END IF;

    -- AFTER FOR EACH ROW: the return value is ignored.
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

CREATE_TRIGGER = """
CREATE TRIGGER trg_appointment_utilization_sync
AFTER INSERT OR UPDATE OR DELETE ON appointment
FOR EACH ROW
EXECUTE FUNCTION analytics.appointment_utilization_sync();
"""

# Backfill so the summary is correct for rows that already exist. Without this,
# the table would only reflect appointments written after the trigger was
# installed, which is the classic way a summary table starts out silently wrong.
BACKFILL = """
INSERT INTO analytics.doctor_utilization (
    doctor_id, utilization_date,
    scheduled_count, completed_count, cancelled_count, no_show_count,
    booked_minutes, created_at, updated_at
)
SELECT
    doctor_id,
    appointment_date,
    COUNT(*) FILTER (WHERE status = 'scheduled'),
    COUNT(*) FILTER (WHERE status = 'completed'),
    COUNT(*) FILTER (WHERE status = 'cancelled'),
    COUNT(*) FILTER (WHERE status = 'no_show'),
    COALESCE(SUM(duration_minutes) FILTER (WHERE status <> 'cancelled'), 0),
    now(), now()
FROM appointment
GROUP BY doctor_id, appointment_date
ON CONFLICT (doctor_id, utilization_date) DO NOTHING;
"""


def upgrade() -> None:
    op.execute(APPLY_DELTA_FN)
    op.execute(SYNC_TRIGGER_FN)
    op.execute(CREATE_TRIGGER)
    op.execute(BACKFILL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_appointment_utilization_sync ON appointment")
    op.execute("DROP FUNCTION IF EXISTS analytics.appointment_utilization_sync()")
    op.execute(
        "DROP FUNCTION IF EXISTS analytics.apply_utilization_delta"
        "(INTEGER, DATE, INTEGER, INTEGER, INTEGER, INTEGER, INTEGER)"
    )
    op.execute("TRUNCATE analytics.doctor_utilization")
