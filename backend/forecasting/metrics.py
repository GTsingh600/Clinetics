"""Scoring. Pure functions over arrays.

Kept separate from the models so the same metric code scores the model and its
baselines — if each computed its own, a difference in the numbers could be a
difference in the metric rather than in the model.
"""

from __future__ import annotations

import numpy as np
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    roc_auc_score,
)

from forecasting.types import ClassificationMetrics, RegressionMetrics


def classification_metrics(
    y_true: np.ndarray, y_prob: np.ndarray, threshold: float
) -> ClassificationMetrics:
    """Score a probabilistic classifier at one decision threshold.

    Reports threshold-free measures (ROC-AUC, PR-AUC, Brier) alongside the
    threshold-dependent ones, because they answer different questions: AUC says
    how well the model ranks, precision and recall say what happens when you act
    on it.

    PR-AUC matters more than ROC-AUC here. With a 22% positive rate, ROC-AUC is
    flattered by the large negative class; precision-recall focuses on the
    minority class you actually want to find.

    Brier score is the calibration check. A model can rank perfectly and still
    output probabilities that are uniformly too high, which breaks any use that
    treats the number as a real probability — such as summing predictions to get
    an expected number of no-shows for overbooking.
    """
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob, dtype=float)
    y_pred = (y_prob >= threshold).astype(int)

    tn, fp, fn, tp = confusion_matrix(y_true, y_pred, labels=[0, 1]).ravel()

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    accuracy = (tp + tn) / len(y_true) if len(y_true) else 0.0

    # A single-class test window makes AUC undefined rather than zero.
    both_classes = len(np.unique(y_true)) > 1
    return ClassificationMetrics(
        threshold=threshold,
        accuracy=accuracy,
        precision=precision,
        recall=recall,
        f1=f1,
        roc_auc=roc_auc_score(y_true, y_prob) if both_classes else float("nan"),
        pr_auc=average_precision_score(y_true, y_prob) if both_classes else float("nan"),
        brier=brier_score_loss(y_true, y_prob),
        true_negatives=int(tn),
        false_positives=int(fp),
        false_negatives=int(fn),
        true_positives=int(tp),
        support=len(y_true),
        positive_rate=float(y_true.mean()) if len(y_true) else 0.0,
    )


def regression_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> RegressionMetrics:
    """MAE, RMSE, signed bias, and R-squared.

    Both MAE and RMSE are reported because they disagree usefully: RMSE squares
    errors, so it is dominated by the occasional large miss, while MAE treats
    every minute equally. A model with low MAE and high RMSE is usually right
    and occasionally very wrong, which for scheduling matters more than the
    average suggests.

    Bias is signed on purpose. Systematic under-prediction and noisy-but-centred
    prediction can produce identical MAE with completely different operational
    consequences.
    """
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    errors = y_pred - y_true

    ss_res = float(np.sum(errors**2))
    ss_tot = float(np.sum((y_true - y_true.mean()) ** 2))
    return RegressionMetrics(
        mae=float(np.mean(np.abs(errors))),
        rmse=float(np.sqrt(np.mean(errors**2))),
        bias=float(np.mean(errors)),
        r2=1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan"),
        support=len(y_true),
    )


def precision_recall_curve_points(
    y_true: np.ndarray, y_prob: np.ndarray, n_points: int = 50
) -> list[dict[str, float]]:
    """Sample the precision/recall trade-off across thresholds.

    Committed as evidence so the chosen operating point can be seen in context
    rather than asserted.
    """
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob, dtype=float)
    out: list[dict[str, float]] = []
    for threshold in np.linspace(0.05, 0.95, n_points):
        m = classification_metrics(y_true, y_prob, float(threshold))
        out.append(
            {
                "threshold": round(float(threshold), 4),
                "precision": round(m.precision, 4),
                "recall": round(m.recall, 4),
                "f1": round(m.f1, 4),
            }
        )
    return out


def threshold_for_cost(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    *,
    cost_false_negative: float,
    cost_false_positive: float,
) -> tuple[float, str]:
    """Choose the threshold minimising expected cost, and explain the choice.

    The two errors are not symmetric:

    * A **false negative** is a no-show the model failed to flag. The slot is
      wasted: a doctor sits idle, and a patient who wanted that time did not
      get it.
    * A **false positive** is a patient flagged as likely to miss who then
      attends. If the flag drove an overbooking, two patients now want the same
      slot and someone waits.

    Which is worse is clinic policy, not a modelling fact, so it is an explicit
    parameter rather than a hidden default. The rationale is returned with the
    number so the two stay together in the model card.
    """
    y_true = np.asarray(y_true).astype(int)
    y_prob = np.asarray(y_prob, dtype=float)

    best_threshold, best_cost = 0.5, float("inf")
    for threshold in np.linspace(0.05, 0.95, 91):
        pred = (y_prob >= threshold).astype(int)
        fn = int(np.sum((y_true == 1) & (pred == 0)))
        fp = int(np.sum((y_true == 0) & (pred == 1)))
        cost = fn * cost_false_negative + fp * cost_false_positive
        if cost < best_cost:
            best_cost, best_threshold = cost, float(threshold)

    ratio = cost_false_negative / cost_false_positive
    rationale = (
        f"minimises expected cost with a missed no-show weighted {ratio:.1f}x an "
        f"over-booked slot (FN={cost_false_negative}, FP={cost_false_positive})"
    )
    return best_threshold, rationale
