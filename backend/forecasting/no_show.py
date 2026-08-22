"""No-show classifier.

Two estimators over the booking-time features in `features.py`: gradient
boosting and regularised logistic regression. Which one ships is decided by
`select_estimator`, on training data only.

**Why both.** The first evaluation put boosting behind logistic regression on
every measure that matters (PR-AUC 0.301 vs 0.323, Brier 0.160 vs 0.158). That
is not a fluke to be tuned away — the underlying no-show process is a logistic
function of the features, so a linear model in log-odds space is *correctly
specified* and boosting is approximating it with step functions. Adding
capacity would fit noise, not signal.

Keeping both, and selecting between them by cross-validation, is the honest
resolution: it means the shipped model is whichever actually wins on this data,
and the comparison is recorded in the model card rather than quietly dropped.
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import TimeSeriesSplit, cross_val_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


def _influence(estimator: object) -> np.ndarray | None:
    """Pull a per-feature influence vector out of either estimator type."""
    if hasattr(estimator, "feature_importances_"):
        return np.asarray(estimator.feature_importances_, dtype=float)
    # A Pipeline: the coefficients live on its final step, and because the
    # features were standardised first, they are directly comparable.
    final = estimator[-1] if hasattr(estimator, "__getitem__") else estimator
    if hasattr(final, "coef_"):
        return np.asarray(final.coef_, dtype=float).ravel()
    return None


EstimatorKind = Literal["gradient_boosting", "logistic_regression"]

DEFAULT_PARAMS: dict[str, Any] = {
    # Small trees and strong regularisation. The dataset is ~31k rows with
    # ~17 features and a genuine but modest signal; a deep forest would fit the
    # noise and score worse on the next quarter.
    "n_estimators": 400,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "max_depth": 6,
    "min_child_samples": 40,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "verbose": -1,
}


class NoShowModel:
    """Predicts P(no-show) for an appointment, as known at booking time.

    **Calibration.** The raw booster ranks well but its probabilities are not
    trustworthy as probabilities. That matters here because the downstream use
    is not just "flag the risky ones": the optimizer wants an *expected* number
    of no-shows for a session, which means summing probabilities. Summing
    uncalibrated scores gives a number with no meaning. Isotonic calibration on
    a held-out slice fixes the mapping from score to probability, and the Brier
    score in the eval harness reports whether it worked.

    **Class weighting is deliberately left at default.** A 22% positive rate is
    imbalanced but not severely so, and `scale_pos_weight` would inflate the
    predicted probabilities away from the true rate — trading the calibration
    that was just paid for in exchange for a threshold shift that is better
    expressed directly as a threshold.
    """

    def __init__(
        self,
        *,
        seed: int = 42,
        params: dict[str, Any] | None = None,
        estimator: EstimatorKind = "logistic_regression",
    ) -> None:
        self.seed = seed
        self.estimator = estimator
        self.params = (
            {**DEFAULT_PARAMS, **(params or {})} if estimator == "gradient_boosting" else {}
        )
        self.feature_names: list[str] = []
        self._model: CalibratedClassifierCV | LGBMClassifier | None = None

    def _base_estimator(self) -> Any:
        if self.estimator == "gradient_boosting":
            return LGBMClassifier(random_state=self.seed, **self.params)
        # Scaling matters here: the features span days, counts and 0/1 flags, and
        # an unscaled L2 penalty would fall almost entirely on the small-valued
        # ones.
        return make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=2000, random_state=self.seed, C=1.0),
        )

    def fit(self, X: pd.DataFrame, y: pd.Series, *, calibrate: bool = True) -> NoShowModel:
        self.feature_names = list(X.columns)
        booster = self._base_estimator()

        if calibrate and len(X) >= 500 and y.nunique() > 1:
            # cv=3 refits internally on folds of the training data only, so the
            # test window stays untouched. Isotonic rather than sigmoid because
            # it makes no shape assumption and there is ample data to fit it.
            self._model = CalibratedClassifierCV(booster, method="isotonic", cv=3)
            self._model.fit(X.fillna(0.0), y)
        else:
            booster.fit(X.fillna(0.0), y)
            self._model = booster
        return self

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        if self._model is None:
            raise RuntimeError("model has not been fitted")
        aligned = X.reindex(columns=self.feature_names).fillna(0.0)
        # np.asarray because scikit-learn's declared return type is a union
        # that includes list; the concrete estimators here always return an
        # array, and this makes that explicit rather than asserted.
        proba = np.asarray(self._model.predict_proba(aligned), dtype=float)
        return proba[:, 1]

    def predict(self, X: pd.DataFrame, threshold: float) -> np.ndarray:
        return (self.predict_proba(X) >= threshold).astype(int)

    def feature_importance(self) -> dict[str, float]:
        """Per-feature influence, normalised to sum to 1.

        The two estimators expose different things, so this reports whichever
        the fitted one has:

        * gradient boosting -> split gain, averaged over calibration folds
        * logistic regression -> absolute standardised coefficient

        They are not the same quantity and should not be compared across
        estimators, but within one they answer the question this is here for:
        is the model leaning on the features the generator actually encoded? If
        lead time and patient history are NOT near the top, something is wrong
        upstream in the features.
        """
        if self._model is None or not self.feature_names:
            return {}

        estimators = []
        if isinstance(self._model, CalibratedClassifierCV):
            for calibrated in self._model.calibrated_classifiers_:
                inner = getattr(calibrated, "estimator", None)
                if inner is not None:
                    estimators.append(inner)
        else:
            estimators.append(self._model)
        if not estimators:
            return {}

        totals = np.zeros(len(self.feature_names), dtype=float)
        counted = 0
        for est in estimators:
            values = _influence(est)
            if values is not None and len(values) == len(self.feature_names):
                totals += np.abs(values)
                counted += 1
        if counted == 0:
            return {}

        totals /= counted
        total = totals.sum() or 1.0
        return {
            name: round(float(value / total), 4)
            for name, value in sorted(
                zip(self.feature_names, totals, strict=True), key=lambda kv: -kv[1]
            )
        }


def select_estimator(
    X: pd.DataFrame, y: pd.Series, *, seed: int = 42, n_splits: int = 3
) -> tuple[EstimatorKind, dict[str, float]]:
    """Pick the estimator by cross-validation on TRAINING data.

    Uses `TimeSeriesSplit`, not `KFold`. The rows are chronological, so a random
    K-fold would select a model using folds that train on the future — the same
    mistake the outer evaluation is careful to avoid, and it would be no less
    wrong for happening inside model selection.

    Selection never touches the evaluation folds. Choosing an estimator by
    looking at test scores is a slower way of overfitting the test set, and it
    would make the reported metrics an optimistic estimate of nothing.

    Scored on average precision (PR-AUC), which is the measure that matters for
    a 22%-positive class.
    """
    splitter = TimeSeriesSplit(n_splits=n_splits)
    kinds: tuple[EstimatorKind, ...] = ("logistic_regression", "gradient_boosting")
    scores: dict[EstimatorKind, float] = {}
    for kind in kinds:
        model = NoShowModel(seed=seed, estimator=kind)
        fold_scores = cross_val_score(
            model._base_estimator(),
            X.fillna(0.0),
            y,
            cv=splitter,
            scoring="average_precision",
        )
        scores[kind] = float(np.mean(fold_scores))

    winner = max(kinds, key=lambda k: scores[k])
    return winner, {str(k): round(v, 4) for k, v in scores.items()}
