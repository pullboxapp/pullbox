"""CLI entrypoint for deterministic IU9 folder-import fixtures."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def build_parser() -> argparse.ArgumentParser:
    """Build the folder-fixture command-line contract."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Absent or empty output directory")
    parser.add_argument("--seed", type=int, default=1300, help="Deterministic fixture seed")
    parser.add_argument(
        "--cbr-seed-dir",
        type=Path,
        help="Optional directory containing fixed RAR3/RAR5 CBR seeds and descriptor",
    )
    parser.add_argument("--cbr-rar3-sha256", help="Optional SHA-256 pin for iu9-rar3.cbr")
    parser.add_argument("--cbr-rar5-sha256", help="Optional SHA-256 pin for iu9-rar5.cbr")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Generate one fresh folder-import fixture tree."""
    from scripts.iu9_acceptance_fixtures.folder import generate_folder_fixture

    parser = build_parser()
    args = parser.parse_args(argv)
    pins = {
        seed_id: digest
        for seed_id, digest in (
            ("rar3", args.cbr_rar3_sha256),
            ("rar5", args.cbr_rar5_sha256),
        )
        if digest is not None
    }
    if pins and args.cbr_seed_dir is None:
        parser.error("CBR SHA-256 pins require --cbr-seed-dir")
    generate_folder_fixture(
        args.output,
        seed=args.seed,
        cbr_seed_dir=args.cbr_seed_dir,
        cbr_expected_sha256=pins or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
