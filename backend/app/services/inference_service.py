"""Serving layer for the trained models.

Loads artifacts once per process and answers prediction requests. This is the
impure side of the boundary: it talks to the database to assemble the features
the pure `forecasting/` package needs, then hands them over.

Two properties matter more than speed here.

**The same feature code trains and serves.** Features are built by calling
`forecasting.features`, never by reimplementing the transformations in serving
code. A training/serving skew — where the model is fed subtly different numbers
than it learned on — is silent, produces no error, and degrades predictions in
ways that look like the model simply being bad.

**Missing artifacts degrade, they do not crash.** A checkout where nobody has
run `train_models.py` should still boot and serve the rest of the API. The
prediction endpoints report that no model is loaded rather than 500ing.
"""

from __future__ import annotations

import datetime as dt
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine

from forecasting import features, registry
from forecasting.demand import DemandModel
from forecasting.duration import DurationModel
from forecasting.no_show import NoShowModel

log = logging.getLogger(__name__)

ARTIFACTS_DIR = Path(__file__).resolve().parent.parent.parent / "artifacts"

# Fallback used when no duration model is available, so the optimizer always
# gets a usable number.
DEFAULT_DURATION_MINUTES = 30.0


class ModelsUnavailableError(RuntimeError):
    """No trained artifacts on disk."""


class LoadedModels:
    """The three models plus their cards, loaded once."""

    def __init__(self, directory: Path = ARTIFACTS_DIR) -> None:
        self.directory = directory
        self.no_show: NoShowModel | None = None
        self.demand: DemandModel | None = None
        self.duration: DurationModel | None = None
        self.cards: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        for name, attribute in (
            ("no_show", "no_show"),
            ("demand", "demand"),
            ("duration", "duration"),
        ):
            try:
                model, card = registry.load_model(name, self.directory)
                setattr(self, attribute, model)
                self.cards[name] = card
            except FileNotFoundError:
                log.warning(
                    "no %s artifact in %s; prediction endpoints will report it as "
                    "unavailable. Run: uv run python scripts/train_models.py",
                    name,
                    self.directory,
                )

    @property
    def available(self) -> bool:
        return any((self.no_show, self.demand, self.duration))

    @property
    def no_show_threshold(self) -> float:
        """The operating point chosen at training time, from the model card.

        Read from the card rather than hardcoded, so retraining with a different
        cost policy takes effect without a code change — and so the number
        serving uses is provably the one the metrics were reported at.
        """
        return float(self.cards.get("no_show", {}).get("threshold") or 0.5)


@lru_cache(maxsize=1)
def get_models() -> LoadedModels:
    """Process-wide singleton. Deserialising on every request would dominate."""
    return LoadedModels()


def reset_models_cache() -> None:
    """Drop the cache so a retrain can be picked up without a restart."""
    get_models.cache_clear()


# ---------------------------------------------------------------------------
# Predictions
# ---------------------------------------------------------------------------
_APPOINTMENT_SQL = """
SELECT
    a.id AS appointment_id, a.patient_id, a.doctor_id, s.slug AS specialty,
    a.appointment_date, a.start_time, a.duration_minutes,
    a.status::text AS status, a.urgency::text AS urgency,
    a.is_new_patient, a.booked_at
FROM appointment a
JOIN specialty s ON s.id = a.specialty_id
WHERE a.patient_id = ANY(:patient_ids)
"""


def _patient_context(engine: Engine, patient_ids: list[int]) -> pd.DataFrame:
    """Every appointment for these patients, so history features can be built.

    History is the whole reason this query exists. Scoring an appointment in
    isolation would give the model a patient with no past, which is a different
    (and worse) prediction than the one it was trained to make.
    """
    from sqlalchemy import text

    frame = pd.read_sql(text(_APPOINTMENT_SQL), engine, params={"patient_ids": patient_ids})
    if not frame.empty:
        frame["appointment_date"] = pd.to_datetime(frame["appointment_date"]).dt.date
        frame["booked_at"] = pd.to_datetime(frame["booked_at"], utc=True)
    return frame


def predict_no_show(
    engine: Engine,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Score appointments for no-show risk.

    `rows` are appointment-shaped dicts; they may describe appointments that do
    not exist yet, which is what makes this usable at booking time.
    """
    models = get_models()
    if models.no_show is None:
        raise ModelsUnavailableError("no no-show model; run scripts/train_models.py")
    if not rows:
        return []

    candidate = pd.DataFrame(rows)
    patient_ids = [int(p) for p in candidate["patient_id"].unique()]
    history = _patient_context(engine, patient_ids)

    # History and candidates are scored as one frame so the candidates' history
    # features can see the patient's past — scoring an appointment in isolation
    # would present every patient as brand new.
    #
    # `build_no_show_features` keeps only rows with an outcome, so the
    # candidates are marked `completed` purely to survive that filter. The label
    # it produces is discarded; only the feature row is used. Their own status
    # never enters their features, and their `booked_at` still bounds which
    # history is visible, so this cannot leak.
    candidate = candidate.assign(status="completed")
    combined = pd.concat([history, candidate], ignore_index=True)

    X_all, _ = features.build_no_show_features(combined)
    X = X_all.tail(len(candidate))

    probabilities = models.no_show.predict_proba(X)
    threshold = models.no_show_threshold

    return [
        {
            "patient_id": int(row["patient_id"]),
            "appointment_date": str(row["appointment_date"]),
            "start_time": str(row["start_time"]),
            "no_show_probability": round(float(p), 4),
            "flagged": bool(p >= threshold),
            "threshold": threshold,
        }
        for row, p in zip(rows, probabilities, strict=True)
    ]


def predict_demand(
    specialty: str,
    start_date: dt.date,
    end_date: dt.date,
    *,
    open_hour: int = 8,
    close_hour: int = 18,
) -> list[dict[str, Any]]:
    """Forecast hourly demand for a specialty over a date range.

    Needs no database: demand depends only on the calendar and the profile
    carried on the model.
    """
    models = get_models()
    if models.demand is None:
        raise ModelsUnavailableError("no demand model; run scripts/train_models.py")

    dates = pd.date_range(start_date, end_date, freq="D").date
    grid = pd.MultiIndex.from_product(
        [[specialty], dates, range(open_hour, close_hour + 1)],
        names=["specialty", "appointment_date", "hour_of_day"],
    ).to_frame(index=False)
    grid["count"] = 0  # placeholder; the target is unused at prediction time

    X, _ = features.build_demand_features(grid, profile=models.demand.profile)
    predictions = models.demand.predict(X)

    return [
        {
            "specialty": specialty,
            "date": str(row.appointment_date),
            "hour": int(row.hour_of_day),
            "predicted_demand": round(float(p), 3),
        }
        for row, p in zip(grid.itertuples(), predictions, strict=True)
    ]


def predict_duration(engine: Engine, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Predict consultation length for candidate appointments."""
    models = get_models()
    if models.duration is None:
        return [
            {**row, "predicted_duration_minutes": DEFAULT_DURATION_MINUTES, "fallback": True}
            for row in rows
        ]
    if not rows:
        return []

    candidate = pd.DataFrame(rows)
    candidate["status"] = "completed"
    candidate["duration_minutes"] = 0.0
    history = _patient_context(engine, [int(p) for p in candidate["patient_id"].unique()])
    combined = pd.concat([history, candidate], ignore_index=True)

    X, _ = features.build_duration_features(combined)
    predictions = models.duration.predict(X.tail(len(candidate)))

    return [
        {
            "patient_id": int(row["patient_id"]),
            "specialty": str(row["specialty"]),
            "predicted_duration_minutes": round(float(p), 1),
            "fallback": False,
        }
        for row, p in zip(rows, predictions, strict=True)
    ]


def model_status() -> dict[str, Any]:
    """What is loaded, and what it scored. Surfaced by the API for the demo."""
    models = get_models()
    return {
        "available": models.available,
        "artifacts_dir": str(models.directory),
        "models": {
            name: {
                "trained_at": card.get("trained_at"),
                "git_sha": card.get("git_sha"),
                "seed": card.get("seed"),
                "train_rows": card.get("train_rows"),
                "train_date_range": card.get("train_date_range"),
                "threshold": card.get("threshold"),
                "threshold_rationale": card.get("threshold_rationale"),
                "n_features": len(card.get("feature_names", [])),
            }
            for name, card in models.cards.items()
        },
    }
