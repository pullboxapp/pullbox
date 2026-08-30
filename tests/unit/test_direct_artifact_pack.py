"""Tests for safe same-series direct-download pack extraction."""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

import pytest

from pullbox.services.direct_artifact_pack import (
    DirectArtifactPackError,
    _issue_path_token,
    extract_same_series_issue_files,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_nested_pack(path: Path, *names: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, b"nested comic")


def test_large_issue_path_token_never_uses_float_or_scientific_suffixes() -> None:
    assert _issue_path_token(1_000_000.0) == "1000000"


def test_extracts_separate_contiguous_issue_files(tmp_path: Path) -> None:
    pack = tmp_path / "Alien #5-6.cbz"
    _write_nested_pack(
        pack,
        "Alien - The Friendliest Facehugger #5.cbz",
        "Alien - The Friendliest Facehugger #6.cbz",
    )

    extracted = extract_same_series_issue_files(
        pack,
        destination=tmp_path / "extracted",
        expected_issue_numbers=frozenset({"5", "6"}),
        expected_series_titles=frozenset({"Alien - The Friendliest Facehugger"}),
    )

    assert set(extracted) == {5.0, 6.0}
    assert extracted[5.0].read_bytes() == b"nested comic"
    assert extracted[6.0].read_bytes() == b"nested comic"


def test_accepts_nested_files_using_a_configured_alternate_series_name(tmp_path: Path) -> None:
    pack = tmp_path / "The Aliens #5-6.cbz"
    _write_nested_pack(
        pack,
        "The Aliens #5.cbz",
        "The Aliens #6.cbz",
    )

    extracted = extract_same_series_issue_files(
        pack,
        destination=tmp_path / "extracted",
        expected_issue_numbers=frozenset({"5", "6"}),
        expected_series_titles=frozenset({"Alien - The Friendliest Facehugger", "The Aliens"}),
    )

    assert set(extracted) == {5.0, 6.0}


def test_rejects_a_combined_comic_with_only_page_images(tmp_path: Path) -> None:
    pack = tmp_path / "Alien #5-6.cbz"
    _write_nested_pack(pack, "001.jpg", "002.jpg")

    with pytest.raises(DirectArtifactPackError, match="separate issue files") as caught:
        extract_same_series_issue_files(
            pack,
            destination=tmp_path / "extracted",
            expected_issue_numbers=frozenset({"5", "6"}),
            expected_series_titles=frozenset({"Alien - The Friendliest Facehugger"}),
        )

    assert caught.value.code == "direct_pack_combined_file"


def test_rejects_pack_when_a_declared_issue_file_is_missing(tmp_path: Path) -> None:
    pack = tmp_path / "Alien #5-6.cbz"
    _write_nested_pack(pack, "Alien - The Friendliest Facehugger #5.cbz")

    with pytest.raises(DirectArtifactPackError, match="does not contain every issue") as caught:
        extract_same_series_issue_files(
            pack,
            destination=tmp_path / "extracted",
            expected_issue_numbers=frozenset({"5", "6"}),
            expected_series_titles=frozenset({"Alien - The Friendliest Facehugger"}),
        )

    assert caught.value.code == "direct_pack_incomplete"


def test_rejects_nested_issue_file_for_a_different_series(tmp_path: Path) -> None:
    pack = tmp_path / "Alien #5-6.cbz"
    _write_nested_pack(
        pack,
        "Alien - The Friendliest Facehugger #5.cbz",
        "Other Series #6.cbz",
    )

    with pytest.raises(DirectArtifactPackError, match="different series") as caught:
        extract_same_series_issue_files(
            pack,
            destination=tmp_path / "extracted",
            expected_issue_numbers=frozenset({"5", "6"}),
            expected_series_titles=frozenset({"Alien - The Friendliest Facehugger"}),
        )

    assert caught.value.code == "direct_pack_mixed_series"
