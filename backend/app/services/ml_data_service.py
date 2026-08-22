"""The boundary between the database and the pure forecasting package.

`forecasting/` may not import SQLAlchemy — CI enforces it. So somebody has to
turn rows into DataFrames, and that somebody lives here, on the impure side.

Everything below returns plain pandas objects with the column names declared in
`forecasting.types.Columns`. Nothing here knows what a model is; nothing in
`forecasting/` knows what a database is. That is the whole arrangement, and it
is what lets the models be trained from a script, a notebook, or a Celery task
without dragging a web framework along.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
from sqlalchemy import Engine, text

# One query, deliberately. Loading appointments and then fetching each
# specialty name per row would be an N+1; joining once and letting Postgres do
# the work is both faster and simpler to read.
APPOINTMENTS_SQL = """
SELECT
    a.id                AS appointment_id,
    a.patient_id,
    a.doctor_id,
    s.slug              AS specialty,
    a.appointment_date,
    a.start_time,
    a.duration_minutes,
    a.status::text      AS status,
    a.urgency::text     AS urgency,
    a.is_new_patient,
    a.booked_at
FROM appointment a
JOIN specialty s ON s.id = a.specialty_id
-- The casts are required, not decorative. In a bare "parameter IS NULL" test
-- PostgreSQL has no column to infer the parameter's type from and raises
-- AmbiguousParameter, so each bind is cast explicitly.
--
-- Note also: do not write a colon-prefixed name inside these comments.
-- SQLAlchemy's text() scans the whole string for bind parameters, comments
-- included, and would demand a value for one that does not exist.
WHERE (CAST(:start_date AS date) IS NULL OR a.appointment_date >= CAST(:start_date AS date))
  AND (CAST(:end_date   AS date) IS NULL OR a.appointment_date <= CAST(:end_date   AS date))
ORDER BY a.appointment_date, a.start_time, a.id
"""


def load_appointments(
    engine: Engine,
    *,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
) -> pd.DataFrame:
    """Every appointment in the window, as one flat frame.

    Includes cancelled and future appointments. Filtering is left to the
    feature builders because each model wants a different subset — the no-show
    classifier needs resolved outcomes, the demand grid needs everything that
    occupied the calendar, the duration model needs completed visits only. A
    loader that pre-filtered would have to be called three different ways.
    """
    frame = pd.read_sql(
        text(APPOINTMENTS_SQL),
        engine,
        params={"start_date": start_date, "end_date": end_date},
    )
    frame["appointment_date"] = pd.to_datetime(frame["appointment_date"]).dt.date
    frame["booked_at"] = pd.to_datetime(frame["booked_at"], utc=True)
    return frame


def load_specialties(engine: Engine) -> pd.DataFrame:
    return pd.read_sql(
        text("SELECT id, slug, name, default_duration_minutes FROM specialty ORDER BY id"),
        engine,
    )


def load_doctors(engine: Engine) -> pd.DataFrame:
    return pd.read_sql(
        text(
            "SELECT d.id, d.first_name, d.last_name, d.clinic_id "
            "FROM doctor d WHERE d.is_active ORDER BY d.id"
        ),
        engine,
    )


def training_summary(frame: pd.DataFrame) -> dict[str, Any]:
    """A few facts about the frame, for the model card and the run log.

    Cheap, and it is what tells you a retrain saw the data you expected rather
    than an empty or truncated table.
    """
    resolved = frame[frame["status"].isin(["completed", "no_show"])]
    return {
        "rows": len(frame),
        "resolved_rows": len(resolved),
        "no_show_rate": (
            round(float((resolved["status"] == "no_show").mean()), 4) if len(resolved) else None
        ),
        "date_range": (
            str(frame["appointment_date"].min()),
            str(frame["appointment_date"].max()),
        ),
        "patients": int(frame["patient_id"].nunique()),
        "specialties": int(frame["specialty"].nunique()),
    }
