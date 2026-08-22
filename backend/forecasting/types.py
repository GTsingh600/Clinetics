"""Shared types for the forecasting package.

Keeping these here rather than importing Pydantic schemas or ORM models is what
lets `forecasting/` stay pure: it depends on pandas and scikit-learn, and on
nothing that knows about HTTP or a database. The API layer converts.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Literal


# Column names the package expects on the raw appointment frame. Declared once
# so a rename in the data loader fails loudly here instead of producing a
# silently-empty feature.
class Columns:
    APPOINTMENT_ID = "appointment_id"
    PATIENT_ID = "patient_id"
    DOCTOR_ID = "doctor_id"
    SPECIALTY = "specialty"
    APPOINTMENT_DATE = "appointment_date"
    START_TIME = "start_time"
    DURATION = "duration_minutes"
    STATUS = "status"
    URGENCY = "urgency"
    IS_NEW_PATIENT = "is_new_patient"
    BOOKED_AT = "booked_at"


@dataclass(frozen=True)
class Fold:
    """One rolling-origin fold: train on everything before `train_end`."""

    index: int
    train_end: dt.date
    test_start: dt.date
    test_end: dt.date

    def __str__(self) -> str:
        return (
            f"fold {self.index}: train < {self.train_end} | "
            f"test {self.test_start}..{self.test_end}"
        )


@dataclass
class ClassificationMetrics:
    """Metrics for the no-show classifier at one decision threshold."""

    threshold: float
    accuracy: float
    precision: float
    recall: float
    f1: float
    roc_auc: float
    pr_auc: float
    brier: float
    # Confusion matrix, in the order the report prints it.
    true_negatives: int
    false_positives: int
    false_negatives: int
    true_positives: int
    support: int
    positive_rate: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "threshold": round(self.threshold, 4),
            "accuracy": round(self.accuracy, 4),
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "roc_auc": round(self.roc_auc, 4),
            "pr_auc": round(self.pr_auc, 4),
            "brier": round(self.brier, 4),
            "confusion_matrix": {
                "true_negatives": self.true_negatives,
                "false_positives": self.false_positives,
                "false_negatives": self.false_negatives,
                "true_positives": self.true_positives,
            },
            "support": self.support,
            "positive_rate": round(self.positive_rate, 4),
        }


@dataclass
class RegressionMetrics:
    """Metrics for the demand and duration models."""

    mae: float
    rmse: float
    # Mean error, signed. Separates "wrong" from "biased": an RMSE of 2 with a
    # bias of -2 means the model is systematically under-predicting, which is a
    # different problem from noisy-but-centred.
    bias: float
    r2: float
    support: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "mae": round(self.mae, 4),
            "rmse": round(self.rmse, 4),
            "bias": round(self.bias, 4),
            "r2": round(self.r2, 4),
            "support": self.support,
        }


@dataclass
class FoldResult:
    """A model's score on one fold, alongside the baselines it must beat."""

    fold: Fold
    model: dict[str, Any]
    baselines: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class ModelCard:
    """Metadata written next to every saved model.

    Without this an artifact is an opaque binary: you cannot tell which code
    produced it, on what data, or whether the feature list still matches the
    one the serving code builds. Every field here exists to answer a question
    someone will eventually ask of a prediction.
    """

    name: str
    kind: Literal["classifier", "regressor"]
    trained_at: str
    git_sha: str | None
    seed: int
    feature_names: list[str]
    train_rows: int
    train_date_range: tuple[str, str]
    params: dict[str, Any]
    metrics: dict[str, Any]
    # For the classifier: the operating point chosen, and why.
    threshold: float | None = None
    threshold_rationale: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "trained_at": self.trained_at,
            "git_sha": self.git_sha,
            "seed": self.seed,
            "feature_names": self.feature_names,
            "train_rows": self.train_rows,
            "train_date_range": list(self.train_date_range),
            "params": self.params,
            "metrics": self.metrics,
            "threshold": self.threshold,
            "threshold_rationale": self.threshold_rationale,
        }
