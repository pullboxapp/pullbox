"""Tests for release changelog extraction."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "extract_changelog_section.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("extract_changelog_section", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


extractor = _load_script()


def test_extracts_requested_version_body_without_neighboring_sections() -> None:
    markdown = """# Changelog

## [Unreleased]

### Fixed

## [1.2.3] - 2026-06-16

Release summary.

### Fixed

- Fixed a thing.

## [1.2.2] - 2026-06-15

- Older fix.
"""

    section = extractor.extract_changelog_section(markdown, "1.2.3")

    assert "Release summary." in section
    assert "- Fixed a thing." in section
    assert "Older fix" not in section
    assert section.endswith("\n")


def test_extract_supports_unbracketed_version_heading() -> None:
    markdown = """# Changelog

## 1.2.3 - 2026-06-16

- Changed release process.
"""

    assert extractor.extract_changelog_section(markdown, "1.2.3") == (
        "- Changed release process.\n"
    )


def test_missing_version_raises_clear_error() -> None:
    with pytest.raises(extractor.ChangelogSectionError, match=r"1\.2\.3"):
        extractor.extract_changelog_section("# Changelog\n", "1.2.3")


def test_heading_only_version_raises_clear_error() -> None:
    markdown = """# Changelog

## [1.2.3] - 2026-06-16

### Added

### Fixed
"""

    with pytest.raises(extractor.ChangelogSectionError, match="empty"):
        extractor.extract_changelog_section(markdown, "1.2.3")


def test_cli_check_and_output_modes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    output = tmp_path / "section.md"
    changelog.write_text(
        """# Changelog

## [1.2.3] - 2026-06-16

- Release note.
""",
        encoding="utf-8",
    )

    assert (
        extractor.main(
            [
                "--version",
                "1.2.3",
                "--changelog",
                str(changelog),
                "--check",
            ]
        )
        == 0
    )
    assert "contains a curated section" in capsys.readouterr().out

    assert (
        extractor.main(
            [
                "--version",
                "1.2.3",
                "--changelog",
                str(changelog),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert output.read_text(encoding="utf-8") == "- Release note.\n"
