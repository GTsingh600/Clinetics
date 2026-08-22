"""Temporal splitting. The one thing that must not be got wrong.

A random train/test split on this data would be meaningless. Appointments are
ordered in time, and a random split puts *later* appointments in the training
set and *earlier* ones in the test set — so the model is scored on its ability
to predict a past it has already seen the future of. Reported accuracy goes up
and real accuracy goes down, which is the worst possible combination because
the number looks better while the model gets worse.

Every split here is by date, and every training window ends strictly before its
test window begins.

**Rolling origin.** Rather than one arbitrary cutoff, several folds each train
on everything before a cutoff and test on the window after it:

    fold 1  train [start .............. c1)  test [c1 .. c2)
    fold 2  train [start ................... c2)  test [c2 .. c3)
    fold 3  train [start ........................ c3)  test [c3 .. c4)

The training window grows; it is never shuffled and never reaches past its
cutoff. This yields a mean *and* a spread per metric, so a lucky cutoff is
visible as high variance instead of being reported as a result.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from forecasting.types import Fold


def rolling_origin_folds(
    dates: pd.Series,
    *,
    n_folds: int = 4,
    test_days: int = 90,
    min_train_days: int = 270,
) -> list[Fold]:
    """Build `n_folds` expanding-window folds ending at the data's last date.

    Args:
        dates: any date-like series covering the modelling period.
        n_folds: how many folds. More folds means a better variance estimate
            and less training data in the earliest fold.
        test_days: length of each test window.
        min_train_days: refuse to build a fold whose training window is shorter
            than this. A model trained on two months of data and scored on the
            next quarter tells you about the sample size, not the model.

    Raises:
        ValueError: if the data cannot support the requested folds. Failing is
            correct here — silently returning fewer folds would make the
            reported "mean of 4 folds" a lie.
    """
    if n_folds < 1:
        raise ValueError("n_folds must be at least 1")

    as_dates = pd.to_datetime(pd.Series(dates)).dt.date
    first, last = as_dates.min(), as_dates.max()
    if first is None or last is None:
        raise ValueError("no dates supplied")

    span = (last - first).days
    needed = min_train_days + n_folds * test_days
    if span < needed:
        raise ValueError(
            f"data spans {span} days but {n_folds} folds of {test_days}d test "
            f"after a {min_train_days}d minimum train window need {needed} days. "
            f"Reduce n_folds or test_days."
        )

    folds: list[Fold] = []
    # Build backwards from the end so the most recent data is always tested on:
    # a model's behaviour on the newest period is the thing you actually care
    # about.
    for i in range(n_folds):
        test_end = last - dt.timedelta(days=test_days * i)
        test_start = test_end - dt.timedelta(days=test_days - 1)
        folds.append(
            Fold(
                index=n_folds - i,
                train_end=test_start,
                test_start=test_start,
                test_end=test_end,
            )
        )
    folds.reverse()
    for position, fold in enumerate(folds, start=1):
        folds[position - 1] = Fold(
            index=position,
            train_end=fold.train_end,
            test_start=fold.test_start,
            test_end=fold.test_end,
        )
    return folds


def split_by_fold(
    frame: pd.DataFrame, fold: Fold, date_column: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (train, test) for one fold.

    Train is everything strictly before `train_end`; test is the fold's window.
    The strict `<` is the whole point — an appointment on the cutoff date must
    not appear in both.
    """
    dates = pd.to_datetime(frame[date_column]).dt.date
    train = frame[dates < fold.train_end]
    test = frame[(dates >= fold.test_start) & (dates <= fold.test_end)]
    return train, test


def final_holdout(
    frame: pd.DataFrame, date_column: str, *, test_fraction: float = 0.2
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A single chronological split, used to fit the model that gets shipped.

    Rolling-origin folds answer "how well does this approach generalise". This
    answers "what do we deploy": one model trained on almost everything, with a
    recent holdout kept back so the shipped artifact still has an honest score
    attached to it.
    """
    if not 0 < test_fraction < 1:
        raise ValueError("test_fraction must be between 0 and 1")
    dates = pd.to_datetime(frame[date_column]).dt.date
    ordered = np.sort(dates.unique())
    cutoff_index = int(len(ordered) * (1 - test_fraction))
    cutoff = ordered[cutoff_index]
    return frame[dates < cutoff], frame[dates >= cutoff]


def assert_no_temporal_leakage(train: pd.DataFrame, test: pd.DataFrame, date_column: str) -> None:
    """Fail loudly if any training row is dated at or after any test row.

    Cheap to run and worth running: the split functions above are easy to
    misuse from a caller, and a leaked split does not look wrong — it looks
    like a better score.
    """
    if train.empty or test.empty:
        return
    train_max = pd.to_datetime(train[date_column]).max()
    test_min = pd.to_datetime(test[date_column]).min()
    if train_max >= test_min:
        raise AssertionError(
            f"temporal leakage: training data reaches {train_max.date()} but the "
            f"test window starts {test_min.date()}"
        )
