#!/usr/bin/env python3
"""Extract a curated release section from CHANGELOG.md."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HEADING_RE = re.compile(
    r"""
    ^\#\#\s+
    \[?(?P<version>[^\]\s]+)\]?
    (?:\s+-\s+.+)?
    \s*$
    """,
    re.VERBOSE,
)


class ChangelogSectionError(ValueError):
    """Raised when a requested changelog section is missing or invalid."""


def extract_changelog_section(markdown: str, version: str) -> str:
    """Return the body for a version heading in Keep a Changelog-style Markdown."""
    lines = markdown.splitlines()
    start_index: int | None = None

    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match and match.group("version") == version:
            start_index = index + 1
            break

    if start_index is None:
        raise ChangelogSectionError(
            f"CHANGELOG.md does not contain a release section for {version!r}."
        )

    end_index = len(lines)
    for index in range(start_index, len(lines)):
        if lines[index].startswith("## "):
            end_index = index
            break

    section = "\n".join(lines[start_index:end_index]).strip()
    if not _has_release_content(section):
        raise ChangelogSectionError(
            f"CHANGELOG.md release section for {version!r} is empty."
        )

    return f"{section}\n"


def _has_release_content(section: str) -> bool:
    """Reject sections that only contain category headings or blank lines."""
    for line in section.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("### "):
            return True
    return False


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract or validate a curated CHANGELOG.md release section."
    )
    parser.add_argument("--version", required=True, help="Release version without the v prefix.")
    parser.add_argument(
        "--changelog",
        default="CHANGELOG.md",
        type=Path,
        help="Path to CHANGELOG.md.",
    )
    parser.add_argument("--output", type=Path, help="Write the section to this file.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the section exists without printing it.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    try:
        section = extract_changelog_section(
            args.changelog.read_text(encoding="utf-8"),
            args.version,
        )
    except (OSError, ChangelogSectionError) as exc:
        print(f"changelog error: {exc}", file=sys.stderr)
        return 1

    if args.check:
        print(f"CHANGELOG.md contains a curated section for {args.version}.")
        return 0

    if args.output:
        args.output.write_text(section, encoding="utf-8")
    else:
        print(section, end="")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
