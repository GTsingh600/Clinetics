"""THE EVAL HARNESS for Phase 3. A gate, not a report.

    uv run python scripts/evaluate.py

Runs rolling-origin cross-validation for all three models, scores each against
the baselines a competent person would try first, writes the numbers to
`backend/reports/metrics/` as committed evidence, and **exits non-zero if a
model fails to beat its baseline**.

That last part is the point. A metric with nothing to compare against is
unfalsifiable: "ROC-AUC 0.68" means nothing until you know that predicting the
base rate scores 0.50 and a logistic regression scores 0.67. If gradient
boosting cannot beat a simple model, the honest conclusion is to ship the
simple model, and this script is what forces that conversation instead of
quietly reporting the boosted number alone.

Rolling origin, not a random split. Appointments are ordered in time; a random
split trains on the future and tests on the past, which inflates the reported
score while making the model worse. Every fold here trains strictly before its
test window — see `forecasting/splits.py`.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # must run headless in CI
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.services.ml_data_service import load_appointments, training_summary
from forecasting import baselines, features, metrics
from forecasting.demand import DemandModel
from forecasting.duration import DurationModel
from forecasting.no_show import NoShowModel
from forecasting.splits import assert_no_temporal_leakage, rolling_origin_folds, split_by_fold
from forecasting.types import Fold

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("evaluate")

REPORTS = Path(__file__).resolve().parent.parent / "reports" / "metrics"

# What the models must clear to be worth shipping. Deliberately modest: the
# point is to catch a model that has learned nothing or regressed, not to
# manufacture an impressive-looking bar.
GATES = {
    "no_show_beats_base_rate_pr_auc": 1.15,  # >=15% better PR-AUC than the base rate
    "no_show_min_roc_auc": 0.60,
    "demand_beats_seasonal_naive_mae": 1.00,  # must not be worse than the profile
    "duration_beats_specialty_mean_mae": 1.00,
}

# Several cost ratios, because the right one is clinic policy. Reporting a
# range shows how the operating point moves rather than presenting one
# threshold as if it were a property of the model.
COST_RATIOS = [1.0, 2.0, 3.0, 5.0]


def _mean_std(values: list[float]) -> dict[str, float]:
    clean = [v for v in values if np.isfinite(v)]
    if not clean:
        return {"mean": float("nan"), "std": float("nan")}
    return {
        "mean": round(float(statistics.fmean(clean)), 4),
        "std": round(float(statistics.pstdev(clean)) if len(clean) > 1 else 0.0, 4),
    }


# --------------------------------------------------------------------------
# No-show
# --------------------------------------------------------------------------
def evaluate_no_show(frame: pd.DataFrame, folds: list[Fold], seed: int) -> dict[str, Any]:
    per_fold: list[dict[str, Any]] = []
    pooled_true: list[np.ndarray] = []
    pooled_prob: list[np.ndarray] = []

    for fold in folds:
        train_raw, test_raw = split_by_fold(frame, fold, "appointment_date")
        assert_no_temporal_leakage(train_raw, test_raw, "appointment_date")

        resolved = train_raw[train_raw["status"].isin(["completed", "no_show"])]
        if resolved.empty:
            continue
        base_rate = float((resolved["status"] == "no_show").mean())

        X_train, y_train = features.build_no_show_features(train_raw, base_rate=base_rate)
        X_test, y_test = features.build_no_show_features(test_raw, base_rate=base_rate)
        if X_test.empty or y_test.nunique() < 2:
            continue

        model = NoShowModel(seed=seed).fit(X_train, y_train)
        prob = model.predict_proba(X_test)

        pooled_true.append(y_test.to_numpy())
        pooled_prob.append(prob)

        model_scores = metrics.classification_metrics(y_test.to_numpy(), prob, 0.5)
        base_prob = baselines.base_rate_classifier(y_train.to_numpy(), len(y_test))
        logit_prob = baselines.logistic_baseline(X_train, y_train.to_numpy(), X_test, seed=seed)

        per_fold.append(
            {
                "fold": str(fold),
                "train_rows": len(X_train),
                "test_rows": len(X_test),
                "model": model_scores.as_dict(),
                "baselines": {
                    "base_rate": metrics.classification_metrics(
                        y_test.to_numpy(), base_prob, 0.5
                    ).as_dict(),
                    "logistic_regression": metrics.classification_metrics(
                        y_test.to_numpy(), logit_prob, 0.5
                    ).as_dict(),
                },
                "feature_importance": model.feature_importance(),
            }
        )
        log.info(
            "  %s -> model PR-AUC %.3f | logistic %.3f | base rate %.3f",
            fold,
            model_scores.pr_auc,
            per_fold[-1]["baselines"]["logistic_regression"]["pr_auc"],
            per_fold[-1]["baselines"]["base_rate"]["pr_auc"],
        )

    if not per_fold:
        raise RuntimeError("no usable folds for the no-show model")

    y_all = np.concatenate(pooled_true)
    p_all = np.concatenate(pooled_prob)

    operating_points = {}
    for ratio in COST_RATIOS:
        threshold, rationale = metrics.threshold_for_cost(
            y_all, p_all, cost_false_negative=ratio, cost_false_positive=1.0
        )
        scored = metrics.classification_metrics(y_all, p_all, threshold)
        operating_points[f"fn_cost_{ratio:g}x"] = {
            "threshold": round(threshold, 3),
            "rationale": rationale,
            **scored.as_dict(),
        }

    return {
        "folds": per_fold,
        "summary": {
            "model": {
                key: _mean_std([f["model"][key] for f in per_fold])
                for key in ("pr_auc", "roc_auc", "brier", "precision", "recall", "f1")
            },
            "logistic_regression": {
                key: _mean_std([f["baselines"]["logistic_regression"][key] for f in per_fold])
                for key in ("pr_auc", "roc_auc", "brier")
            },
            "base_rate": {
                key: _mean_std([f["baselines"]["base_rate"][key] for f in per_fold])
                for key in ("pr_auc", "roc_auc", "brier")
            },
        },
        "operating_points": operating_points,
        "precision_recall_curve": metrics.precision_recall_curve_points(y_all, p_all),
        "pooled_support": len(y_all),
        "pooled_positive_rate": round(float(y_all.mean()), 4),
    }


# --------------------------------------------------------------------------
# Demand
# --------------------------------------------------------------------------
def evaluate_demand(frame: pd.DataFrame, folds: list[Fold], seed: int) -> dict[str, Any]:
    grid = features.build_demand_grid(frame)
    per_fold: list[dict[str, Any]] = []

    for fold in folds:
        train_grid, test_grid = split_by_fold(grid, fold, "appointment_date")
        assert_no_temporal_leakage(train_grid, test_grid, "appointment_date")
        if train_grid.empty or test_grid.empty:
            continue

        profile = features.demand_profile(train_grid)
        X_train, y_train = features.build_demand_features(train_grid, profile=profile)
        X_test, y_test = features.build_demand_features(test_grid, profile=profile)

        model = DemandModel(seed=seed).fit(X_train, y_train, profile=profile)
        model_scores = metrics.regression_metrics(y_test.to_numpy(), model.predict(X_test))
        naive = baselines.seasonal_naive_demand(train_grid, test_grid)
        naive_scores = metrics.regression_metrics(y_test.to_numpy(), naive)

        per_fold.append(
            {
                "fold": str(fold),
                "test_cells": len(X_test),
                "model": model_scores.as_dict(),
                "baselines": {"seasonal_naive": naive_scores.as_dict()},
                # Which candidate selection picked on this fold's TRAINING data.
                # When it is "profile", the shipped model IS the baseline, and
                # the comparison below is a tie by construction rather than a win.
                "selected_strategy": model.strategy,
                "selection_scores": model.selection_scores,
                "feature_importance": model.feature_importance(),
            }
        )
        log.info(
            "  %s -> model MAE %.4f | seasonal-naive MAE %.4f | selected: %s",
            fold,
            model_scores.mae,
            naive_scores.mae,
            model.strategy,
        )

    if not per_fold:
        raise RuntimeError("no usable folds for the demand model")

    return {
        "folds": per_fold,
        "summary": {
            "model": {
                k: _mean_std([f["model"][k] for f in per_fold])
                for k in ("mae", "rmse", "bias", "r2")
            },
            "seasonal_naive": {
                k: _mean_std([f["baselines"]["seasonal_naive"][k] for f in per_fold])
                for k in ("mae", "rmse", "bias", "r2")
            },
        },
        "zero_cell_fraction": round(float((grid["count"] == 0).mean()), 4),
        "total_cells": len(grid),
        # Which candidate selection chose on each fold. When every fold picked
        # "profile", the shipped model IS the baseline and the comparison below
        # is a tie by construction, not a win. Reporting it prevents that tie
        # from being read as the model beating the baseline.
        "selected_strategy": [f["selected_strategy"] for f in per_fold],
    }


# --------------------------------------------------------------------------
# Duration
# --------------------------------------------------------------------------
def evaluate_duration(frame: pd.DataFrame, folds: list[Fold], seed: int) -> dict[str, Any]:
    per_fold: list[dict[str, Any]] = []

    for fold in folds:
        train_raw, test_raw = split_by_fold(frame, fold, "appointment_date")
        assert_no_temporal_leakage(train_raw, test_raw, "appointment_date")

        X_train, y_train = features.build_duration_features(train_raw)
        X_test, y_test = features.build_duration_features(test_raw)
        if X_test.empty:
            continue

        model = DurationModel(seed=seed).fit(X_train, y_train)
        model_scores = metrics.regression_metrics(y_test.to_numpy(), model.predict(X_test))

        train_completed = train_raw[train_raw["status"] == "completed"]
        test_completed = test_raw[test_raw["status"] == "completed"]
        naive = baselines.mean_duration_by_specialty(train_completed, test_completed)
        naive_scores = metrics.regression_metrics(
            test_completed["duration_minutes"].to_numpy(dtype=float), naive
        )

        per_fold.append(
            {
                "fold": str(fold),
                "test_rows": len(X_test),
                "model": model_scores.as_dict(),
                "baselines": {"specialty_mean": naive_scores.as_dict()},
                "feature_importance": model.feature_importance(),
            }
        )
        log.info(
            "  %s -> model MAE %.3f min | specialty-mean MAE %.3f min",
            fold,
            model_scores.mae,
            naive_scores.mae,
        )

    if not per_fold:
        raise RuntimeError("no usable folds for the duration model")

    return {
        "folds": per_fold,
        "summary": {
            "model": {
                k: _mean_std([f["model"][k] for f in per_fold])
                for k in ("mae", "rmse", "bias", "r2")
            },
            "specialty_mean": {
                k: _mean_std([f["baselines"]["specialty_mean"][k] for f in per_fold])
                for k in ("mae", "rmse", "bias", "r2")
            },
        },
    }


# --------------------------------------------------------------------------
# Plots
# --------------------------------------------------------------------------
def write_plots(
    no_show: dict[str, Any], demand: dict[str, Any], duration: dict[str, Any]
) -> list[Path]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    curve = no_show["precision_recall_curve"]
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.plot([p["recall"] for p in curve], [p["precision"] for p in curve], color="#0d4a76", lw=2)
    ax.axhline(
        no_show["pooled_positive_rate"],
        ls="--",
        color="#74777f",
        lw=1,
        label=f"base rate ({no_show['pooled_positive_rate']:.1%})",
    )
    for name, point in no_show["operating_points"].items():
        ax.plot(
            point["recall"], point["precision"], "o", ms=6, label=f"{name} (t={point['threshold']})"
        )
    ax.set_xlabel("Recall — share of no-shows caught")
    ax.set_ylabel("Precision — share of flags that were right")
    ax.set_title("No-show classifier: the operating-point trade-off")
    ax.legend(fontsize=8)
    fig.tight_layout()
    path = REPORTS / "no_show_precision_recall.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    written.append(path)

    # Model vs baseline, one bar pair per task. The comparison IS the result.
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.8))
    axes[0].bar(
        ["model", "logistic", "base rate"],
        [
            no_show["summary"]["model"]["pr_auc"]["mean"],
            no_show["summary"]["logistic_regression"]["pr_auc"]["mean"],
            no_show["summary"]["base_rate"]["pr_auc"]["mean"],
        ],
        color=["#0d4a76", "#2a7f8f", "#c4c6cf"],
    )
    axes[0].set_title("No-show: PR-AUC (higher better)")
    axes[1].bar(
        ["model", "seasonal naive"],
        [
            demand["summary"]["model"]["mae"]["mean"],
            demand["summary"]["seasonal_naive"]["mae"]["mean"],
        ],
        color=["#0d4a76", "#c4c6cf"],
    )
    axes[1].set_title("Demand: MAE (lower better)")
    axes[2].bar(
        ["model", "specialty mean"],
        [
            duration["summary"]["model"]["mae"]["mean"],
            duration["summary"]["specialty_mean"]["mae"]["mean"],
        ],
        color=["#0d4a76", "#c4c6cf"],
    )
    axes[2].set_title("Duration: MAE minutes (lower better)")
    for ax in axes:
        ax.tick_params(labelsize=8)
    fig.tight_layout()
    path = REPORTS / "model_vs_baseline.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    written.append(path)

    importance = no_show["folds"][-1]["feature_importance"]
    top = list(importance.items())[:12][::-1]
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.barh([k for k, _ in top], [v for _, v in top], color="#0d4a76")
    ax.set_title("No-show: feature importance (gain share, last fold)")
    ax.tick_params(labelsize=8)
    fig.tight_layout()
    path = REPORTS / "no_show_feature_importance.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    written.append(path)

    return written


# --------------------------------------------------------------------------
# Gate
# --------------------------------------------------------------------------
def check_gates(
    no_show: dict[str, Any], demand: dict[str, Any], duration: dict[str, Any]
) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []

    model_pr = no_show["summary"]["model"]["pr_auc"]["mean"]
    base_pr = no_show["summary"]["base_rate"]["pr_auc"]["mean"]
    logit_pr = no_show["summary"]["logistic_regression"]["pr_auc"]["mean"]
    ratio = model_pr / base_pr if base_pr else float("inf")
    checks.append(
        (
            "no-show beats the base rate on PR-AUC",
            ratio >= GATES["no_show_beats_base_rate_pr_auc"],
            f"{model_pr:.3f} vs {base_pr:.3f} ({ratio:.2f}x, need >={GATES['no_show_beats_base_rate_pr_auc']}x)",
        )
    )
    checks.append(
        (
            "no-show ROC-AUC above the floor",
            no_show["summary"]["model"]["roc_auc"]["mean"] >= GATES["no_show_min_roc_auc"],
            f"{no_show['summary']['model']['roc_auc']['mean']:.3f} (need >={GATES['no_show_min_roc_auc']})",
        )
    )
    # Not a gate: reported so the comparison cannot be quietly omitted. If the
    # boosted model does not beat logistic regression, the writeup has to say so.
    checks.append(
        (
            "no-show beats logistic regression (informational)",
            True,
            f"model {model_pr:.3f} vs logistic {logit_pr:.3f} -> "
            + ("model wins" if model_pr > logit_pr else "LOGISTIC WINS: prefer the simpler model"),
        )
    )

    d_model = demand["summary"]["model"]["mae"]["mean"]
    d_naive = demand["summary"]["seasonal_naive"]["mae"]["mean"]
    strategies = set(demand.get("selected_strategy", []))
    picked = "/".join(sorted(s for s in strategies if s))
    note = (
        " (selection chose the profile: the simple estimator IS the baseline here)"
        if strategies == {"profile"}
        else ""
    )
    checks.append(
        (
            "demand is at least as good as the seasonal-naive profile",
            d_model <= d_naive * GATES["demand_beats_seasonal_naive_mae"],
            f"MAE {d_model:.4f} vs naive {d_naive:.4f}; selected [{picked}]{note}",
        )
    )

    u_model = duration["summary"]["model"]["mae"]["mean"]
    u_naive = duration["summary"]["specialty_mean"]["mae"]["mean"]
    checks.append(
        (
            "duration beats the specialty mean",
            u_model <= u_naive * GATES["duration_beats_specialty_mean_mae"],
            f"MAE {u_model:.3f} min vs mean {u_naive:.3f} min",
        )
    )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--test-days", type=int, default=90)
    args = parser.parse_args()

    engine = create_engine(settings.database_url_sync, future=True)
    frame = load_appointments(engine)
    summary = training_summary(frame)
    log.info("data: %s", json.dumps(summary))
    if summary["resolved_rows"] < 2000:
        log.error("not enough resolved appointments; run scripts/generate_data.py first")
        return 1

    folds = rolling_origin_folds(
        frame["appointment_date"], n_folds=args.folds, test_days=args.test_days
    )
    log.info("rolling-origin folds:")
    for fold in folds:
        log.info("  %s", fold)

    log.info("evaluating no-show classifier...")
    no_show = evaluate_no_show(frame, folds, args.seed)
    log.info("evaluating demand model...")
    demand = evaluate_demand(frame, folds, args.seed)
    log.info("evaluating duration model...")
    duration = evaluate_duration(frame, folds, args.seed)

    REPORTS.mkdir(parents=True, exist_ok=True)
    payload = {
        "data": summary,
        "seed": args.seed,
        "validation": {
            "scheme": "rolling-origin (expanding window)",
            "folds": len(folds),
            "test_days_per_fold": args.test_days,
        },
        "no_show": no_show,
        "demand": demand,
        "duration": duration,
    }
    (REPORTS / "metrics.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    plots = write_plots(no_show, demand, duration)

    checks = check_gates(no_show, demand, duration)

    print("\n" + "=" * 78)
    print("PHASE 3 MODEL EVALUATION")
    print("=" * 78)
    print(f"validation : rolling-origin, {len(folds)} folds x {args.test_days}d test windows")
    print(
        f"data       : {summary['resolved_rows']} resolved appointments, "
        f"{summary['date_range'][0]} .. {summary['date_range'][1]}"
    )
    print("-" * 78)
    print("NO-SHOW CLASSIFIER            mean +/- std over folds")
    for name, key in (
        ("model", "model"),
        ("logistic regression", "logistic_regression"),
        ("base rate", "base_rate"),
    ):
        block = no_show["summary"][key]
        print(
            f"  {name:<22} PR-AUC {block['pr_auc']['mean']:.3f} +/- {block['pr_auc']['std']:.3f}"
            f"   ROC-AUC {block['roc_auc']['mean']:.3f}   Brier {block['brier']['mean']:.4f}"
        )
    print("\n  operating points (pooled across folds):")
    for name, point in no_show["operating_points"].items():
        print(
            f"    {name:<14} t={point['threshold']:.2f}  precision {point['precision']:.3f}"
            f"  recall {point['recall']:.3f}  F1 {point['f1']:.3f}"
        )
    print("-" * 78)
    d, dn = demand["summary"]["model"], demand["summary"]["seasonal_naive"]
    print(
        f"DEMAND (per specialty/date/hour cell, {demand['zero_cell_fraction']:.0%} of cells empty)"
    )
    print(
        f"  model                  MAE {d['mae']['mean']:.4f} +/- {d['mae']['std']:.4f}"
        f"   RMSE {d['rmse']['mean']:.4f}   bias {d['bias']['mean']:+.4f}"
    )
    print(f"  seasonal-naive profile MAE {dn['mae']['mean']:.4f}   RMSE {dn['rmse']['mean']:.4f}")
    print(f"  selected per fold      {demand.get('selected_strategy')}")
    print("-" * 78)
    u, un = duration["summary"]["model"], duration["summary"]["specialty_mean"]
    print("DURATION (minutes)")
    print(
        f"  model                  MAE {u['mae']['mean']:.3f} +/- {u['mae']['std']:.3f}"
        f"   RMSE {u['rmse']['mean']:.3f}   bias {u['bias']['mean']:+.3f}"
    )
    print(f"  specialty mean         MAE {un['mae']['mean']:.3f}   RMSE {un['rmse']['mean']:.3f}")
    print("=" * 78)
    for name, passed, detail in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}")
    for path in plots:
        print(f"plot: {path.relative_to(path.parent.parent.parent)}")
    print(f"metrics: {(REPORTS / 'metrics.json').relative_to(REPORTS.parent.parent)}")
    print("=" * 78)

    failed = [c for c in checks if not c[1]]
    if failed:
        print(f"\nGATE FAILED: {len(failed)} check(s) did not pass.")
        return 1
    print(f"\nGATE PASSED: all {len(checks)} checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
