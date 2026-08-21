#!/usr/bin/env python
"""Architectural fitness function: keep `forecasting/` and `optimizer/` pure.

CLAUDE.md requires that the optimization and forecasting modules have no
FastAPI or DB imports so they stay independently testable. A convention that is
not checked decays, so this script parses every module in those packages with
`ast` and fails CI on a forbidden import.

`ast` rather than regex: it will not be fooled by a banned name appearing in a
string, comment, or docstring.

Usage:  python scripts/check_purity.py
Exit:   0 clean, 1 violation found.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PURE_PACKAGES = ("forecasting", "optimizer")
FORBIDDEN_ROOTS = frozenset(
    {
        "fastapi",
        "sqlalchemy",
        "alembic",
        "celery",
        "app",
        "asyncpg",
        "psycopg",
        "redis",
        "anthropic",
    }
)

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _root_module(name: str) -> str:
    return name.split(".", 1)[0]


def violations_in(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _root_module(alias.name) in FORBIDDEN_ROOTS:
                    found.append((node.lineno, alias.name))
        # `level > 0` is a relative import (`from .x import y`) - always fine.
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and _root_module(node.module) in FORBIDDEN_ROOTS
        ):
            found.append((node.lineno, node.module))
    return found


def main() -> int:
    failures = 0
    for package in PURE_PACKAGES:
        pkg_dir = BACKEND_ROOT / package
        if not pkg_dir.is_dir():
            print(f"warn: {package}/ does not exist yet - skipping")
            continue
        for py_file in sorted(pkg_dir.rglob("*.py")):
            for lineno, module in violations_in(py_file):
                rel = py_file.relative_to(BACKEND_ROOT)
                print(f"IMPURE {rel}:{lineno}: forbidden import '{module}'")
                failures += 1

    if failures:
        print(
            f"\n{failures} purity violation(s). {'/'.join(PURE_PACKAGES)} must stay free of web/DB imports."
        )
        return 1
    print(f"purity OK - {'/'.join(PURE_PACKAGES)} import no web/DB modules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
