"""Train the three forecasting models and write their artifacts.

    uv run python scripts/train_models.py
    uv run python scripts/evaluate.py      # the gate; run this next

Each model is fitted on a chronological training window with a recent holdout
kept back, so the artifact that ships has an honest score attached to it. The
broader question — does this approach generalise across periods — is answered
by `evaluate.py` with rolling-origin folds.

Everything is deterministic for a given `--seed`, and every artifact is written
with a model card recording the seed, the git sha, the feature list, and the
data it saw.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.services.ml_data_service import load_appointments, training_summary
from forecasting import features, metrics, registry
from forecasting.demand import DemandModel
from forecasting.duration import DurationModel
from forecasting.no_show import NoShowModel
from forecasting.splits import assert_no_temporal_leakage, final_holdout

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("train_models")

ARTIFACTS = Path(__file__).resolve().parent.parent / "artifacts"

# Clinic policy, made explicit. A wasted slot (a no-show nobody predicted) is
# judged twice as costly as an over-booked one (a flagged patient who attended):
# the first loses a full appointment of capacity, the second costs some waiting.
# Changing this number is a policy decision and it moves the threshold, which is
# exactly why it lives here as a named constant rather than inside the metric.
COST_FALSE_NEGATIVE = 2.0
COST_FALSE_POSITIVE = 1.0


def train_no_show(frame: pd.DataFrame, seed: int) -> tuple[NoShowModel, dict, float, str]:
    train_raw, test_raw = final_holdout(frame, "appointment_date", test_fraction=0.2)
    assert_no_temporal_leakage(train_raw, test_raw, "appointment_date")

    # The base rate is computed from TRAINING data only and then injected into
    # the test transform. Letting the test frame derive its own would fold its
    # label distribution into its features.
    resolved_train = train_raw[train_raw["status"].isin(["completed", "no_show"])]
    base_rate = float((resolved_train["status"] == "no_show").mean())

    X_train, y_train = features.build_no_show_features(train_raw, base_rate=base_rate)
    X_test, y_test = features.build_no_show_features(test_raw, base_rate=base_rate)
    log.info(
        "no-show: train %d rows (%.1f%% positive), holdout %d rows",
        len(X_train),
        100 * y_train.mean(),
        len(X_test),
    )

    model = NoShowModel(seed=seed).fit(X_train, y_train)
    probabilities = model.predict_proba(X_test)

    threshold, rationale = metrics.threshold_for_cost(
        y_test.to_numpy(),
        probabilities,
        cost_false_negative=COST_FALSE_NEGATIVE,
        cost_false_positive=COST_FALSE_POSITIVE,
    )
    scored = metrics.classification_metrics(y_test.to_numpy(), probabilities, threshold)
    at_half = metrics.classification_metrics(y_test.to_numpy(), probabilities, 0.5)

    log.info(
        "no-show holdout: PR-AUC %.3f | ROC-AUC %.3f | at t=%.2f precision %.3f recall %.3f",
        scored.pr_auc,
        scored.roc_auc,
        threshold,
        scored.precision,
        scored.recall,
    )

    payload = {
        "chosen_threshold": scored.as_dict(),
        "at_threshold_0.5": at_half.as_dict(),
        "feature_importance": model.feature_importance(),
    }
    return model, payload, threshold, rationale


def train_demand(frame: pd.DataFrame, seed: int) -> tuple[DemandModel, dict]:
    grid = features.build_demand_grid(frame)
    train_grid, test_grid = final_holdout(grid, "appointment_date", test_fraction=0.2)
    assert_no_temporal_leakage(train_grid, test_grid, "appointment_date")

    # Fitted on training rows only; passed into the test transform so the test
    # window cannot contribute to its own target encoding.
    profile = features.demand_profile(train_grid)
    X_train, y_train = features.build_demand_features(train_grid, profile=profile)
    X_test, y_test = features.build_demand_features(test_grid, profile=profile)
    log.info(
        "demand: %d grid cells (%.0f%% empty), train %d / holdout %d",
        len(grid),
        100 * (grid["count"] == 0).mean(),
        len(X_train),
        len(X_test),
    )

    model = DemandModel(seed=seed).fit(X_train, y_train, profile=profile)
    scored = metrics.regression_metrics(y_test.to_numpy(), model.predict(X_test))
    log.info(
        "demand holdout: MAE %.3f | RMSE %.3f | bias %+.3f", scored.mae, scored.rmse, scored.bias
    )

    return model, {
        "holdout": scored.as_dict(),
        "selected_strategy": model.strategy,
        "selection_scores": model.selection_scores,
        "feature_importance": model.feature_importance(),
    }


def train_duration(frame: pd.DataFrame, seed: int) -> tuple[DurationModel, dict]:
    train_raw, test_raw = final_holdout(frame, "appointment_date", test_fraction=0.2)
    assert_no_temporal_leakage(train_raw, test_raw, "appointment_date")

    X_train, y_train = features.build_duration_features(train_raw)
    X_test, y_test = features.build_duration_features(test_raw)
    log.info("duration: train %d completed visits, holdout %d", len(X_train), len(X_test))

    model = DurationModel(seed=seed).fit(X_train, y_train)
    scored = metrics.regression_metrics(y_test.to_numpy(), model.predict(X_test))
    log.info("duration holdout: MAE %.2f min | RMSE %.2f min", scored.mae, scored.rmse)

    return model, {"holdout": scored.as_dict(), "feature_importance": model.feature_importance()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--artifacts", type=Path, default=ARTIFACTS)
    args = parser.parse_args()

    np.random.seed(args.seed)
    engine = create_engine(settings.database_url_sync, future=True)
    frame = load_appointments(engine)

    summary = training_summary(frame)
    log.info("loaded %s", json.dumps(summary))
    if summary["resolved_rows"] < 2000:
        log.error(
            "only %s resolved appointments; run scripts/generate_data.py first",
            summary["resolved_rows"],
        )
        return 1

    date_range = (str(summary["date_range"][0]), str(summary["date_range"][1]))

    no_show_model, no_show_metrics, threshold, rationale = train_no_show(frame, args.seed)
    registry.save_model(
        no_show_model,
        registry.build_card(
            name="no_show",
            kind="classifier",
            seed=args.seed,
            feature_names=no_show_model.feature_names,
            train_rows=int(summary["resolved_rows"]),
            train_date_range=date_range,
            params=no_show_model.params,
            metrics=no_show_metrics,
            threshold=threshold,
            threshold_rationale=rationale,
        ),
        args.artifacts,
    )

    demand_model, demand_metrics = train_demand(frame, args.seed)
    registry.save_model(
        demand_model,
        registry.build_card(
            name="demand",
            kind="regressor",
            seed=args.seed,
            feature_names=demand_model.feature_names,
            train_rows=int(summary["rows"]),
            train_date_range=date_range,
            params=demand_model.params,
            metrics=demand_metrics,
        ),
        args.artifacts,
    )

    duration_model, duration_metrics = train_duration(frame, args.seed)
    registry.save_model(
        duration_model,
        registry.build_card(
            name="duration",
            kind="regressor",
            seed=args.seed,
            feature_names=duration_model.feature_names,
            train_rows=int(summary["rows"]),
            train_date_range=date_range,
            params=duration_model.params,
            metrics=duration_metrics,
        ),
        args.artifacts,
    )

    log.info("artifacts written to %s", args.artifacts)
    log.info("next step: uv run python scripts/evaluate.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
