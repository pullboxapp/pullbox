"""Generate a deterministic Local Comic Vine-backed import scale fixture."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def build_parser() -> argparse.ArgumentParser:
    """Build the scale-fixture command-line contract."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path, help="Read-only localcv.db path")
    parser.add_argument("output", type=Path, help="Output path, which must not exist")
    parser.add_argument("--series-count", type=int, default=50_000)
    parser.add_argument("--file-count", type=int, default=200_000)
    parser.add_argument("--seed", type=int, default=1300)
    parser.add_argument(
        "--profile",
        choices=("balanced", "realistic-skew"),
        default="balanced",
    )
    parser.add_argument("--max-issues-per-series", type=int, default=250)
    parser.add_argument(
        "--layout-profile",
        choices=("series", "mixed"),
        default="series",
        help="Use uniform series folders for certification or mixed layouts for stress testing",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Select and summarize the workload without writing fixture files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Plan or generate one exact fixture."""
    from scripts.import_scale_fixtures.generator import (
        FixtureRequest,
        generate_import_scale_fixture,
        plan_import_scale_fixture,
    )

    args = build_parser().parse_args(argv)
    request = FixtureRequest(
        catalog_path=args.catalog,
        output_path=args.output,
        series_count=args.series_count,
        file_count=args.file_count,
        seed=args.seed,
        profile=args.profile,
        max_issues_per_series=args.max_issues_per_series,
        layout_profile=args.layout_profile,
    )
    if args.plan_only:
        plan = plan_import_scale_fixture(request)
        summary: dict[str, object] = {
            "profile": plan.profile,
            "seed": plan.seed,
            "series_count": len(plan.series),
            "file_count": sum(len(series.issues) for series in plan.series),
            "max_issues_per_series": plan.max_issues_per_series,
            "layout_profile": plan.layout_profile,
        }
    else:
        summary = generate_import_scale_fixture(request)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
