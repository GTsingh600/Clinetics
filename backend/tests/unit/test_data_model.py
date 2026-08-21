"""Unit tests for the synthetic data generator's statistical models.

These are pure functions — no database, no I/O — so they test fast and pin the
*intended* structure independently of any particular generated dataset.

The division of labour matters: these tests assert the models are specified
correctly, while `scripts/validate_data.py` asserts the structure actually
survives into the generated rows. A model can be right and the generator still
wrong (bad sampling, capacity saturation flattening demand), so both are needed.
"""

from __future__ import annotations

import datetime as dt
import itertools
import random
import statistics
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from generate_data import (
    SPECIALTIES,
    Urgency,
    _hour_effect,
    _poisson,
    cancel_probability,
    expected_volume,
    no_show_probability,
    sample_duration,
    sample_lead_days,
)

BASE = {
    "weekday": 3,  # Wednesday, the neutral day
    "hour": 11,
    "patient_offset": 0.0,
    "is_new_patient": False,
    "urgency": Urgency.ROUTINE,
}


# --------------------------------------------------------------------------
# No-show model
# --------------------------------------------------------------------------
def test_no_show_rises_monotonically_with_lead_time() -> None:
    """The single most important property in the whole dataset.

    If this were flat, the Phase 3 classifier would have nothing to learn and
    its reported AUC would be meaningless.
    """
    probs = [no_show_probability(lead_days=d, **BASE) for d in range(60)]
    # itertools.pairwise, not zip(..., strict=True): the two slices are
    # deliberately different lengths.
    assert all(b > a for a, b in itertools.pairwise(probs))
    assert probs[-1] > probs[0] * 2


def test_lead_time_effect_saturates() -> None:
    """log1p means early days matter far more than late ones."""
    early = no_show_probability(lead_days=7, **BASE) - no_show_probability(lead_days=0, **BASE)
    late = no_show_probability(lead_days=67, **BASE) - no_show_probability(lead_days=60, **BASE)
    assert early > late


def test_new_patients_have_higher_no_show_rate() -> None:
    kw = {k: v for k, v in BASE.items() if k != "is_new_patient"}
    assert no_show_probability(lead_days=7, is_new_patient=True, **kw) > no_show_probability(
        lead_days=7, is_new_patient=False, **kw
    )


@pytest.mark.parametrize(
    ("higher", "lower"),
    [(Urgency.ROUTINE, Urgency.URGENT), (Urgency.URGENT, Urgency.EMERGENCY)],
)
def test_sicker_patients_turn_up(higher: Urgency, lower: Urgency) -> None:
    kw = {k: v for k, v in BASE.items() if k != "urgency"}
    assert no_show_probability(lead_days=3, urgency=higher, **kw) > no_show_probability(
        lead_days=3, urgency=lower, **kw
    )


def test_monday_and_friday_are_worse_than_midweek() -> None:
    kw = {k: v for k, v in BASE.items() if k != "weekday"}
    monday = no_show_probability(lead_days=7, weekday=1, **kw)
    friday = no_show_probability(lead_days=7, weekday=5, **kw)
    wednesday = no_show_probability(lead_days=7, weekday=3, **kw)
    assert monday > wednesday
    assert friday > wednesday


def test_hour_effect_is_u_shaped() -> None:
    """High at the open, dipping midday, high again at the close."""
    values = {h: _hour_effect(h) for h in range(8, 19)}
    trough_hour = min(values, key=lambda h: values[h])
    assert 11 <= trough_hour <= 13
    assert values[8] > values[trough_hour]
    assert values[18] > values[trough_hour]


def test_patient_offset_shifts_probability() -> None:
    """The mechanism that makes a patient's own history predictive."""
    kw = {k: v for k, v in BASE.items() if k != "patient_offset"}
    frequent = no_show_probability(lead_days=7, patient_offset=1.5, **kw)
    reliable = no_show_probability(lead_days=7, patient_offset=-1.5, **kw)
    assert frequent > reliable * 2


def test_probabilities_stay_in_range() -> None:
    for lead in (0, 1, 30, 90):
        for offset in (-3.0, 0.0, 3.0):
            for urgency in Urgency:
                kw = {k: v for k, v in BASE.items() if k not in {"patient_offset", "urgency"}}
                p = no_show_probability(
                    lead_days=lead, patient_offset=offset, urgency=urgency, **kw
                )
                assert 0.0 < p < 1.0


def test_cancellation_also_rises_with_lead_time() -> None:
    assert cancel_probability(30) > cancel_probability(0)
    assert 0.0 < cancel_probability(0) < 0.2


# --------------------------------------------------------------------------
# Duration model
# --------------------------------------------------------------------------
def test_duration_varies_by_specialty() -> None:
    rng = random.Random(7)
    means = {
        spec.slug: statistics.mean(sample_duration(rng, spec, False) for _ in range(400))
        for spec in SPECIALTIES
    }
    assert means["general-practice"] < means["cardiology"]
    assert max(means.values()) - min(means.values()) > 10


def test_new_patients_take_longer() -> None:
    rng = random.Random(11)
    spec = SPECIALTIES[0]
    new = statistics.mean(sample_duration(rng, spec, True) for _ in range(500))
    returning = statistics.mean(sample_duration(rng, spec, False) for _ in range(500))
    assert new > returning + 4


def test_duration_is_clamped_and_rounded() -> None:
    rng = random.Random(3)
    for spec in SPECIALTIES:
        for _ in range(200):
            d = sample_duration(rng, spec, True)
            assert 10 <= d <= 120
            assert d % 5 == 0


# --------------------------------------------------------------------------
# Lead time and demand
# --------------------------------------------------------------------------
def test_urgent_appointments_are_booked_at_shorter_notice() -> None:
    rng = random.Random(5)
    routine = statistics.mean(sample_lead_days(rng, Urgency.ROUTINE) for _ in range(500))
    urgent = statistics.mean(sample_lead_days(rng, Urgency.URGENT) for _ in range(500))
    emergency = statistics.mean(sample_lead_days(rng, Urgency.EMERGENCY) for _ in range(500))
    assert routine > urgent > emergency


def test_demand_drops_at_the_weekend() -> None:
    spec = SPECIALTIES[2]  # general practice
    monday = dt.date(2026, 6, 1)
    sunday = dt.date(2026, 6, 7)
    assert expected_volume(spec, monday, 10) > 4 * expected_volume(spec, sunday, 10)


def test_monday_is_the_busiest_weekday() -> None:
    spec = SPECIALTIES[2]
    week = [dt.date(2026, 6, 1) + dt.timedelta(days=i) for i in range(5)]
    volumes = [expected_volume(spec, d, 10) for d in week]
    assert volumes[0] == max(volumes)


def test_dermatology_peaks_in_the_evening_others_in_the_morning() -> None:
    """The specialty-specific intra-day shape, checked at the model level."""
    date = dt.date(2026, 6, 3)  # a Wednesday
    by_slug = {s.slug: s for s in SPECIALTIES}

    derm = by_slug["dermatology"]
    assert expected_volume(derm, date, 17) > expected_volume(derm, date, 9)

    gp = by_slug["general-practice"]
    assert expected_volume(gp, date, 9) > expected_volume(gp, date, 17)


def test_pediatrics_demand_is_bimodal() -> None:
    date = dt.date(2026, 6, 3)
    peds = next(s for s in SPECIALTIES if s.slug == "pediatrics")
    volumes = {h: expected_volume(peds, date, h) for h in range(8, 19)}
    # A dip between the morning and after-school peaks.
    assert volumes[9] > volumes[12] < volumes[15]


def test_poisson_sampler_has_the_right_mean() -> None:
    rng = random.Random(13)
    for lam in (0.2, 1.0, 4.0):
        draws = [_poisson(rng, lam) for _ in range(4000)]
        assert abs(statistics.mean(draws) - lam) < 0.15 * max(lam, 1.0)
    assert _poisson(rng, 0.0) == 0
