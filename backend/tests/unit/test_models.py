"""Unit tests for the model wrappers, metrics, and baselines.

No database and no real training data — these check the machinery is correct on
inputs whose right answer is known by construction. The question of whether the
models are any *good* on real data is `scripts/evaluate.py`, which is a gate;
this is the layer below that.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forecasting import baselines, metrics
from forecasting.demand import DemandModel
from forecasting.duration import DurationModel
from forecasting.no_show import NoShowModel, select_estimator


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
def test_classification_metrics_on_a_hand_checkable_case() -> None:
    y_true = np.array([1, 1, 0, 0])
    y_prob = np.array([0.9, 0.4, 0.6, 0.1])
    m = metrics.classification_metrics(y_true, y_prob, threshold=0.5)

    # Predicted positive: rows 0 and 2 -> one right, one wrong.
    assert (m.true_positives, m.false_positives) == (1, 1)
    assert (m.false_negatives, m.true_negatives) == (1, 1)
    assert m.precision == pytest.approx(0.5)
    assert m.recall == pytest.approx(0.5)
    assert m.f1 == pytest.approx(0.5)
    assert m.support == 4


def test_perfect_and_inverted_classifiers_bracket_the_scale() -> None:
    y_true = np.array([1, 1, 0, 0])
    assert metrics.classification_metrics(y_true, y_true.astype(float), 0.5).roc_auc == 1.0
    assert metrics.classification_metrics(y_true, 1.0 - y_true, 0.5).roc_auc == 0.0


def test_brier_rewards_calibration_not_just_ranking() -> None:
    """Two models with identical ranking and very different honesty.

    Both order the cases perfectly, so ROC-AUC cannot tell them apart. Brier
    can — which is why it is reported, since the optimizer will sum these
    probabilities and needs them to mean something.
    """
    y_true = np.array([1, 0, 1, 0])
    calibrated = np.array([0.9, 0.1, 0.8, 0.2])
    overconfident = np.array([1.0, 0.0, 1.0, 0.0])

    assert metrics.classification_metrics(y_true, calibrated, 0.5).roc_auc == 1.0
    assert metrics.classification_metrics(y_true, overconfident, 0.5).roc_auc == 1.0
    assert (
        metrics.classification_metrics(y_true, overconfident, 0.5).brier
        < metrics.classification_metrics(y_true, calibrated, 0.5).brier
    )


def test_regression_metrics_separate_error_from_bias() -> None:
    """MAE alone cannot tell "noisy" from "systematically wrong"."""
    y_true = np.array([10.0, 10.0, 10.0, 10.0])
    noisy = np.array([8.0, 12.0, 8.0, 12.0])  # centred
    biased = np.array([8.0, 8.0, 8.0, 8.0])  # always 2 low

    noisy_m = metrics.regression_metrics(y_true, noisy)
    biased_m = metrics.regression_metrics(y_true, biased)

    assert noisy_m.mae == pytest.approx(biased_m.mae)  # identical MAE
    assert noisy_m.bias == pytest.approx(0.0)
    assert biased_m.bias == pytest.approx(-2.0)  # bias tells them apart


def test_rmse_punishes_a_single_large_miss_more_than_mae() -> None:
    y_true = np.zeros(10)
    spread = np.full(10, 1.0)
    one_big = np.concatenate([np.zeros(9), [10.0]])

    spread_m = metrics.regression_metrics(y_true, spread)
    big_m = metrics.regression_metrics(y_true, one_big)

    assert big_m.mae == pytest.approx(spread_m.mae)
    assert big_m.rmse > spread_m.rmse


def test_higher_false_negative_cost_lowers_the_threshold() -> None:
    """The operating point must respond to the stated cost, in the right direction.

    Weighting missed no-shows more heavily should make the model flag MORE, not
    fewer. A threshold that moved the other way would be a sign error, and the
    resulting policy would be exactly backwards.
    """
    rng = np.random.default_rng(0)
    y_true = rng.binomial(1, 0.25, size=2000)
    y_prob = np.clip(0.25 + 0.35 * (y_true - 0.25) + rng.normal(0, 0.15, size=2000), 0.01, 0.99)

    cheap, _ = metrics.threshold_for_cost(
        y_true, y_prob, cost_false_negative=1, cost_false_positive=1
    )
    dear, _ = metrics.threshold_for_cost(
        y_true, y_prob, cost_false_negative=5, cost_false_positive=1
    )
    assert dear < cheap


def test_precision_recall_curve_recall_decreases_with_threshold() -> None:
    rng = np.random.default_rng(1)
    y_true = rng.binomial(1, 0.3, size=500)
    y_prob = rng.uniform(size=500)
    points = metrics.precision_recall_curve_points(y_true, y_prob, n_points=10)

    recalls = [p["recall"] for p in points]
    assert recalls == sorted(recalls, reverse=True)


# --------------------------------------------------------------------------
# Baselines
# --------------------------------------------------------------------------
def test_base_rate_classifier_has_no_discrimination() -> None:
    """The floor, and the reason a model beating it means something."""
    y_train = np.array([1, 0, 0, 0])
    y_test = np.array([1, 0, 1, 0])
    prob = baselines.base_rate_classifier(y_train, len(y_test))

    assert np.allclose(prob, 0.25)
    assert metrics.classification_metrics(y_test, prob, 0.5).roc_auc == pytest.approx(0.5)


def test_specialty_mean_baseline_uses_training_means_only() -> None:
    train = pd.DataFrame(
        {"specialty": ["a", "a", "b", "b"], "duration_minutes": [10.0, 20.0, 40.0, 60.0]}
    )
    test = pd.DataFrame({"specialty": ["a", "b", "c"], "duration_minutes": [0.0, 0.0, 0.0]})
    predictions = baselines.mean_duration_by_specialty(train, test)

    assert predictions[0] == pytest.approx(15.0)
    assert predictions[1] == pytest.approx(50.0)
    # An unseen specialty falls back to the overall mean rather than NaN.
    assert predictions[2] == pytest.approx(32.5)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
def _separable_frame(n: int = 800, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    """A dataset with one genuinely predictive feature and one pure noise column."""
    rng = np.random.default_rng(seed)
    signal = rng.normal(size=n)
    noise = rng.normal(size=n)
    probability = 1 / (1 + np.exp(-(1.8 * signal)))
    y = rng.binomial(1, probability)
    return pd.DataFrame({"signal": signal, "noise": noise}), pd.Series(y)


def test_no_show_model_learns_a_learnable_signal() -> None:
    X, y = _separable_frame()
    model = NoShowModel(seed=0).fit(X.iloc[:600], y.iloc[:600])
    prob = model.predict_proba(X.iloc[600:])

    auc = metrics.classification_metrics(y.iloc[600:].to_numpy(), prob, 0.5).roc_auc
    assert auc > 0.75, f"should recover a strong synthetic signal, got AUC {auc}"


def test_no_show_probabilities_are_in_range() -> None:
    X, y = _separable_frame()
    prob = NoShowModel(seed=0).fit(X, y).predict_proba(X)
    assert prob.min() >= 0.0 and prob.max() <= 1.0


def test_no_show_ranks_the_signal_feature_above_noise() -> None:
    X, y = _separable_frame()
    importance = NoShowModel(seed=0).fit(X, y).feature_importance()
    assert importance["signal"] > importance["noise"]


def test_predicting_before_fitting_is_an_error_not_a_wrong_answer() -> None:
    with pytest.raises(RuntimeError, match="not been fitted"):
        NoShowModel().predict_proba(pd.DataFrame({"a": [1.0]}))
    with pytest.raises(RuntimeError, match="not been fitted"):
        DemandModel().predict(pd.DataFrame({"a": [1.0]}))
    with pytest.raises(RuntimeError, match="not been fitted"):
        DurationModel().predict(pd.DataFrame({"a": [1.0]}))


def test_estimator_selection_prefers_the_better_model() -> None:
    """Selection must pick a winner on training data alone.

    On a cleanly logistic signal the linear model should win or tie; what is
    asserted here is that selection returns a valid choice with scores for both,
    so the decision is recorded rather than assumed.
    """
    X, y = _separable_frame(n=1200)
    winner, scores = select_estimator(X, y, seed=0, n_splits=3)
    assert winner in {"logistic_regression", "gradient_boosting"}
    assert set(scores) == {"logistic_regression", "gradient_boosting"}
    assert scores[winner] == max(scores.values())


def test_demand_model_never_predicts_negative_counts() -> None:
    """A negative appointment count is not a prediction anyone can act on."""
    rng = np.random.default_rng(0)
    X = pd.DataFrame({"weekday": rng.integers(0, 7, 400), "hour_of_day": rng.integers(8, 19, 400)})
    y = pd.Series(rng.poisson(0.4, 400).astype(float))

    predictions = DemandModel(seed=0).fit(X, y).predict(X)
    assert (predictions >= 0).all()


def test_duration_predictions_stay_within_schema_limits() -> None:
    """Clamped so a prediction can never produce a row the database rejects."""
    rng = np.random.default_rng(0)
    X = pd.DataFrame(
        {"specialty_code": rng.integers(0, 5, 300), "is_new_patient": rng.integers(0, 2, 300)}
    )
    y = pd.Series(rng.normal(30, 8, 300))

    predictions = DurationModel(seed=0).fit(X, y).predict(X)
    assert predictions.min() >= 5.0
    assert predictions.max() <= 240.0


def test_models_are_deterministic_for_a_given_seed() -> None:
    """Reproducibility is a requirement, not a nicety: the committed metrics
    must be regenerable from the committed code."""
    X, y = _separable_frame()
    first = NoShowModel(seed=7).fit(X, y).predict_proba(X)
    second = NoShowModel(seed=7).fit(X, y).predict_proba(X)
    assert np.allclose(first, second)
