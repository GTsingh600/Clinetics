"""Hourly demand model.

Predicts appointment count for a (specialty, date, hour) cell — the exact grain
of the `analytics.forecast` table, so a prediction maps to a row without
reshaping.

**Two candidates, selected on training data.** Gradient boosting over calendar
features, and the plain (specialty, weekday, hour) historical mean.

The second is not a strawman. Demand at this clinic is close to *exactly* a
weekday-by-hour profile per specialty scaled by a mild annual cycle, so the
average is nearly the true generating process — and evaluation bore that out.
The boosted model and the profile finished level on MAE (0.482 vs 0.482), with
the model ahead on RMSE (0.730 vs 0.734) and near-zero bias.

Rather than declare a winner by eye — or, worse, pick whichever looked better on
the test folds — `fit` compares them on a time-series split of the *training*
data and keeps the better one. When the simple estimator wins, the simple
estimator ships. That is the same discipline used for the no-show classifier,
and it is what stops the reported model-versus-baseline comparison being
circular.
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor
from sklearn.model_selection import TimeSeriesSplit

Strategy = Literal["gbm", "profile"]

DEFAULT_PARAMS: dict[str, Any] = {
    # Poisson, not squared error. The target is a count of arrivals in a fixed
    # interval: non-negative, integer, and with variance that grows with the
    # mean. Squared error assumes constant-variance symmetric noise, so it
    # under-weights errors on busy hours and will happily predict -0.3
    # appointments for a Sunday morning.
    "objective": "poisson",
    # Deliberately small. The feature space is tiny — 5 specialties x 7 weekdays
    # x 11 hours plus smooth seasonality — so a large forest has far more
    # capacity than there are distinct patterns, and spends it fitting the
    # Poisson noise in individual cells.
    "n_estimators": 300,
    "learning_rate": 0.05,
    "num_leaves": 24,
    "max_depth": 6,
    "min_child_samples": 100,
    "subsample": 0.9,
    "subsample_freq": 1,
    "colsample_bytree": 0.9,
    "reg_lambda": 1.0,
    "verbose": -1,
}


def select_strategy(
    X: pd.DataFrame,
    y: pd.Series,
    *,
    booster_params: dict[str, Any],
    seed: int,
    n_splits: int = 3,
) -> tuple[Strategy, dict[str, float]]:
    """Compare the boosted model against the profile, on training data alone.

    `TimeSeriesSplit`, not `KFold`: these rows are chronological, and a random
    fold would train on the future — the same error the outer evaluation takes
    care to avoid, and no less wrong for happening inside model selection.
    """
    splitter = TimeSeriesSplit(n_splits=n_splits)
    gbm_errors: list[float] = []
    profile_errors: list[float] = []

    for train_idx, test_idx in splitter.split(X):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]

        booster = LGBMRegressor(random_state=seed, **booster_params)
        booster.fit(X_train, y_train)
        predicted = np.clip(booster.predict(X_test), 0.0, None)
        actual = y_test.to_numpy(dtype=float)
        gbm_errors.append(float(np.mean(np.abs(predicted - actual))))
        profile_errors.append(
            float(np.mean(np.abs(X_test["profile_mean"].to_numpy(dtype=float) - actual)))
        )

    gbm_mae = float(np.mean(gbm_errors))
    profile_mae = float(np.mean(profile_errors))
    scores = {"gbm_mae": round(gbm_mae, 4), "profile_mae": round(profile_mae, 4)}
    return ("gbm" if gbm_mae < profile_mae else "profile"), scores


class DemandModel:
    """Expected appointments per (specialty, date, hour).

    Trained on a ZERO-FILLED grid — see `features.build_demand_grid`. Training
    only on cells that had appointments is the classic version of this mistake:
    the model never observes a quiet hour, so it learns demand is always at
    least one and over-predicts every empty slot in the calendar.

    No lagged features, deliberately. Lags would sharpen next-week accuracy, but
    the optimizer asks for a date weeks out, where lag values do not exist yet.
    A model that cannot be evaluated the way it will be used is not worth the
    extra accuracy.
    """

    def __init__(self, *, seed: int = 42, params: dict[str, Any] | None = None) -> None:
        self.seed = seed
        self.params = {**DEFAULT_PARAMS, **(params or {})}
        self.feature_names: list[str] = []
        # The (specialty, weekday, hour) profile behind the `profile_mean`
        # feature. Carried ON the model, not recomputed at inference: serving
        # would otherwise rebuild it from whatever happens to be in the database
        # and feed the model a different encoding than it was fitted against.
        self.profile: pd.Series | None = None
        # Which candidate won selection. Recorded so the model card states what
        # actually shipped rather than what was hoped for.
        self.strategy: Strategy | None = None
        self.selection_scores: dict[str, float] = {}
        self._model: LGBMRegressor | None = None

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        *,
        profile: pd.Series | None = None,
        strategy: Strategy | Literal["auto"] = "auto",
    ) -> DemandModel:
        self.feature_names = list(X.columns)
        self.profile = profile

        if "profile_mean" not in X.columns:
            # Without the profile feature there is nothing to select between.
            self.strategy = "gbm"
        elif strategy in ("gbm", "profile"):
            self.strategy = strategy
        else:
            self.strategy, self.selection_scores = select_strategy(
                X, y, booster_params=self.params, seed=self.seed
            )

        if self.strategy == "gbm":
            booster = LGBMRegressor(random_state=self.seed, **self.params)
            booster.fit(X, y)
            self._model = booster
        else:
            # "Fitting" the profile estimator is a no-op: the profile is already
            # materialised as a feature column, and prediction reads it.
            self._model = None
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        if self.strategy is None:
            raise RuntimeError("model has not been fitted")
        aligned = X.reindex(columns=self.feature_names)

        if self.strategy == "profile":
            raw = aligned["profile_mean"].to_numpy(dtype=float)
        else:
            if self._model is None:
                raise RuntimeError("model has not been fitted")
            raw = np.asarray(self._model.predict(aligned), dtype=float)

        # Clip at zero: the Poisson objective makes negatives unlikely rather
        # than impossible, and a negative appointment count is not a prediction
        # anyone can act on.
        return np.clip(raw, 0.0, None)

    def feature_importance(self) -> dict[str, float]:
        if self.strategy == "profile":
            # The profile estimator has exactly one input, by construction.
            return {"profile_mean": 1.0}
        if self._model is None:
            return {}
        values = np.asarray(self._model.feature_importances_, dtype=float)
        total = values.sum() or 1.0
        return {
            name: round(float(v / total), 4)
            for name, v in sorted(
                zip(self.feature_names, values, strict=True), key=lambda kv: -kv[1]
            )
        }
