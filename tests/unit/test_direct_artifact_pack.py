"""Tests for safe same-series direct-download pack extraction."""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

import pytest

from pullbox.services.direct_artifact_pack import (
    DirectArtifactPackError,
    _issue_path_token,
    extract_same_series_issue_files,
    is_separable_issue_pack,
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

    assert set(extracted) == {"5", "6"}
    assert extracted["5"].read_bytes() == b"nested comic"
    assert extracted["6"].read_bytes() == b"nested comic"


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

    assert set(extracted) == {"5", "6"}


def test_extracts_suffix_siblings_as_distinct_exact_issues(tmp_path: Path) -> None:
    pack = tmp_path / "Suffix Siblings.cbz"
    _write_nested_pack(
        pack,
        "Suffix Siblings #1AU.cbz",
        "Suffix Siblings #1B.cbz",
    )

    extracted = extract_same_series_issue_files(
        pack,
        destination=tmp_path / "extracted",
        expected_issue_numbers=frozenset({"1AU", "1B"}),
        expected_series_titles=frozenset({"Suffix Siblings"}),
    )

    assert set(extracted) == {"1AU", "1B"}
    assert extracted["1AU"].name == "issue-1AU.cbz"
    assert extracted["1B"].name == "issue-1B.cbz"


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


def test_structural_pack_detection_handles_valid_and_unreadable_archives(tmp_path: Path) -> None:
    pack = tmp_path / "Alien #5-6.cbz"
    _write_nested_pack(pack, "Alien #5.cbz", "Alien #6.cbz")

    assert is_separable_issue_pack(pack) is True
    assert is_separable_issue_pack(tmp_path / "missing.cbz") is False


def test_single_expected_issue_does_not_enter_pack_extraction(tmp_path: Path) -> None:
    assert (
        extract_same_series_issue_files(
            tmp_path / "missing.cbz",
            destination=tmp_path / "extracted",
            expected_issue_numbers=frozenset({"5"}),
            expected_series_titles=frozenset({"Alien"}),
        )
        == {}
    )


def test_pack_requires_a_normalizable_series_identity(tmp_path: Path) -> None:
    pack = tmp_path / "Alien #5-6.cbz"
    _write_nested_pack(pack, "Alien #5.cbz", "Alien #6.cbz")

    with pytest.raises(DirectArtifactPackError) as caught:
        extract_same_series_issue_files(
            pack,
            destination=tmp_path / "extracted",
            expected_issue_numbers=frozenset({"5", "6"}),
            expected_series_titles=frozenset({""}),
        )

    assert caught.value.code == "direct_pack_series_invalid"


def test_unreadable_pack_has_stable_failure(tmp_path: Path) -> None:
    with pytest.raises(DirectArtifactPackError) as caught:
        extract_same_series_issue_files(
            tmp_path / "missing.cbz",
            destination=tmp_path / "extracted",
            expected_issue_numbers=frozenset({"5", "6"}),
            expected_series_titles=frozenset({"Alien"}),
        )

    assert caught.value.code == "direct_pack_unreadable"


def test_pack_rejects_unsafe_nested_comic_path(tmp_path: Path) -> None:
    pack = tmp_path / "Alien #5-6.cbz"
    _write_nested_pack(pack, "../Alien #5.cbz", "Alien #6.cbz")

    with pytest.raises(DirectArtifactPackError) as caught:
        extract_same_series_issue_files(
            pack,
            destination=tmp_path / "extracted",
            expected_issue_numbers=frozenset({"5", "6"}),
            expected_series_titles=frozenset({"Alien"}),
        )

    assert caught.value.code == "direct_pack_unsafe_member"


def test_pack_skips_unparseable_and_unwanted_issue_members(tmp_path: Path) -> None:
    pack = tmp_path / "Alien #5-6.cbz"
    _write_nested_pack(pack, "notes.cbz", "Alien #4.cbz", "Alien #5.cbz")

    with pytest.raises(DirectArtifactPackError) as caught:
        extract_same_series_issue_files(
            pack,
            destination=tmp_path / "extracted",
            expected_issue_numbers=frozenset({"5", "6"}),
            expected_series_titles=frozenset({"Alien"}),
        )

    assert caught.value.code == "direct_pack_incomplete"


def test_pack_rejects_duplicate_files_for_one_issue(tmp_path: Path) -> None:
    pack = tmp_path / "Alien #5-6.cbz"
    _write_nested_pack(pack, "Alien #5.cbz", "nested/Alien #5.cbz", "Alien #6.cbz")

    with pytest.raises(DirectArtifactPackError) as caught:
        extract_same_series_issue_files(
            pack,
            destination=tmp_path / "extracted",
            expected_issue_numbers=frozenset({"5", "6"}),
            expected_series_titles=frozenset({"Alien"}),
        )

    assert caught.value.code == "direct_pack_ambiguous_issue"


def test_pack_rejects_invalid_declared_coverage(tmp_path: Path) -> None:
    pack = tmp_path / "Alien #5-6.cbz"
    _write_nested_pack(pack, "Alien #5.cbz", "Alien #6.cbz")

    with pytest.raises(DirectArtifactPackError) as caught:
        extract_same_series_issue_files(
            pack,
            destination=tmp_path / "extracted",
            expected_issue_numbers=frozenset({"not-a-number", "6"}),
            expected_series_titles=frozenset({"Alien"}),
        )

    assert caught.value.code == "direct_pack_coverage_invalid"


def test_pack_reports_destination_write_failure(tmp_path: Path) -> None:
    pack = tmp_path / "Alien #5-6.cbz"
    _write_nested_pack(pack, "Alien #5.cbz", "Alien #6.cbz")
    destination = tmp_path / "extracted"
    (destination / "issue-5.cbz").mkdir(parents=True)

    with pytest.raises(DirectArtifactPackError) as caught:
        extract_same_series_issue_files(
            pack,
            destination=destination,
            expected_issue_numbers=frozenset({"5", "6"}),
            expected_series_titles=frozenset({"Alien"}),
        )

    assert caught.value.code == "direct_pack_extract_failed"


def test_issue_path_token_falls_back_for_unusual_text() -> None:
    assert _issue_path_token("Special Edition") == "Special Edition"
