"""Optimizer vs FCFS baseline. The number, and the conditions it holds under.

    uv run python scripts/benchmark.py

Two runs, and BOTH are reported:

**1. As-is.** Every weekday in a held-out window, scheduled by both approaches
from the real appointment data. This is the honest headline for this clinic as
it currently operates.

**2. Load sweep.** The same days with each doctor's working windows compressed,
so the same appointments must fit into less time. This raises utilisation and
creates the contention that scheduling exists to resolve. Reported as a curve:
improvement against utilisation.

--------------------------------------------------------------------------
WHY THE SWEEP EXISTS, STATED PLAINLY
--------------------------------------------------------------------------
The as-is result is approximately zero, and that is a real finding rather than a
disappointment to be engineered away. The synthetic clinic runs at about 17%
utilisation with roughly four appointments per doctor per day; almost nothing
competes for the same slot, so both schedulers place nearly every appointment at
exactly the time it was requested. An optimizer cannot improve a schedule that
has no conflicts, and reporting a percentage from such a day would be reporting
noise.

The sweep is therefore NOT an attempt to find a flattering number. It answers
the question the as-is result raises: *at what load does this start to matter?*
A scheduler that helps only above 60% utilisation is a useful thing to know
about, and it is a far more defensible claim than a single percentage detached
from the conditions that produced it.

Compressing working hours is the honest way to add contention here. Doctors are
fixed to their appointments, so removing a doctor would make their appointments
unschedulable rather than contended; shortening the day keeps every appointment
valid and simply reduces the room to place it. It also corresponds to something
real: a half-day, a training afternoon, or a clinic short-staffed by illness.

--------------------------------------------------------------------------
REPRODUCIBILITY
--------------------------------------------------------------------------
CP-SAT runs single-threaded with a fixed seed, and the waiting-room simulation
uses the same seed for both schedules, so the comparison is paired: both see
identical sampled durations and attendance. Two runs of this script produce
identical numbers.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.services.optimizer_service import NoSuchClinicDayError, build_request
from optimizer import greedy, model, simulate
from optimizer.objective import DEFAULT_SLACK, NO_SLACK, SlackPolicy
from optimizer.score import improvement, score_solution
from optimizer.types import AppointmentRequest, DoctorDay, ScheduleRequest

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("benchmark")

REPORTS = Path(__file__).resolve().parent.parent / "reports" / "benchmark"

# Fractions of each doctor's working day retained. 1.0 is the clinic as it runs;
# 0.25 keeps a quarter of the hours, so the same appointments contend for four
# times less time.
COMPRESSION_FACTORS = [1.0, 0.6, 0.45, 0.35, 0.28, 0.22]


@dataclass
class DayResult:
    date: str
    appointments: int
    utilisation_pct: float
    baseline: dict[str, Any]
    optimized: dict[str, Any]
    improvement: dict[str, float]
    simulated_wait_before: float
    simulated_wait_after: float
    solve_time_ms: int
    status: str


def _utilisation(request: ScheduleRequest) -> float:
    busy: dict[int, int] = {}
    for appointment in request.appointments:
        busy[appointment.doctor_id] = (
            busy.get(appointment.doctor_id, 0) + appointment.duration_minutes
        )
    shares = [
        busy.get(doctor.doctor_id, 0) / doctor.available_minutes * 100
        for doctor in request.doctors
        if doctor.available_minutes > 0
    ]
    return round(statistics.fmean(shares), 1) if shares else 0.0


def _compress(request: ScheduleRequest, factor: float) -> ScheduleRequest:
    """Fit the same day's demand into a shorter working day.

    Two things are compressed together, and both are necessary:

    1. Each doctor's working windows are shortened, keeping the break as a gap.
    2. Each appointment's REQUESTED time is remapped proportionally into the
       shortened window.

    Compressing only the windows produces nonsense: an appointment requested at
    17:00 cannot sit in a window that now ends at 13:00, and because the model
    forbids moving anyone earlier than they asked, the day becomes infeasible
    for a reason that has nothing to do with scheduling quality. That is exactly
    what happened on the first attempt -- all but one day dropped out below a
    0.6 factor, and a sweep computed from one surviving day is worthless.

    Remapping keeps the shape of demand (who wants early, who wants late, in
    what order) while squeezing it into less room. That is the definition of
    load, and it is what creates genuine contention.
    """
    longest = max((a.duration_minutes for a in request.appointments), default=30)

    compressed_windows: dict[int, tuple[tuple[int, int], ...]] = {}
    originals: dict[int, tuple[tuple[int, int], ...]] = {}
    for doctor in request.doctors:
        windows = []
        for start, end in doctor.windows:
            length = max(int((end - start) * factor), longest)
            windows.append((start, min(end, start + length)))
        compressed_windows[doctor.doctor_id] = tuple(windows)
        originals[doctor.doctor_id] = doctor.windows

    def remap(appointment: AppointmentRequest) -> int:
        """Scale a requested time into the compressed version of its window."""
        original = originals.get(appointment.doctor_id, ())
        shrunk = compressed_windows.get(appointment.doctor_id, ())
        for (o_start, o_end), (c_start, c_end) in zip(original, shrunk, strict=False):
            if o_start <= appointment.requested_start_minute < o_end:
                position = (appointment.requested_start_minute - o_start) / max(o_end - o_start, 1)
                target = c_start + position * max(c_end - c_start - appointment.duration_minutes, 0)
                grid = request.granularity_minutes
                return int(target // grid * grid)
        return appointment.requested_start_minute

    doctors = tuple(
        DoctorDay(
            doctor_id=doctor.doctor_id,
            windows=compressed_windows[doctor.doctor_id],
            specialty_ids=doctor.specialty_ids,
        )
        for doctor in request.doctors
    )
    appointments = tuple(
        replace(appointment, requested_start_minute=remap(appointment))
        for appointment in request.appointments
    )

    return ScheduleRequest(
        clinic_id=request.clinic_id,
        date=request.date,
        open_minute=request.open_minute,
        close_minute=request.close_minute,
        appointments=appointments,
        doctors=doctors,
        rooms=request.rooms,
        granularity_minutes=request.granularity_minutes,
    )


def run_day(
    request: ScheduleRequest,
    *,
    simulate_runs: int,
    time_limit: float,
    slack: SlackPolicy = DEFAULT_SLACK,
) -> DayResult | None:
    try:
        optimized = model.solve(request, time_limit_seconds=time_limit, slack=slack)
    except model.InfeasibleScheduleError as exc:
        log.debug("%s skipped: %s", request.date, exc)
        return None
    if not optimized.is_usable:
        log.debug("%s skipped: solver returned %s", request.date, optimized.solver_status)
        return None

    # Same slack policy for both: the comparison is the algorithm, not the policy.
    baseline = greedy.solve(request, slack=slack)
    optimized_score = score_solution(optimized, request)
    baseline_score = score_solution(baseline, request)

    # Same seed for both: a paired comparison, so a difference is the schedule
    # rather than the luck of the sampled durations.
    before = simulate.simulate(baseline, request, runs=simulate_runs, seed=42)
    after = simulate.simulate(optimized, request, runs=simulate_runs, seed=42)

    return DayResult(
        date=str(request.date),
        appointments=len(request.appointments),
        utilisation_pct=_utilisation(request),
        baseline=baseline_score.as_dict(),
        optimized=optimized_score.as_dict(),
        improvement=improvement(baseline_score, optimized_score),
        simulated_wait_before=round(before.mean_wait_minutes, 3),
        simulated_wait_after=round(after.mean_wait_minutes, 3),
        solve_time_ms=optimized.solve_time_ms,
        status=optimized.solver_status,
    )


def summarise(results: list[DayResult]) -> dict[str, Any]:
    if not results:
        return {"days": 0}

    def spread(values: list[float]) -> dict[str, float]:
        ordered = sorted(values)
        return {
            "mean": round(statistics.fmean(ordered), 2),
            "median": round(statistics.median(ordered), 2),
            "p10": round(ordered[int(len(ordered) * 0.1)], 2),
            "p90": round(ordered[int(len(ordered) * 0.9)], 2),
            "min": round(ordered[0], 2),
            "max": round(ordered[-1], 2),
        }

    delay = [r.improvement["delay_pct"] for r in results]
    idle = [r.improvement["idle_pct"] for r in results]
    overtime = [r.improvement["overtime_pct"] for r in results]
    objective = [r.improvement["objective_pct"] for r in results]
    wait = [
        (
            round(
                (r.simulated_wait_before - r.simulated_wait_after) / r.simulated_wait_before * 100,
                2,
            )
            if r.simulated_wait_before > 0
            else 0.0
        )
        for r in results
    ]

    return {
        "days": len(results),
        "mean_utilisation_pct": round(statistics.fmean([r.utilisation_pct for r in results]), 1),
        "mean_appointments_per_day": round(statistics.fmean([r.appointments for r in results]), 1),
        "delay_reduction_pct": spread(delay),
        "idle_reduction_pct": spread(idle),
        "overtime_reduction_pct": spread(overtime),
        "objective_reduction_pct": spread(objective),
        "simulated_wait_reduction_pct": spread(wait),
        # The honest denominators. "Improves 40% of days" is a materially
        # different claim from "improves every day", and a mean hides which.
        "days_optimizer_strictly_better": sum(1 for v in objective if v > 0.01),
        "days_identical": sum(1 for v in objective if abs(v) <= 0.01),
        "days_worse": sum(1 for v in objective if v < -0.01),
        "solve_time_ms": spread([float(r.solve_time_ms) for r in results]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clinic-id", type=int, default=1)
    parser.add_argument("--days", type=int, default=60, help="weekdays to benchmark")
    parser.add_argument("--simulate-runs", type=int, default=100)
    parser.add_argument("--time-limit", type=float, default=10.0)
    args = parser.parse_args()

    engine = create_engine(settings.database_url_sync, future=True)
    with engine.connect() as conn:
        dates = (
            conn.execute(
                text(
                    "SELECT DISTINCT appointment_date FROM appointment "
                    "WHERE clinic_id = :c AND status <> 'cancelled' "
                    "AND EXTRACT(ISODOW FROM appointment_date) < 6 "
                    "AND appointment_date < CURRENT_DATE "
                    "ORDER BY appointment_date DESC LIMIT :n"
                ),
                {"c": args.clinic_id, "n": args.days},
            )
            .scalars()
            .all()
        )

    if not dates:
        log.error("no appointments found; run scripts/generate_data.py first")
        return 1
    dates = sorted(dates)
    log.info("benchmarking %d weekdays: %s .. %s", len(dates), dates[0], dates[-1])

    requests: list[ScheduleRequest] = []
    for on_date in dates:
        try:
            requests.append(build_request(engine, args.clinic_id, on_date))
        except NoSuchClinicDayError:
            continue

    # Each policy is run end to end, with BOTH schedulers under it. Comparing an
    # optimizer that reserves buffer against a baseline that packs back-to-back
    # would measure the policy, not the algorithm.
    policies: list[tuple[str, SlackPolicy]] = [
        ("no_slack", NO_SLACK),
        ("with_slack", DEFAULT_SLACK),
    ]
    runs: dict[str, dict[str, Any]] = {}

    for name, slack in policies:
        log.info("policy %s (slack fraction %.2f)", name, slack.fraction)

        as_is = [
            r
            for r in (
                run_day(
                    req,
                    simulate_runs=args.simulate_runs,
                    time_limit=args.time_limit,
                    slack=slack,
                )
                for req in requests
            )
            if r is not None
        ]
        as_is_summary = summarise(as_is)
        log.info(
            "  as-is: %d days at %.1f%% utilisation -> objective %+.2f%%, sim wait %+.2f%%",
            as_is_summary["days"],
            as_is_summary["mean_utilisation_pct"],
            as_is_summary["objective_reduction_pct"]["mean"],
            as_is_summary["simulated_wait_reduction_pct"]["mean"],
        )

        sweep: list[dict[str, Any]] = []
        for factor in COMPRESSION_FACTORS:
            compressed = [_compress(req, factor) for req in requests]
            results = [
                r
                for r in (
                    run_day(
                        req,
                        simulate_runs=args.simulate_runs,
                        time_limit=args.time_limit,
                        slack=slack,
                    )
                    for req in compressed
                )
                if r is not None
            ]
            # A point computed from a handful of surviving days is noise rather
            # than a measurement, so it is dropped instead of plotted.
            if len(results) < 5:
                continue
            point = summarise(results)
            point["compression_factor"] = factor
            sweep.append(point)
            log.info(
                "  %.0f%% day -> util %.1f%%, delay %+.1f%%, objective %+.1f%%, "
                "sim wait %+.1f%%, better %d/%d",
                factor * 100,
                point["mean_utilisation_pct"],
                point["delay_reduction_pct"]["mean"],
                point["objective_reduction_pct"]["mean"],
                point["simulated_wait_reduction_pct"]["mean"],
                point["days_optimizer_strictly_better"],
                point["days"],
            )

        runs[name] = {
            "slack_fraction": slack.fraction,
            "as_is": as_is_summary,
            "load_sweep": sweep,
            "per_day_as_is": [r.__dict__ for r in as_is],
        }

    REPORTS.mkdir(parents=True, exist_ok=True)
    payload = {
        "clinic_id": args.clinic_id,
        "window": {"from": str(dates[0]), "to": str(dates[-1])},
        "policies": runs,
        "reproducibility": {
            "cpsat_workers": 1,
            "cpsat_seed": 42,
            "simulation_seed": 42,
            "simulation_runs": args.simulate_runs,
            "note": "both schedulers run under the same slack policy in each comparison",
        },
    }
    (REPORTS / "benchmark.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _plot(runs)

    print("\n" + "=" * 88)
    print("OPTIMIZER vs FCFS BASELINE  (positive = optimizer better)")
    print("=" * 88)
    print(f"window: {dates[0]} .. {dates[-1]}")
    for name, block in runs.items():
        as_is = block["as_is"]
        print("-" * 88)
        print(f"POLICY: {name}  (slack fraction {block['slack_fraction']:.2f})")
        print(
            f"  as-is  util {as_is['mean_utilisation_pct']}%  "
            f"delay {as_is['delay_reduction_pct']['mean']:+.2f}%  "
            f"objective {as_is['objective_reduction_pct']['mean']:+.2f}%  "
            f"sim wait {as_is['simulated_wait_reduction_pct']['mean']:+.2f}%  "
            f"(better/same/worse {as_is['days_optimizer_strictly_better']}/"
            f"{as_is['days_identical']}/{as_is['days_worse']})"
        )
        print(
            f"  {'util%':>7} {'delay%':>9} {'idle%':>9} {'objective%':>11} "
            f"{'sim wait%':>10} {'better':>10}"
        )
        for point in block["load_sweep"]:
            print(
                f"  {point['mean_utilisation_pct']:>7.1f} "
                f"{point['delay_reduction_pct']['mean']:>+9.2f} "
                f"{point['idle_reduction_pct']['mean']:>+9.2f} "
                f"{point['objective_reduction_pct']['mean']:>+11.2f} "
                f"{point['simulated_wait_reduction_pct']['mean']:>+10.2f} "
                f"{point['days_optimizer_strictly_better']:>5}/{point['days']:<4}"
            )
    print("-" * 88)
    timing = runs["with_slack"]["as_is"]["solve_time_ms"]
    print(f"  solve time: median {timing['median']:.0f} ms, p90 {timing['p90']:.0f} ms")
    print(f"  report: {(REPORTS / 'benchmark.json')}")
    print("=" * 88)
    return 0


def _plot(runs: dict[str, dict[str, Any]]) -> None:
    """Improvement against utilisation, one panel per slack policy."""
    if not runs:
        return
    fig, axes = plt.subplots(1, len(runs), figsize=(11, 4.2), sharey=True)
    if len(runs) == 1:
        axes = [axes]

    for ax, (name, block) in zip(axes, runs.items(), strict=False):
        sweep = block["load_sweep"]
        if not sweep:
            continue
        x = [p["mean_utilisation_pct"] for p in sweep]
        for key, label, colour in (
            ("delay_reduction_pct", "patient delay", "#0d4a76"),
            ("idle_reduction_pct", "doctor idle", "#2a7f8f"),
            ("simulated_wait_reduction_pct", "simulated waiting room", "#ff9800"),
        ):
            ax.plot(x, [p[key]["mean"] for p in sweep], marker="o", label=label, color=colour)
        ax.axhline(0, color="#74777f", lw=1)
        ax.set_title(f"{name} (slack {block['slack_fraction']:.2f})", fontsize=10)
        ax.set_xlabel("Mean doctor utilisation (%)")
        ax.tick_params(labelsize=8)

    axes[0].set_ylabel("Reduction vs FCFS baseline (%)")
    axes[-1].legend(fontsize=8)
    fig.suptitle("Optimizer vs FCFS: both schedulers under the same slack policy", fontsize=11)
    fig.tight_layout()
    fig.savefig(REPORTS / "improvement_vs_utilisation.png", dpi=120)
    plt.close(fig)


if __name__ == "__main__":
    raise SystemExit(main())
