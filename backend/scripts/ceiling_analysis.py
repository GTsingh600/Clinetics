"""How good COULD the no-show model be? Answering "is 0.66 any good?"

    uv run python scripts/ceiling_analysis.py

A ROC-AUC of 0.66 looks poor next to the numbers people quote for classifiers,
and the honest response is not to argue about it but to compute the ceiling.

Two things are measured:

**1. The Bayes ceiling.** The generator draws each no-show as
`Bernoulli(p)`. Even a model that knows `p` exactly cannot predict the coin
flip, so its AUC is bounded well below 1. This script re-simulates the exact
process — importing the generator's own coefficients so nothing is transcribed —
while keeping the true `p`, then scores `p` against the labels it produced. That
number is the hard upper bound for ANY model on this data.

**2. An ablation on the real data.** How much does the patient-history feature
actually contribute, and how much is left on the table?

Why this matters beyond reassurance: it sets the range in which a *believable*
result lives. If this model reported AUC 0.95, the correct reaction would not be
delight but suspicion — on data whose ceiling is ~0.76, a score like that is
evidence of label leakage, not skill. Knowing the ceiling is what makes the
reported score falsifiable.

The simulated intermediate rows are indicative rather than exact: they assume
every non-latent coefficient is known perfectly and draw calendar features
uniformly, where the real data has correlated ones. The Bayes bound itself does
not depend on those assumptions.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score
from sqlalchemy import create_engine

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts"))

from generate_data import (  # noqa: E402
    B0_NO_SHOW,
    B_DOW,
    B_LEAD,
    B_NEW_PATIENT,
    B_URGENCY,
    PATIENT_PROPENSITY_ALPHA,
    PATIENT_PROPENSITY_BETA,
    PATIENT_PROPENSITY_MEAN,
    _hour_effect,
    _logit,
    _sigmoid,
    sample_lead_days,
    sample_urgency,
)

from app.core.config import settings  # noqa: E402
from app.services.ml_data_service import load_appointments  # noqa: E402
from forecasting import features, metrics  # noqa: E402
from forecasting.features import SMOOTHING_STRENGTH  # noqa: E402
from forecasting.no_show import NoShowModel  # noqa: E402
from forecasting.splits import final_holdout  # noqa: E402

SEED = 7
N_PATIENTS = 4000
APPTS_PER_PATIENT = 8  # ~ the real dataset: 31,352 resolved / 3,999 patients

HISTORY_FEATURES = [
    "patient_prior_appointments",
    "patient_prior_no_show_rate",
    "patient_prior_no_shows",
    "days_since_patient_last_appointment",
]


def simulate() -> dict[str, np.ndarray]:
    """Re-run the generative process, keeping the true probability behind each label."""
    rng = random.Random(SEED)
    baseline_logit = _logit(PATIENT_PROPENSITY_MEAN)

    p_true: list[float] = []
    p_without_latent: list[float] = []
    labels: list[int] = []
    patients: list[int] = []
    offsets: list[float] = []

    for patient in range(N_PATIENTS):
        propensity = min(
            max(rng.betavariate(PATIENT_PROPENSITY_ALPHA, PATIENT_PROPENSITY_BETA), 0.01), 0.95
        )
        offset = _logit(propensity) - baseline_logit

        for k in range(APPTS_PER_PATIENT):
            urgency = sample_urgency(rng)
            lead = sample_lead_days(rng, urgency)
            weekday = rng.randint(1, 7)
            hour = rng.randint(8, 18)

            observable = (
                B0_NO_SHOW
                + B_LEAD * math.log1p(lead)
                + B_DOW[weekday]
                + _hour_effect(hour)
                + B_NEW_PATIENT * float(k == 0)
                + B_URGENCY[urgency]
            )
            probability = _sigmoid(observable + offset)

            p_true.append(probability)
            p_without_latent.append(_sigmoid(observable))
            labels.append(1 if rng.random() < probability else 0)
            patients.append(patient)
            offsets.append(offset)

    return {
        "p_true": np.array(p_true),
        "p_without_latent": np.array(p_without_latent),
        "y": np.array(labels),
        "patient": np.array(patients),
        "offset": np.array(offsets),
    }


def with_estimated_history(data: dict[str, np.ndarray]) -> np.ndarray:
    """Score using the latent ESTIMATED from prior outcomes, as the real features do.

    Mirrors `features._patient_history`: an expanding count of prior no-shows,
    shrunk toward the base rate, converted back to a log-odds offset. This is the
    information a leakage-free model genuinely has access to.
    """
    y, base = data["y"], data["p_without_latent"]
    baseline_logit = _logit(PATIENT_PROPENSITY_MEAN)

    out = np.zeros(len(y))
    prior_n = prior_k = 0
    current = -1

    for i in range(len(y)):
        if data["patient"][i] != current:
            prior_n = prior_k = 0
            current = int(data["patient"][i])

        rate = (prior_k + SMOOTHING_STRENGTH * PATIENT_PROPENSITY_MEAN) / (
            prior_n + SMOOTHING_STRENGTH
        )
        rate = min(max(rate, 0.01), 0.99)
        observable = _logit(min(max(base[i], 1e-6), 1 - 1e-6))
        out[i] = _sigmoid(observable + _logit(rate) - baseline_logit)

        prior_n += 1
        prior_k += int(y[i])

    return out


def ablation() -> list[tuple[str, float, float]]:
    """How much does each feature group contribute on the REAL data?"""
    engine = create_engine(settings.database_url_sync, future=True)
    frame = load_appointments(engine)
    train, test = final_holdout(frame, "appointment_date", test_fraction=0.2)

    resolved = train[train["status"].isin(["completed", "no_show"])]
    base_rate = float((resolved["status"] == "no_show").mean())
    X_train, y_train = features.build_no_show_features(train, base_rate=base_rate)
    X_test, y_test = features.build_no_show_features(test, base_rate=base_rate)

    results = []
    for label, columns in (
        ("all features", list(X_train.columns)),
        ("without patient history", [c for c in X_train.columns if c not in HISTORY_FEATURES]),
        ("patient history only", HISTORY_FEATURES),
    ):
        model = NoShowModel(seed=42).fit(X_train[columns], y_train)
        scored = metrics.classification_metrics(
            y_test.to_numpy(), model.predict_proba(X_test[columns]), 0.5
        )
        results.append((label, scored.roc_auc, scored.pr_auc))
    return results


def main() -> int:
    data = simulate()
    y = data["y"]

    def line(name: str, scores: np.ndarray) -> tuple[float, float]:
        roc = roc_auc_score(y, scores)
        pr = average_precision_score(y, scores)
        print(f"  {name:<46} ROC-AUC {roc:.4f}   PR-AUC {pr:.4f}")
        return roc, pr

    print("=" * 84)
    print("CEILING ANALYSIS - how good could a no-show model be on this data?")
    print("=" * 84)
    print(
        f"simulated {len(y):,} appointments over {N_PATIENTS:,} patients "
        f"({APPTS_PER_PATIENT} each); positive rate {y.mean():.1%}"
    )
    print("-" * 84)
    bayes_roc, _ = line("BAYES ceiling - scores the TRUE probability", data["p_true"])
    floor_roc, _ = line("ceiling ignoring patient history", data["p_without_latent"])
    line("realistic - history estimated from prior visits", with_estimated_history(data))
    print("-" * 84)

    print("ABLATION on the real dataset (temporal holdout):")
    measured_roc = None
    for label, roc, pr in ablation():
        print(f"  {label:<46} ROC-AUC {roc:.4f}   PR-AUC {pr:.4f}")
        if label == "all features":
            measured_roc = roc
    print("=" * 84)

    assert measured_roc is not None
    captured = (measured_roc - 0.5) / (bayes_roc - 0.5)
    print(
        f"\nThe outcome is a Bernoulli draw, so the hard ceiling is {bayes_roc:.3f}, not 1.0.\n"
        f"Measured {measured_roc:.3f} captures {captured:.0%} of the signal available above chance.\n"
        f"Patient history is worth little here ({floor_roc:.3f} without it) because patients\n"
        f"average under four prior visits, so their latent propensity is estimated from\n"
        f"very few observations.\n\n"
        f"A model reporting 0.95 on this data would be evidence of label leakage, not skill."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
