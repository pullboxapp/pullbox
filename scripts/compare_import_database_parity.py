#!/usr/bin/env python
"""Compare bounded SQLite and PostgreSQL import target reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from pullbox.performance.baseline import write_report  # noqa: E402
from pullbox.performance.import_target_harness import (  # noqa: E402
    build_import_database_parity_report,
)


def _read_report(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("target report must be a JSON object")
    return cast("dict[str, object]", value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_import_database_parity_report(
            _read_report(args.left),
            _read_report(args.right),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    write_report(report, args.output)
    return 0 if report["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
