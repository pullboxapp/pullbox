#!/usr/bin/env python
"""Run one standardized IU7 import target lane and write bounded JSON."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from pullbox.performance.baseline import write_report  # noqa: E402
from pullbox.performance.import_target_harness import (  # noqa: E402
    ImportTargetBackend,
    ImportTargetCacheState,
    ImportTargetConfig,
    ImportTargetScaleProfile,
    ImportTargetSourceLane,
    build_import_target_report,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument(
        "--backend",
        choices=tuple(item.value for item in ImportTargetBackend),
        required=True,
    )
    parser.add_argument(
        "--scale-profile",
        choices=tuple(item.value for item in ImportTargetScaleProfile),
        required=True,
    )
    parser.add_argument(
        "--source-lane",
        choices=tuple(item.value for item in ImportTargetSourceLane),
        required=True,
    )
    parser.add_argument(
        "--cache-state",
        choices=tuple(item.value for item in ImportTargetCacheState),
        required=True,
    )
    parser.add_argument("--injection-point", default="none")
    parser.add_argument(
        "--api-url",
        action="append",
        default=[],
        help="Absolute endpoint as Label=https://host/path. Repeatable.",
    )
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--environment-label", required=True)
    parser.add_argument("--filesystem-label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        config = ImportTargetConfig(
            repo_root=args.repo_root.resolve(),
            seed=args.seed,
            backend=ImportTargetBackend(args.backend),
            scale_profile=ImportTargetScaleProfile(args.scale_profile),
            source_lane=ImportTargetSourceLane(args.source_lane),
            cache_state=ImportTargetCacheState(args.cache_state),
            injection_point=args.injection_point,
            api_urls=tuple(args.api_url),
            samples=args.samples,
            timeout_seconds=args.timeout,
            environment_label=args.environment_label,
            filesystem_label=args.filesystem_label,
        )
    except ValueError as exc:
        _build_parser().error(str(exc))

    report = build_import_target_report(config)
    write_report(report, args.output)
    gates = report["gate_evaluation"]
    return 0 if isinstance(gates, dict) and gates.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
