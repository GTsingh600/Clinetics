"""Proof that the no-show features cannot see the future.

The patient-history feature is the single most dangerous thing in Phase 3.
Phase 1 deliberately gave every patient a latent propensity so their history
predicts their future — which means a careless implementation produces a model
with excellent metrics and no value, because the feature it relies on cannot
exist at prediction time.

These tests construct small, hand-checkable frames where a leak would be
visible as a specific wrong number, rather than as a suspiciously good score.
They run in milliseconds and need no database.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from forecasting.features import (
    NO_SHOW_FEATURES,
    build_demand_grid,
    build_no_show_features,
)
from forecasting.splits import (
    assert_no_temporal_leakage,
    final_holdout,
    rolling_origin_folds,
    split_by_fold,
)


def appointment(
    *,
    appointment_id: int,
    patient_id: int,
    date: str,
    booked: str,
    status: str,
    hour: int = 9,
    specialty: str = "cardiology",
) -> dict:
    return {
        "appointment_id": appointment_id,
        "patient_id": patient_id,
        "doctor_id": 1,
        "specialty": specialty,
        "appointment_date": dt.date.fromisoformat(date),
        "start_time": f"{hour:02d}:00:00",
        "duration_minutes": 30,
        "status": status,
        "urgency": "routine",
        "is_new_patient": False,
        "booked_at": pd.Timestamp(booked, tz="UTC"),
    }


# --------------------------------------------------------------------------
# The core leakage guarantee
# --------------------------------------------------------------------------
def test_patient_history_excludes_the_rows_own_outcome() -> None:
    """The most direct leak: a patient's rate including their own label.

    One patient, one appointment, a no-show. If the feature included the row's
    own outcome the prior-no-show count would be 1. It must be 0 — at booking
    time nothing had happened yet.
    """
    frame = pd.DataFrame(
        [
            appointment(
                appointment_id=1,
                patient_id=7,
                date="2026-03-10",
                booked="2026-03-01",
                status="no_show",
            )
        ]
    )
    X, y = build_no_show_features(frame)

    assert y.tolist() == [1]
    assert X["patient_prior_appointments"].iloc[0] == 0
    assert X["patient_prior_no_shows"].iloc[0] == 0


def test_patient_history_excludes_future_appointments() -> None:
    """A later appointment must not inform an earlier one.

    Three appointments for one patient. The FIRST is scored; the two after it
    are no-shows. A rate computed with `groupby.transform("mean")` would give
    the first row 2/3. It must see nothing.
    """
    frame = pd.DataFrame(
        [
            appointment(
                appointment_id=1,
                patient_id=7,
                date="2026-01-10",
                booked="2026-01-01",
                status="completed",
            ),
            appointment(
                appointment_id=2,
                patient_id=7,
                date="2026-02-10",
                booked="2026-02-01",
                status="no_show",
            ),
            appointment(
                appointment_id=3,
                patient_id=7,
                date="2026-03-10",
                booked="2026-03-01",
                status="no_show",
            ),
        ]
    )
    X, _ = build_no_show_features(frame)

    first = X.iloc[0]
    assert first["patient_prior_appointments"] == 0
    assert first["patient_prior_no_shows"] == 0


def test_history_counts_only_appointments_that_had_already_happened() -> None:
    """The subtle leak: booked earlier, but not yet attended.

    Appointment A is booked 1 Jan for 1 June. Appointment B is booked 1 Feb.
    On 1 Feb, A has NOT happened, so its outcome is unknown and it must not
    count — even though it was booked first.

    An implementation filtering on `booked_at < booked_at` would wrongly count
    it, importing an outcome from four months in the future.
    """
    frame = pd.DataFrame(
        [
            appointment(
                appointment_id=1,
                patient_id=7,
                date="2026-06-01",
                booked="2026-01-01",
                status="no_show",
            ),
            appointment(
                appointment_id=2,
                patient_id=7,
                date="2026-02-15",
                booked="2026-02-01",
                status="completed",
            ),
        ]
    )
    X, _ = build_no_show_features(frame)

    later_booking = X.iloc[1]
    assert (
        later_booking["patient_prior_appointments"] == 0
    ), "an appointment that has not taken place yet must not count as history"


def test_history_accumulates_only_past_outcomes() -> None:
    """The positive case: real history is counted, in order."""
    frame = pd.DataFrame(
        [
            appointment(
                appointment_id=1,
                patient_id=7,
                date="2026-01-10",
                booked="2026-01-05",
                status="no_show",
            ),
            appointment(
                appointment_id=2,
                patient_id=7,
                date="2026-02-10",
                booked="2026-02-05",
                status="no_show",
            ),
            appointment(
                appointment_id=3,
                patient_id=7,
                date="2026-03-10",
                booked="2026-03-05",
                status="completed",
            ),
            appointment(
                appointment_id=4,
                patient_id=7,
                date="2026-04-10",
                booked="2026-04-05",
                status="completed",
            ),
        ]
    )
    X, _ = build_no_show_features(frame)

    assert X["patient_prior_appointments"].tolist() == [0, 1, 2, 3]
    assert X["patient_prior_no_shows"].tolist() == [0, 1, 2, 2]


def test_patients_histories_do_not_mix() -> None:
    """Patient B's misses must not raise patient A's risk."""
    rows = [
        appointment(
            appointment_id=i,
            patient_id=99,
            date=f"2026-0{i}-10",
            booked=f"2026-0{i}-05",
            status="no_show",
        )
        for i in range(1, 5)
    ]
    rows.append(
        appointment(
            appointment_id=9,
            patient_id=1,
            date="2026-05-10",
            booked="2026-05-05",
            status="completed",
        )
    )
    X, _ = build_no_show_features(pd.DataFrame(rows))

    other_patient = X.iloc[-1]
    assert other_patient["patient_prior_appointments"] == 0
    assert other_patient["patient_prior_no_shows"] == 0


def test_history_rate_shrinks_toward_the_base_rate() -> None:
    """A single miss must not read as a 100% no-show risk.

    Without smoothing, one missed appointment gives a rate of 1.0 and the model
    treats a single data point as certainty. Empirical-Bayes shrinkage pulls
    sparse evidence toward the clinic base rate.
    """
    frame = pd.DataFrame(
        [
            appointment(
                appointment_id=1,
                patient_id=7,
                date="2026-01-10",
                booked="2026-01-05",
                status="no_show",
            ),
            appointment(
                appointment_id=2,
                patient_id=7,
                date="2026-02-10",
                booked="2026-02-05",
                status="completed",
            ),
        ]
    )
    X, _ = build_no_show_features(frame, base_rate=0.2)

    second = X["patient_prior_no_show_rate"].iloc[1]
    assert 0.2 < second < 1.0, f"one miss should shrink toward the base rate, got {second}"


def test_cancelled_appointments_are_not_labelled() -> None:
    """A cancellation is neither an attendance nor a no-show.

    Treating it as a negative would teach the model that cancelling is showing
    up, and it is a different decision by a different person at a different time.
    """
    frame = pd.DataFrame(
        [
            appointment(
                appointment_id=1,
                patient_id=7,
                date="2026-01-10",
                booked="2026-01-05",
                status="cancelled",
            ),
            appointment(
                appointment_id=2,
                patient_id=7,
                date="2026-02-10",
                booked="2026-02-05",
                status="completed",
            ),
        ]
    )
    X, y = build_no_show_features(frame)

    assert len(y) == 1, "only resolved appointments carry a label"
    assert y.iloc[0] == 0
    assert X["patient_prior_appointments"].iloc[0] == 0


def test_base_rate_is_injectable_so_test_sets_cannot_leak() -> None:
    """Transforming a test set must use the TRAINING base rate.

    Deriving it from the frame being scored would fold that frame's own label
    distribution into its features — a quiet leak that no split can catch.
    """
    frame = pd.DataFrame(
        [
            appointment(
                appointment_id=1,
                patient_id=7,
                date="2026-01-10",
                booked="2026-01-05",
                status="no_show",
            ),
            appointment(
                appointment_id=2,
                patient_id=7,
                date="2026-02-10",
                booked="2026-02-05",
                status="no_show",
            ),
        ]
    )
    low, _ = build_no_show_features(frame, base_rate=0.05)
    high, _ = build_no_show_features(frame, base_rate=0.50)
    assert low["patient_prior_no_show_rate"].iloc[0] < high["patient_prior_no_show_rate"].iloc[0]


def test_every_declared_feature_is_produced() -> None:
    """Guards against a feature silently vanishing from the matrix."""
    frame = pd.DataFrame(
        [
            appointment(
                appointment_id=1,
                patient_id=7,
                date="2026-01-10",
                booked="2026-01-05",
                status="completed",
            ),
        ]
    )
    X, _ = build_no_show_features(frame)
    assert list(X.columns) == NO_SHOW_FEATURES
    assert X.notna().all().all() or X["days_since_patient_last_appointment"].isna().all()


# --------------------------------------------------------------------------
# Splits
# --------------------------------------------------------------------------
def _year_of_dates() -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", "2026-06-30", freq="D")
    return pd.DataFrame({"appointment_date": dates.date, "value": np.arange(len(dates))})


def test_rolling_origin_folds_never_overlap_train_and_test() -> None:
    frame = _year_of_dates()
    folds = rolling_origin_folds(frame["appointment_date"], n_folds=4, test_days=90)

    assert len(folds) == 4
    for fold in folds:
        train, test = split_by_fold(frame, fold, "appointment_date")
        assert not train.empty and not test.empty
        # The real assertion: nothing in train is dated at or after the test start.
        assert_no_temporal_leakage(train, test, "appointment_date")


def test_rolling_origin_training_window_expands() -> None:
    """Each fold trains on strictly more history than the last."""
    frame = _year_of_dates()
    folds = rolling_origin_folds(frame["appointment_date"], n_folds=4, test_days=90)
    sizes = [len(split_by_fold(frame, f, "appointment_date")[0]) for f in folds]
    assert sizes == sorted(sizes)
    assert sizes[0] < sizes[-1]


def test_rolling_origin_refuses_impossible_configurations() -> None:
    """Silently returning fewer folds would make a reported mean a lie."""
    short = pd.DataFrame({"appointment_date": pd.date_range("2026-01-01", periods=60).date})
    with pytest.raises(ValueError, match="days"):
        rolling_origin_folds(short["appointment_date"], n_folds=4, test_days=90)


def test_final_holdout_is_chronological() -> None:
    frame = _year_of_dates()
    train, test = final_holdout(frame, "appointment_date", test_fraction=0.2)
    assert_no_temporal_leakage(train, test, "appointment_date")
    assert len(test) > 0
    assert len(train) > len(test)


def test_leakage_detector_actually_detects_leakage() -> None:
    """The guard must fail on a bad split, or it is decoration."""
    frame = _year_of_dates()
    shuffled = frame.sample(frac=1.0, random_state=0)
    bad_train = shuffled.iloc[: len(frame) // 2]
    bad_test = shuffled.iloc[len(frame) // 2 :]
    with pytest.raises(AssertionError, match="temporal leakage"):
        assert_no_temporal_leakage(bad_train, bad_test, "appointment_date")


# --------------------------------------------------------------------------
# Demand grid
# --------------------------------------------------------------------------
def test_demand_grid_is_zero_filled() -> None:
    """The missing-zeros trap.

    Two appointments in one hour of one day. Grouping alone yields ONE row; the
    grid must contain a row for every (specialty, date, hour), most of them
    zero, or the model never learns what a quiet hour looks like.
    """
    frame = pd.DataFrame(
        [
            appointment(
                appointment_id=1,
                patient_id=1,
                date="2026-01-01",
                booked="2025-12-01",
                status="completed",
                hour=9,
            ),
            appointment(
                appointment_id=2,
                patient_id=2,
                date="2026-01-03",
                booked="2025-12-01",
                status="completed",
                hour=9,
            ),
        ]
    )
    grid = build_demand_grid(frame, open_hour=8, close_hour=10)

    # 1 specialty x 3 days x 3 hours
    assert len(grid) == 9
    assert grid["count"].sum() == 2
    assert (grid["count"] == 0).sum() == 7, "quiet cells must exist as explicit zeros"


def test_demand_grid_excludes_cancellations() -> None:
    """Demand means appointments that actually occupied the calendar."""
    frame = pd.DataFrame(
        [
            appointment(
                appointment_id=1,
                patient_id=1,
                date="2026-01-01",
                booked="2025-12-01",
                status="cancelled",
                hour=9,
            ),
            appointment(
                appointment_id=2,
                patient_id=2,
                date="2026-01-01",
                booked="2025-12-01",
                status="completed",
                hour=9,
            ),
        ]
    )
    grid = build_demand_grid(frame, open_hour=9, close_hour=9)
    assert grid["count"].sum() == 1
