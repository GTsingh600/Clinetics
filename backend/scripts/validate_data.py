"""THE VALIDATION GATE for the synthetic data.

Phase 1 does not end when `generate_data.py` runs without error. It ends when
this script passes. If the correlations the generator claims to produce are not
measurably present in the rows it wrote, the generator is wrong and no ML work
may proceed — a model trained on structureless data will report metrics that
mean nothing, and every number downstream inherits that emptiness.

So this is a *gate*, not a report: it exits non-zero on failure and names what
broke. It checks the same properties the module docstring of `generate_data.py`
promises:

  1. No-show rate rises with lead time (the strongest intended signal).
  2. No-show rate varies by day of week, with Monday and Friday worst.
  3. No-show rate varies by hour, high at the edges of the day.
  4. A patient's past no-show behaviour predicts their future behaviour.
  5. Demand shows a weekday/weekend split and a Monday peak.
  6. Specialty-specific intra-day shape: dermatology peaks in the evening while
     the morning-weighted specialties peak before noon.
  7. Consultation duration varies by specialty, and new patients take longer.
  8. Sanity: the overall no-show rate is plausible, and the generator did not
     silently drop most appointments because the clinic was full.

Plots are written to `backend/reports/` as visual evidence. They are committed
deliberately: "the correlation is visible in the plot" is a claim a reader
should be able to check without running anything.

    uv run python scripts/validate_data.py
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: this must run in CI with no display
import matplotlib.pyplot as plt
import pandas as pd
from scipy import stats
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("validate_data")

REPORTS = Path(__file__).resolve().parent.parent / "reports"
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def render(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return f"[{mark}] {self.name}: {self.detail}"


def load_frames(engine: Engine) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Past appointments (with a known outcome) and all appointments."""
    past = pd.read_sql(
        text("""
            SELECT a.id, a.patient_id, a.doctor_id, a.appointment_date, a.start_time,
                   a.duration_minutes, a.status::text AS status, a.urgency::text AS urgency,
                   a.is_new_patient, a.booked_at, s.slug AS specialty
            FROM appointment a
            JOIN specialty s ON s.id = a.specialty_id
            WHERE a.appointment_date < CURRENT_DATE
              AND a.status IN ('completed', 'no_show')
            """),
        engine,
    )
    every = pd.read_sql(
        text("""
            SELECT a.appointment_date, a.start_time, a.status::text AS status,
                   a.duration_minutes, a.is_new_patient, s.slug AS specialty
            FROM appointment a
            JOIN specialty s ON s.id = a.specialty_id
            """),
        engine,
    )
    for df in (past, every):
        df["hour"] = pd.to_datetime(df["start_time"].astype(str)).dt.hour
        df["weekday"] = pd.to_datetime(df["appointment_date"]).dt.dayofweek  # Mon=0
    past["lead_days"] = (
        pd.to_datetime(past["appointment_date"])
        - pd.to_datetime(past["booked_at"]).dt.tz_localize(None).dt.normalize()
    ).dt.days.clip(lower=0)
    past["no_show"] = (past["status"] == "no_show").astype(int)
    return past, every


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def check_lead_time(past: pd.DataFrame, checks: list[Check]) -> pd.DataFrame:
    bins = [-1, 0, 1, 3, 7, 14, 21, 30, 45, 1000]
    labels = ["0", "1", "2-3", "4-7", "8-14", "15-21", "22-30", "31-45", "46+"]
    past = past.copy()
    past["lead_bucket"] = pd.cut(past["lead_days"], bins=bins, labels=labels)
    grouped = (
        past.groupby("lead_bucket", observed=True)["no_show"].agg(["mean", "count"]).reset_index()
    )

    rho, pval = stats.spearmanr(past["lead_days"], past["no_show"])
    first, last = grouped["mean"].iloc[0], grouped["mean"].iloc[-1]
    ratio = last / first if first > 0 else float("inf")

    checks.append(
        Check(
            "no-show rises with lead time (Spearman)",
            rho > 0.05 and pval < 0.01,
            f"rho={rho:+.4f}, p={pval:.2e} (need rho>0.05, p<0.01)",
        )
    )
    checks.append(
        Check(
            "no-show rate at longest vs shortest lead",
            ratio >= 1.5,
            f"{first:.1%} -> {last:.1%} ({ratio:.2f}x, need >=1.5x)",
        )
    )
    # Monotonicity is checked loosely: bucket noise is expected, a trend is not
    # optional.
    trend_rho, _ = stats.spearmanr(range(len(grouped)), grouped["mean"])
    checks.append(
        Check(
            "lead-time buckets trend upward",
            trend_rho >= 0.7,
            f"bucket-order rho={trend_rho:+.2f} (need >=0.70)",
        )
    )
    return grouped


def check_weekday_and_hour(past: pd.DataFrame, checks: list[Check]) -> None:
    by_dow = past.groupby("weekday")["no_show"].mean()
    mon, fri = by_dow.get(0, 0), by_dow.get(4, 0)
    midweek = by_dow.reindex([1, 2, 3]).mean()
    checks.append(
        Check(
            "Monday/Friday no-show worse than midweek",
            mon > midweek and fri > midweek,
            f"Mon={mon:.1%}, Fri={fri:.1%}, midweek={midweek:.1%}",
        )
    )

    by_hour = past.groupby("hour")["no_show"].mean()
    edges = by_hour.reindex([8, 17, 18]).dropna().mean()
    middle = by_hour.reindex([11, 12, 13]).dropna().mean()
    checks.append(
        Check(
            "no-show higher at the edges of the day",
            edges > middle,
            f"edges(08,17,18)={edges:.1%} vs midday(11,12,13)={middle:.1%}",
        )
    )


def check_patient_history(past: pd.DataFrame, checks: list[Check]) -> None:
    """A patient's own history must predict their future.

    Split each patient's appointments chronologically and correlate the
    first-half no-show rate against the second-half rate. If patients were
    i.i.d. coin flips this correlation would be ~0 and the "patient historical
    no-show rate" feature in Phase 3 would be pure noise.
    """
    df = past.sort_values(["patient_id", "appointment_date"])
    rows = []
    for _patient_id, g in df.groupby("patient_id"):
        if len(g) < 6:
            continue
        mid = len(g) // 2
        rows.append((g["no_show"].iloc[:mid].mean(), g["no_show"].iloc[mid:].mean()))
    if len(rows) < 30:
        checks.append(
            Check(
                "patient no-show behaviour persists",
                False,
                f"only {len(rows)} patients with >=6 outcomes; generate more data",
            )
        )
        return
    early = [r[0] for r in rows]
    late = [r[1] for r in rows]
    rho, pval = stats.spearmanr(early, late)
    checks.append(
        Check(
            "patient no-show behaviour persists across halves",
            rho > 0.10 and pval < 0.05,
            f"rho={rho:+.3f}, p={pval:.2e} over {len(rows)} patients (need rho>0.10)",
        )
    )


def check_demand(every: pd.DataFrame, checks: list[Check]) -> pd.DataFrame:
    active = every[every["status"] != "cancelled"]
    per_day = active.groupby(["appointment_date", "weekday"]).size().reset_index(name="n")
    by_dow = per_day.groupby("weekday")["n"].mean()

    weekday_mean = by_dow.reindex([0, 1, 2, 3, 4]).mean()
    weekend_mean = by_dow.reindex([5, 6]).mean()
    checks.append(
        Check(
            "weekday/weekend demand split",
            weekend_mean < 0.6 * weekday_mean,
            f"weekday={weekday_mean:.1f}/day vs weekend={weekend_mean:.1f}/day "
            f"(need weekend < 60% of weekday)",
        )
    )
    checks.append(
        Check(
            "Monday is the busiest weekday",
            by_dow.reindex([0, 1, 2, 3, 4]).idxmax() == 0,
            f"busiest weekday = {WEEKDAY_NAMES[int(by_dow.reindex([0,1,2,3,4]).idxmax())]} "
            f"({by_dow[0]:.1f}/day)",
        )
    )

    hour_of_week = active.groupby(["weekday", "hour"]).size().reset_index(name="n")
    return hour_of_week


def check_specialty_shape(every: pd.DataFrame, checks: list[Check]) -> pd.DataFrame:
    active = every[every["status"] != "cancelled"]
    profile = active.groupby(["specialty", "hour"]).size().reset_index(name="n")
    peaks = profile.loc[profile.groupby("specialty")["n"].idxmax()].set_index("specialty")["hour"]

    derm_peak = peaks.get("dermatology")
    gp_peak = peaks.get("general-practice")
    checks.append(
        Check(
            "dermatology peaks in the evening",
            derm_peak is not None and derm_peak >= 15,
            f"dermatology peak hour = {derm_peak} (need >= 15)",
        )
    )
    checks.append(
        Check(
            "general practice peaks in the morning",
            gp_peak is not None and gp_peak <= 12,
            f"general-practice peak hour = {gp_peak} (need <= 12)",
        )
    )
    return profile


def check_duration(every: pd.DataFrame, checks: list[Check]) -> pd.DataFrame:
    by_spec = every.groupby("specialty")["duration_minutes"].mean().sort_values()
    spread = by_spec.max() - by_spec.min()
    checks.append(
        Check(
            "duration varies by specialty",
            spread >= 10,
            f"mean duration {by_spec.min():.1f}..{by_spec.max():.1f} min "
            f"(spread {spread:.1f}, need >= 10)",
        )
    )

    grouped = every.groupby(["specialty", "is_new_patient"])["duration_minutes"].mean().unstack()
    if {True, False}.issubset(set(grouped.columns)):
        diffs = grouped[True] - grouped[False]
        checks.append(
            Check(
                "new patients take longer in every specialty",
                bool((diffs > 0).all()),
                "; ".join(f"{s}: +{d:.1f}min" for s, d in diffs.items()),
            )
        )
    return every


def check_sanity(past: pd.DataFrame, every: pd.DataFrame, checks: list[Check]) -> None:
    rate = past["no_show"].mean()
    checks.append(
        Check(
            "overall no-show rate is plausible",
            0.08 <= rate <= 0.35,
            f"{rate:.1%} of completed/no-show appointments (need 8%-35%)",
        )
    )
    checks.append(
        Check(
            "enough data to train on",
            len(past) >= 3000,
            f"{len(past)} resolved appointments, {len(every)} total",
        )
    )


# --------------------------------------------------------------------------
# Plots
# --------------------------------------------------------------------------
def plot_all(
    lead_buckets: pd.DataFrame,
    hour_of_week: pd.DataFrame,
    spec_profile: pd.DataFrame,
    every: pd.DataFrame,
    past: pd.DataFrame,
) -> list[Path]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    # 1. No-show rate vs lead time — the headline correlation.
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.bar(lead_buckets["lead_bucket"].astype(str), lead_buckets["mean"] * 100, color="#3b6ea5")
    ax.set_xlabel("Lead time (days between booking and appointment)")
    ax.set_ylabel("No-show rate (%)")
    ax.set_title("No-show rate rises with lead time")
    for i, (_, row) in enumerate(lead_buckets.iterrows()):
        ax.text(i, row["mean"] * 100 + 0.4, f"n={int(row['count'])}", ha="center", fontsize=7)
    fig.tight_layout()
    p = REPORTS / "no_show_vs_lead_time.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    written.append(p)

    # 2. Demand across the hour-of-week grid.
    grid = hour_of_week.pivot(index="hour", columns="weekday", values="n").fillna(0)
    fig, ax = plt.subplots(figsize=(9, 5))
    im = ax.imshow(grid.values, aspect="auto", origin="lower", cmap="viridis")
    ax.set_xticks(range(len(grid.columns)))
    ax.set_xticklabels([WEEKDAY_NAMES[int(c)] for c in grid.columns])
    ax.set_yticks(range(len(grid.index)))
    ax.set_yticklabels(grid.index)
    ax.set_xlabel("Day of week")
    ax.set_ylabel("Hour of day")
    ax.set_title("Appointment demand by hour of week")
    fig.colorbar(im, ax=ax, label="appointments")
    fig.tight_layout()
    p = REPORTS / "demand_by_hour_of_week.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    written.append(p)

    # 3. Specialty intra-day shape — dermatology must look different.
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for specialty, g in spec_profile.groupby("specialty"):
        g = g.sort_values("hour")
        ax.plot(g["hour"], g["n"] / g["n"].sum(), marker="o", label=specialty)
    ax.set_xlabel("Hour of day")
    ax.set_ylabel("Share of that specialty's appointments")
    ax.set_title("Intra-day demand shape differs by specialty")
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = REPORTS / "demand_by_specialty_hour.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    written.append(p)

    # 4. Duration distribution by specialty and patient type.
    fig, ax = plt.subplots(figsize=(8, 4.5))
    specialties = sorted(every["specialty"].unique())
    for offset, is_new in ((-0.18, False), (0.18, True)):
        means = [
            every[(every["specialty"] == s) & (every["is_new_patient"] == is_new)][
                "duration_minutes"
            ].mean()
            for s in specialties
        ]
        ax.bar(
            [i + offset for i in range(len(specialties))],
            means,
            width=0.34,
            label="new patient" if is_new else "returning",
        )
    ax.set_xticks(range(len(specialties)))
    ax.set_xticklabels(specialties, rotation=20, ha="right")
    ax.set_ylabel("Mean duration (minutes)")
    ax.set_title("Consultation duration by specialty and patient type")
    ax.legend()
    fig.tight_layout()
    p = REPORTS / "duration_by_specialty.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    written.append(p)

    # 5. No-show by weekday and hour.
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    dow = past.groupby("weekday")["no_show"].mean() * 100
    axes[0].bar([WEEKDAY_NAMES[int(i)] for i in dow.index], dow.values, color="#a5533b")
    axes[0].set_title("No-show rate by day of week")
    axes[0].set_ylabel("No-show rate (%)")
    hr = past.groupby("hour")["no_show"].mean() * 100
    axes[1].plot(hr.index, hr.values, marker="o", color="#a5533b")
    axes[1].set_title("No-show rate by hour of day")
    axes[1].set_xlabel("Hour")
    fig.tight_layout()
    p = REPORTS / "no_show_by_weekday_and_hour.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    written.append(p)

    return written


def main() -> int:
    engine = create_engine(settings.database_url_sync, future=True)
    past, every = load_frames(engine)

    if past.empty:
        log.error("no resolved appointments found. Run scripts/generate_data.py first.")
        return 1

    checks: list[Check] = []
    lead_buckets = check_lead_time(past, checks)
    check_weekday_and_hour(past, checks)
    check_patient_history(past, checks)
    hour_of_week = check_demand(every, checks)
    spec_profile = check_specialty_shape(every, checks)
    check_duration(every, checks)
    check_sanity(past, every, checks)

    written = plot_all(lead_buckets, hour_of_week, spec_profile, every, past)

    print("\n" + "=" * 78)
    print("SYNTHETIC DATA VALIDATION GATE")
    print("=" * 78)
    for check in checks:
        print(check.render())
    print("-" * 78)
    for path in written:
        print(f"plot: {path.relative_to(path.parent.parent.parent)}")
    print("=" * 78)

    failed = [c for c in checks if not c.passed]
    if failed:
        print(f"\nGATE FAILED: {len(failed)} of {len(checks)} checks did not pass.")
        print("The generator is not producing the structure it claims. Fix it before")
        print("any ML work -- a model trained on this data would report meaningless")
        print("metrics.")
        return 1

    print(f"\nGATE PASSED: all {len(checks)} checks. Phase 1 data is fit for modelling.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
