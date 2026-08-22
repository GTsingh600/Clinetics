"""The baselines every model must beat.

Same principle as the optimizer's greedy comparison in Phase 4: a metric with
nothing to compare against is unfalsifiable. "ROC-AUC 0.72" sounds respectable
until you learn that a two-feature logistic regression scores 0.71 — at which
point the gradient boosting was not worth its complexity.

Each baseline is deliberately something a competent person would try first, so
beating it means something.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


def base_rate_classifier(y_train: np.ndarray, n_test: int) -> np.ndarray:
    """Predict the training base rate for everyone.

    The floor. Any model that cannot beat this has learned nothing. ROC-AUC is
    exactly 0.5 by construction, because every prediction ties.
    """
    return np.full(n_test, float(np.mean(y_train)))


def logistic_baseline(
    X_train: pd.DataFrame, y_train: np.ndarray, X_test: pd.DataFrame, *, seed: int = 42
) -> np.ndarray:
    """Regularised logistic regression on the same features.

    The honest comparison, and a deliberately hard one: the generator produced
    no-shows from a logistic model, so this baseline is well specified for the
    data. If gradient boosting only matches it, the correct conclusion is that
    the extra machinery is not earning its place — and the writeup should say so
    rather than quietly reporting only the boosted number.
    """
    model = make_pipeline(
        StandardScaler(),
        LogisticRegression(max_iter=1000, random_state=seed, C=1.0),
    )
    model.fit(X_train.fillna(0.0), y_train)
    return model.predict_proba(X_test.fillna(0.0))[:, 1]


def seasonal_naive_demand(train_grid: pd.DataFrame, test_grid: pd.DataFrame) -> np.ndarray:
    """Predict each cell as its historical mean for that (specialty, weekday, hour).

    The strong baseline for demand. Most of the generator's structure is a
    weekday-by-hour profile per specialty, so this captures the bulk of the
    signal with no model at all. Beating it requires the model to contribute
    something the plain average misses — seasonality, or an interaction.
    """
    train = train_grid.copy()
    train["weekday"] = pd.to_datetime(train["appointment_date"]).dt.weekday
    profile = train.groupby(["specialty", "weekday", "hour_of_day"])["count"].mean()

    test = test_grid.copy()
    test["weekday"] = pd.to_datetime(test["appointment_date"]).dt.weekday
    keys = pd.MultiIndex.from_arrays([test["specialty"], test["weekday"], test["hour_of_day"]])
    overall = float(train["count"].mean())
    return profile.reindex(keys).fillna(overall).to_numpy(dtype=float)


def mean_duration_by_specialty(train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    """Predict each specialty's mean consultation length.

    What a scheduler does today: one standard slot length per specialty. The
    duration model has to beat this to justify predicting per appointment at
    all.
    """
    means = train.groupby("specialty")["duration_minutes"].mean()
    overall = float(train["duration_minutes"].mean())
    return test["specialty"].map(means).fillna(overall).to_numpy(dtype=float)
