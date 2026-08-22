"""Consultation-duration model.

Predicts how long an appointment will actually take, so the optimizer can
allocate a slot that fits instead of assuming the specialty default.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from lightgbm import LGBMRegressor

DEFAULT_PARAMS: dict[str, Any] = {
    # L1 (mean absolute error), not L2. Consultation length is right-skewed:
    # most visits sit near the specialty norm and a few run long. Squared error
    # chases those tails and inflates every ordinary prediction to hedge against
    # them, which in scheduling terms means padding every slot for the rare long
    # case. L1 fits the typical appointment, and the rare overrun is better
    # handled by the optimizer overtime term than by systematic padding.
    "objective": "regression_l1",
    "n_estimators": 400,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "max_depth": 6,
    "min_child_samples": 40,
    "subsample": 0.9,
    "subsample_freq": 1,
    "colsample_bytree": 0.9,
    "reg_lambda": 1.0,
    "verbose": -1,
}

# Clamp to the range the schema permits, so a prediction can never produce an
# appointment the database would reject.
MIN_MINUTES = 5.0
MAX_MINUTES = 240.0


class DurationModel:
    """Expected consultation length in minutes.

    Trained only on COMPLETED appointments. A no-show has no consultation, so
    its `duration_minutes` is the slot that was booked rather than a length
    anyone observed; including those rows would teach the model to reproduce the
    planned duration it is supposed to improve on.
    """

    def __init__(self, *, seed: int = 42, params: dict[str, Any] | None = None) -> None:
        self.seed = seed
        self.params = {**DEFAULT_PARAMS, **(params or {})}
        self.feature_names: list[str] = []
        self._model: LGBMRegressor | None = None

    def fit(self, X: pd.DataFrame, y: pd.Series) -> DurationModel:
        self.feature_names = list(X.columns)
        self._model = LGBMRegressor(random_state=self.seed, **self.params)
        self._model.fit(X.fillna(0.0), y)
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        if self._model is None:
            raise RuntimeError("model has not been fitted")
        aligned = X.reindex(columns=self.feature_names).fillna(0.0)
        return np.clip(self._model.predict(aligned), MIN_MINUTES, MAX_MINUTES)

    def feature_importance(self) -> dict[str, float]:
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
