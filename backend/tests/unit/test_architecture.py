"""Architecture tests.

CLAUDE.md requires `forecasting/` and `optimizer/` to be pure — no FastAPI or DB
imports — so they stay independently testable. This runs the same checker CI
runs, so a violation fails locally at `pytest` time too.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT / "scripts"))

from check_purity import main as purity_main  # noqa: E402


def test_pure_packages_have_no_web_or_db_imports() -> None:
    assert purity_main() == 0, "forecasting/ or optimizer/ imported a forbidden module"
