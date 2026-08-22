"""Saving and loading model artifacts.

A model binary on its own is unusable six months later: you cannot tell what
code produced it, what data it saw, or whether the feature list still matches
what the serving code builds. Every save writes a `ModelCard` alongside the
binary answering those questions, and every load checks the card.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

import joblib

from forecasting.types import ModelCard


def current_git_sha() -> str | None:
    """Best-effort commit id, so a prediction can be traced to its code.

    Returns None outside a git checkout (a container, a released wheel) rather
    than failing: provenance is valuable, not mandatory.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip() or None if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def save_model(
    model: Any,
    card: ModelCard,
    directory: Path,
) -> tuple[Path, Path]:
    """Write `<name>.joblib` and `<name>.card.json`."""
    directory.mkdir(parents=True, exist_ok=True)
    model_path = directory / f"{card.name}.joblib"
    card_path = directory / f"{card.name}.card.json"
    joblib.dump(model, model_path)
    card_path.write_text(json.dumps(card.as_dict(), indent=2), encoding="utf-8")
    return model_path, card_path


def load_model(name: str, directory: Path) -> tuple[Any, dict[str, Any]]:
    """Load a model and its card.

    Raises FileNotFoundError with an actionable message rather than a bare
    traceback, because the overwhelmingly common cause is simply that nobody has
    run the training script in this checkout.
    """
    model_path = directory / f"{name}.joblib"
    card_path = directory / f"{name}.card.json"
    if not model_path.exists():
        raise FileNotFoundError(
            f"no trained model at {model_path}. Run: uv run python scripts/train_models.py"
        )
    model = joblib.load(model_path)
    card = json.loads(card_path.read_text(encoding="utf-8")) if card_path.exists() else {}
    return model, card


def build_card(
    *,
    name: str,
    kind: str,
    seed: int,
    feature_names: list[str],
    train_rows: int,
    train_date_range: tuple[str, str],
    params: dict[str, Any],
    metrics: dict[str, Any],
    threshold: float | None = None,
    threshold_rationale: str | None = None,
) -> ModelCard:
    return ModelCard(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        trained_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        git_sha=current_git_sha(),
        seed=seed,
        feature_names=feature_names,
        train_rows=train_rows,
        train_date_range=train_date_range,
        params=params,
        metrics=metrics,
        threshold=threshold,
        threshold_rationale=threshold_rationale,
    )
