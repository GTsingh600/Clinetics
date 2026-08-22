"""Feature engineering, and the leakage boundary.

**The prediction point is booking time.** Every feature must be knowable the
moment the appointment is booked. That is what makes the prediction usable by
the optimizer, which schedules weeks ahead, and by any overbooking decision
taken when the slot is claimed.

That constraint is easy to state and easy to violate, because the single most
predictive feature — the patient's own no-show history — is exactly the one
that leaks if computed carelessly.

--------------------------------------------------------------------------
THE LEAKAGE TRAP
--------------------------------------------------------------------------
Phase 1's generator gives every patient a latent no-show propensity, so their
history genuinely predicts their future. The naive way to use it:

    df["patient_no_show_rate"] = df.groupby("patient_id")["no_show"].transform("mean")

This is catastrophic. The mean includes the row's *own* label, and every future
appointment's label. A model fed this reports excellent metrics and is worthless,
because at prediction time that column cannot exist.

The subtler version is only slightly better:

    # still wrong: uses appointments booked earlier but not yet ATTENDED
    prior = appointments[appointments.booked_at < row.booked_at]

An appointment booked in January for a date in June has no outcome in February.
Counting it as a known result imports information from the future.

**The correct condition** is that the prior appointment must already have
*happened* by the time this one is booked:

    prior.appointment_date < row.booked_at.date()

`_patient_history` below implements exactly that, per patient, with a
searchsorted over each patient's sorted timeline of resolved outcomes. Its
correctness is asserted directly by `tests/unit/test_no_leakage.py`, which
constructs a patient whose behaviour flips and checks the feature never sees
the flip early.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from forecasting.types import Columns

# The clinic's own long-run no-show rate, used as the prior for a patient with
# no history yet. Passing 0.0 would tell the model "this patient never misses",
# which is the opposite of what "unknown" means; the base rate is the honest
# default and shrinks smoothly as evidence accumulates.
SMOOTHING_STRENGTH = 5.0

NO_SHOW_FEATURES = [
    "lead_time_days",
    "log_lead_time",
    "weekday",
    "hour_of_day",
    "month",
    "is_new_patient",
    "urgency_ordinal",
    "duration_minutes",
    "patient_prior_appointments",
    "patient_prior_no_show_rate",
    "patient_prior_no_shows",
    "days_since_patient_last_appointment",
    "specialty_code",
    "is_monday",
    "is_friday",
    "is_early_slot",
    "is_late_slot",
]

URGENCY_ORDER = {"routine": 0, "urgent": 1, "emergency": 2}


def _hour_of(series: pd.Series) -> pd.Series:
    """Hour from a time-like column, whatever pandas made of it."""
    return pd.to_datetime(series.astype(str), format="mixed").dt.hour


def _patient_history(frame: pd.DataFrame, base_rate: float) -> pd.DataFrame:
    """Per-row patient history using only outcomes known at booking time.

    For each row, considers that patient's earlier appointments whose
    `appointment_date` falls strictly before this row's booking date — i.e.
    appointments that had already taken place, so their outcome was observable.

    Implementation note: a self-join would be O(n^2) per patient. Instead each
    patient's resolved outcomes are sorted by date once, cumulative sums are
    taken, and `searchsorted` locates how many of them predate each booking.
    That is O(n log n) overall and, more importantly, makes the time condition a
    single explicit comparison rather than something buried in a merge.
    """
    resolved = frame[frame["no_show"].notna()]

    counts = np.zeros(len(frame), dtype=float)
    no_shows = np.zeros(len(frame), dtype=float)
    days_since = np.full(len(frame), np.nan, dtype=float)

    booking_dates = pd.to_datetime(frame[Columns.BOOKED_AT]).dt.tz_localize(None).dt.normalize()

    # Group both sides ONCE. An earlier version located each patient's rows with
    # `frame[frame.patient_id == pid]` inside the loop, which rescans the whole
    # frame per patient: O(patients x rows), about 147M comparisons here. Taking
    # positional indices from a single groupby makes it O(n log n).
    history_groups = dict(resolved.groupby(Columns.PATIENT_ID).indices)
    target_groups = frame.groupby(Columns.PATIENT_ID).indices

    resolved_dates = pd.to_datetime(resolved[Columns.APPOINTMENT_DATE]).to_numpy()
    resolved_labels = resolved["no_show"].to_numpy(dtype=float)
    all_bookings = booking_dates.to_numpy()

    for patient_id, target_positions in target_groups.items():
        hist_positions = history_groups.get(patient_id)
        if hist_positions is None:
            continue

        order = np.argsort(resolved_dates[hist_positions], kind="stable")
        hist_dates = resolved_dates[hist_positions][order]
        cum_no_shows = np.concatenate([[0.0], np.cumsum(resolved_labels[hist_positions][order])])

        target_bookings = all_bookings[target_positions]
        # How many of this patient's appointments had already occurred when each
        # target row was booked. 'left' so an appointment on the booking day
        # itself does not count: its outcome is not yet known.
        n_prior = np.searchsorted(hist_dates, target_bookings, side="left")

        counts[target_positions] = n_prior
        no_shows[target_positions] = cum_no_shows[n_prior]
        has_history = n_prior > 0
        if has_history.any():
            previous = hist_dates[n_prior[has_history] - 1]
            days_since[target_positions[has_history]] = (
                target_bookings[has_history] - previous
            ) / np.timedelta64(1, "D")

    # Empirical-Bayes shrinkage toward the clinic base rate. A patient who has
    # missed their only appointment is not a 100% no-show risk; with SMOOTHING
    # of 5 they read as roughly (1 + 5*base) / (1 + 5), which decays toward
    # their true rate as evidence accumulates.
    rate = (no_shows + SMOOTHING_STRENGTH * base_rate) / (counts + SMOOTHING_STRENGTH)

    return pd.DataFrame(
        {
            "patient_prior_appointments": counts,
            "patient_prior_no_shows": no_shows,
            "patient_prior_no_show_rate": rate,
            "days_since_patient_last_appointment": days_since,
        },
        index=frame.index,
    )


def build_no_show_features(
    frame: pd.DataFrame, *, base_rate: float | None = None
) -> tuple[pd.DataFrame, pd.Series]:
    """Build the no-show feature matrix and label.

    Args:
        frame: raw appointment rows. Must contain the `Columns` fields.
        base_rate: clinic no-show rate used to smooth sparse patient history.
            MUST be computed from the training window only and passed in when
            transforming a test set — deriving it from the frame being scored
            would leak the test set's label distribution into its own features.

    Returns:
        (X, y) where y is 1 for a no-show. Cancelled and future appointments
        have no outcome and are dropped.
    """
    required = {
        Columns.PATIENT_ID,
        Columns.APPOINTMENT_DATE,
        Columns.START_TIME,
        Columns.BOOKED_AT,
        Columns.STATUS,
        Columns.SPECIALTY,
        Columns.URGENCY,
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")

    df = frame.copy()
    # A cancelled appointment is not a no-show and not an attendance; it is a
    # different outcome entirely. Training on it as a negative would teach the
    # model that cancellations are attendances.
    df["no_show"] = np.where(
        df[Columns.STATUS] == "no_show",
        1.0,
        np.where(df[Columns.STATUS] == "completed", 0.0, np.nan),
    )

    if base_rate is None:
        base_rate = float(df["no_show"].mean(skipna=True))
    if not np.isfinite(base_rate):
        base_rate = 0.2

    booked = pd.to_datetime(df[Columns.BOOKED_AT]).dt.tz_localize(None)
    appointment = pd.to_datetime(df[Columns.APPOINTMENT_DATE])

    df["lead_time_days"] = (appointment - booked.dt.normalize()).dt.days.clip(lower=0)
    # log1p because the effect saturates: the difference between booking today
    # and a week out matters far more than between 60 and 67 days.
    df["log_lead_time"] = np.log1p(df["lead_time_days"])
    df["weekday"] = appointment.dt.weekday
    df["month"] = appointment.dt.month
    df["hour_of_day"] = _hour_of(df[Columns.START_TIME])
    df["is_monday"] = (df["weekday"] == 0).astype(int)
    df["is_friday"] = (df["weekday"] == 4).astype(int)
    df["is_early_slot"] = (df["hour_of_day"] <= 9).astype(int)
    df["is_late_slot"] = (df["hour_of_day"] >= 17).astype(int)
    df["urgency_ordinal"] = df[Columns.URGENCY].map(URGENCY_ORDER).fillna(0).astype(int)
    df["is_new_patient"] = df[Columns.IS_NEW_PATIENT].astype(int)
    # Specialty as an ordered code; the tree splits on it as a categorical
    # proxy without needing one column per specialty.
    df["specialty_code"] = df[Columns.SPECIALTY].astype("category").cat.codes
    if Columns.DURATION not in df.columns:
        df[Columns.DURATION] = np.nan

    df = df.join(_patient_history(df, base_rate))

    labelled = df[df["no_show"].notna()]
    return labelled[NO_SHOW_FEATURES].astype(float), labelled["no_show"].astype(int)


# ---------------------------------------------------------------------------
# Demand
# ---------------------------------------------------------------------------
DEMAND_FEATURES = [
    "weekday",
    "hour_of_day",
    "month",
    "day_of_year",
    "specialty_code",
    "is_weekend",
    "week_of_year",
    # Annual seasonality as Fourier terms rather than only a raw day number.
    # A tree splitting on day_of_year has to approximate a smooth yearly cycle
    # with step functions, and cannot represent that 31 December is adjacent to
    # 1 January. sin/cos give it both properties for two columns.
    "season_sin",
    "season_cos",
    # The seasonal-naive baseline, handed to the model as a feature.
    #
    # That baseline is close to the true generating process here: demand is
    # essentially a (specialty, weekday, hour) profile scaled by an annual
    # cycle. A model asked to rediscover the profile from calendar splits will
    # approximate it with error; given it directly, it starts level with the
    # baseline and only has to learn what the profile misses.
    #
    # This is target encoding, so it is leakage-prone by construction. The
    # profile MUST be computed on training rows only and passed in when
    # transforming a test set - see `demand_profile`.
    "profile_mean",
]


def build_demand_grid(
    frame: pd.DataFrame, *, open_hour: int = 8, close_hour: int = 18
) -> pd.DataFrame:
    """Aggregate appointments into a ZERO-FILLED (specialty, date, hour) grid.

    The zero-filling is the point, and it is the easiest thing to get wrong.
    Grouping the appointment table gives only cells that *have* appointments —
    roughly 22,000 of about 61,000 possible cells in this dataset. Train on
    those alone and the model never sees a quiet hour, so it learns that demand
    is always at least one and systematically over-predicts Sunday mornings and
    late evenings.

    Cancelled appointments are excluded: demand means appointments that
    actually occupied the calendar.
    """
    df = frame[frame[Columns.STATUS] != "cancelled"].copy()
    df["hour_of_day"] = _hour_of(df[Columns.START_TIME])
    df[Columns.APPOINTMENT_DATE] = pd.to_datetime(df[Columns.APPOINTMENT_DATE]).dt.date

    observed = (
        df.groupby([Columns.SPECIALTY, Columns.APPOINTMENT_DATE, "hour_of_day"])
        .size()
        .reset_index(name="count")
    )

    specialties = sorted(df[Columns.SPECIALTY].unique())
    dates = pd.date_range(
        df[Columns.APPOINTMENT_DATE].min(), df[Columns.APPOINTMENT_DATE].max(), freq="D"
    ).date
    hours = range(open_hour, close_hour + 1)

    full = pd.MultiIndex.from_product(
        [specialties, dates, hours],
        names=[Columns.SPECIALTY, Columns.APPOINTMENT_DATE, "hour_of_day"],
    ).to_frame(index=False)

    grid = full.merge(
        observed, on=[Columns.SPECIALTY, Columns.APPOINTMENT_DATE, "hour_of_day"], how="left"
    )
    grid["count"] = grid["count"].fillna(0).astype(int)
    return grid


def demand_profile(train_grid: pd.DataFrame) -> pd.Series:
    """Mean count per (specialty, weekday, hour), from TRAINING rows only.

    Fitted separately from the transform so a test set can never contribute to
    the encoding of its own features.
    """
    frame = train_grid.copy()
    frame["weekday"] = pd.to_datetime(frame[Columns.APPOINTMENT_DATE]).dt.weekday
    return frame.groupby([Columns.SPECIALTY, "weekday", "hour_of_day"])["count"].mean()


def build_demand_features(
    grid: pd.DataFrame, *, profile: pd.Series | None = None
) -> tuple[pd.DataFrame, pd.Series]:
    """Calendar features for the demand grid.

    Deliberately no lagged counts. Lags would improve short-horizon accuracy,
    but the optimizer needs a forecast for a date weeks ahead, when the lag
    values do not exist yet. A model that cannot be evaluated the way it will be
    used is not worth its extra accuracy.
    """
    df = grid.copy()
    dates = pd.to_datetime(df[Columns.APPOINTMENT_DATE])
    df["weekday"] = dates.dt.weekday
    df["month"] = dates.dt.month
    df["day_of_year"] = dates.dt.dayofyear
    df["week_of_year"] = dates.dt.isocalendar().week.astype(int)
    df["is_weekend"] = (df["weekday"] >= 5).astype(int)
    df["specialty_code"] = df[Columns.SPECIALTY].astype("category").cat.codes
    angle = 2 * np.pi * df["day_of_year"] / 365.25
    df["season_sin"] = np.sin(angle)
    df["season_cos"] = np.cos(angle)

    if profile is None:
        # Training-time convenience only. Callers evaluating a test set must
        # pass a profile fitted on the training window.
        profile = demand_profile(grid)
    keys = pd.MultiIndex.from_arrays([df[Columns.SPECIALTY], df["weekday"], df["hour_of_day"]])
    # An unseen combination falls back to the global mean rather than NaN, so a
    # new specialty or an extended opening hour degrades instead of failing.
    df["profile_mean"] = profile.reindex(keys).to_numpy(dtype=float)
    df["profile_mean"] = pd.Series(df["profile_mean"], index=df.index).fillna(
        float(profile.mean()) if len(profile) else 0.0
    )

    return df[DEMAND_FEATURES].astype(float), df["count"].astype(float)


# ---------------------------------------------------------------------------
# Duration
# ---------------------------------------------------------------------------
DURATION_FEATURES = [
    "specialty_code",
    "is_new_patient",
    "urgency_ordinal",
    "weekday",
    "hour_of_day",
    "patient_prior_appointments",
]


def build_duration_features(
    frame: pd.DataFrame, *, base_rate: float = 0.2
) -> tuple[pd.DataFrame, pd.Series]:
    """Features for predicting consultation length.

    Known at booking time, like the no-show model: the optimizer needs a
    duration estimate to allocate a slot, which happens before the consultation.
    """
    df = frame.copy()
    df["no_show"] = np.where(
        df[Columns.STATUS] == "no_show",
        1.0,
        np.where(df[Columns.STATUS] == "completed", 0.0, np.nan),
    )
    appointment = pd.to_datetime(df[Columns.APPOINTMENT_DATE])
    df["weekday"] = appointment.dt.weekday
    df["hour_of_day"] = _hour_of(df[Columns.START_TIME])
    df["urgency_ordinal"] = df[Columns.URGENCY].map(URGENCY_ORDER).fillna(0).astype(int)
    df["is_new_patient"] = df[Columns.IS_NEW_PATIENT].astype(int)
    df["specialty_code"] = df[Columns.SPECIALTY].astype("category").cat.codes
    df = df.join(_patient_history(df, base_rate)[["patient_prior_appointments"]])

    # A no-show has no consultation, so its duration is the slot that was
    # booked, not a length anyone observed. Training on those would teach the
    # model to predict planned duration rather than actual.
    usable = df[df[Columns.STATUS] == "completed"]
    return usable[DURATION_FEATURES].astype(float), usable[Columns.DURATION].astype(float)
