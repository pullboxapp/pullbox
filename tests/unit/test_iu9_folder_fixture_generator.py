"""Deterministic folder-import acceptance fixture coverage."""

from __future__ import annotations

import json
import zipfile
from typing import TYPE_CHECKING

import pytest

from pullbox.core.collection_scanner import CollectionScanner
from pullbox.core.source_metadata import MetadataSignal, SourceMetadataExtractor
from scripts.iu9_acceptance_fixtures.folder import generate_folder_fixture

if TYPE_CHECKING:
    from pathlib import Path


def _manifest(root: Path) -> dict[str, object]:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def test_folder_fixture_is_deterministic_and_covers_real_world_layouts(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    generate_folder_fixture(first, seed=1300)
    generate_folder_fixture(second, seed=1300)

    first_manifest = _manifest(first)
    second_manifest = _manifest(second)
    assert first_manifest == second_manifest
    assert first_manifest["fixture_kind"] == "iu9-folder-import"
    assert first_manifest["seed"] == 1300
    cases = {case["id"]: case for case in first_manifest["cases"]}
    assert {
        "issue_only_filename",
        "publisher_series_layout",
        "issue_title_filename",
        "number_zero",
        "number_dot_five",
        "number_leading_dot_five",
        "number_half",
        "number_suffix",
        "number_ten_thousand",
        "number_one_million",
        "unicode_punctuation",
        "nested_generic_containers",
        "loose_mixed_series",
        "uppercase_extension",
        "conflicting_metadata",
        "duplicate_identical",
        "duplicate_different",
        "mislabeled_zip_cbr",
        "corrupt_cbz",
        "empty_cbz",
        "story_arc_reading_order",
        "hidden_noncomic",
        "safe_symlink",
        "broken_symlink",
    } <= cases.keys()
    assert {case["expected_outcome"] for case in cases.values()} >= {
        "success",
        "review",
        "blocked",
    }

    expected_paths = {
        "roots/source-a/Absolute Batman/Issue 01.cbz",
        "roots/source-a/DC Comics/Absolute Batman/Issue 02.cbz",
        ("roots/source-a/Batman (2011)/Batman The Court of Owls, Part One Issue 001.cbz"),
        "roots/source-a/Number Lab (2026)/Issue 0.5.cbz",
        "roots/source-a/Number Lab (2026)/Issue 1000000.cbz",
    }
    tree_paths = {row["path"] for row in first_manifest["tree"]}
    assert expected_paths <= tree_paths


def test_folder_fixture_preserves_number_text_and_omits_metroninfo(tmp_path: Path) -> None:
    root = tmp_path / "folder-fixture"
    generate_folder_fixture(root, seed=44)
    manifest = _manifest(root)
    cases = {case["id"]: case for case in manifest["cases"]}

    assert cases["number_dot_five"]["issue_number"] == "0.5"
    assert cases["number_leading_dot_five"]["issue_number"] == ".5"
    assert cases["number_half"]["issue_number"] == "1/2"
    assert cases["number_one_million"]["issue_number"] == "1000000"
    assert "e+" not in json.dumps(manifest).lower()

    names = {path.name.casefold() for path in root.rglob("*")}
    assert "metroninfo.xml" not in names
    for row in manifest["tree"]:
        members = [member.casefold() for member in row.get("archive_members", [])]
        assert "metroninfo.xml" not in members

    million = root / cases["number_one_million"]["paths"][0]
    with zipfile.ZipFile(million) as archive:
        comic_info = archive.read("ComicInfo.xml").decode()
    assert "<Number>1000000</Number>" in comic_info


def test_folder_fixture_identifies_zip_disguised_as_cbr_without_claiming_rar(
    tmp_path: Path,
) -> None:
    root = tmp_path / "folder-fixture"
    generate_folder_fixture(root, seed=8)
    manifest = _manifest(root)
    cases = {case["id"]: case for case in manifest["cases"]}
    disguised = root / cases["mislabeled_zip_cbr"]["paths"][0]

    assert disguised.suffix == ".cbr"
    assert zipfile.is_zipfile(disguised)
    assert cases["mislabeled_zip_cbr"]["archive_format"] == "zip-mislabeled-cbr"
    assert manifest["cbr_seed_set"]["status"] == "not_provided"


@pytest.mark.asyncio
async def test_folder_fixture_is_readable_through_the_product_scanner(tmp_path: Path) -> None:
    root = tmp_path / "folder-fixture"
    generate_folder_fixture(root, seed=1300)
    manifest = _manifest(root)
    cases = {case["id"]: case for case in manifest["cases"]}
    selected = [
        root / cases["issue_only_filename"]["paths"][0],
        root / cases["number_dot_five"]["paths"][0],
        root / cases["number_one_million"]["paths"][0],
    ]

    discovered = await CollectionScanner().scan_files(
        [str(path) for path in selected],
        root_path=root / "roots" / "source-a",
    )
    files = [file for series in discovered for file in series.files]

    assert {file.issue_number_raw for file in files} == {"1", "0.5", "1000000"}
    assert any(series.raw_series_name == "Absolute Batman" for series in discovered)


def test_generator_refuses_to_replace_an_existing_fixture(tmp_path: Path) -> None:
    root = tmp_path / "folder-fixture"
    generate_folder_fixture(root, seed=1300)
    marker = root / "operator-note.txt"
    marker.write_text("preserve me", encoding="utf-8")

    with pytest.raises(FileExistsError, match="must be empty"):
        generate_folder_fixture(root, seed=1300)

    assert marker.read_text(encoding="utf-8") == "preserve me"


def test_folder_sidecar_and_comicinfo_round_trip_through_product_parser(tmp_path: Path) -> None:
    root = tmp_path / "folder-fixture"
    generate_folder_fixture(root, seed=1300)
    manifest = _manifest(root)
    cases = {case["id"]: case for case in manifest["cases"]}
    archive = root / cases["trusted_comicvine_identity"]["paths"][0]

    metadata = SourceMetadataExtractor().from_archive_path(archive)

    assert metadata.comicvine_series_id == 123456
    assert metadata.comicvine_issue_id == 700100
    assert metadata.signals["comicvine_series_id"] == MetadataSignal.SIDECAR
    assert metadata.signals["comicvine_issue_id"] == MetadataSignal.COMICINFO


@pytest.mark.asyncio
async def test_metadata_poor_layout_cases_use_filename_and_folder_signals(tmp_path: Path) -> None:
    root = tmp_path / "folder-fixture"
    generate_folder_fixture(root, seed=1300)
    manifest = _manifest(root)
    cases = {case["id"]: case for case in manifest["cases"]}
    fallback_ids = {
        "issue_only_filename",
        "publisher_series_layout",
        "issue_title_filename",
    }
    expected_paths = {case_id: str(root / cases[case_id]["paths"][0]) for case_id in fallback_ids}

    results = [
        candidate async for candidate in CollectionScanner().scan(root / "roots" / "source-a")
    ]
    files_by_path = {
        file.file_path: (candidate, file)
        for candidate in results
        for file in candidate.files
        if file.file_path in expected_paths.values()
    }

    assert set(files_by_path) == set(expected_paths.values())
    issue_candidate, issue_file = files_by_path[expected_paths["issue_only_filename"]]
    assert issue_candidate.raw_series_name == "Absolute Batman"
    assert issue_file.issue_number_raw == "1"
    publisher_candidate, publisher_file = files_by_path[expected_paths["publisher_series_layout"]]
    assert publisher_candidate.raw_series_name == "Absolute Batman"
    assert publisher_candidate.raw_publisher == "DC Comics"
    assert publisher_file.issue_number_raw == "2"
    title_candidate, title_file = files_by_path[expected_paths["issue_title_filename"]]
    assert title_candidate.raw_series_name == "Batman"
    assert title_file.issue_number_raw == "1"
    assert all(
        file.has_comicinfo is False
        and file.comicvine_series_id is None
        and file.comicvine_issue_id is None
        for _candidate, file in files_by_path.values()
    )
    assert all(
        "metadata-poor" in cases[case_id]["tags"]
        and cases[case_id]["identity_source"]
        in {"folder-and-filename", "publisher-folder-and-filename", "filename"}
        for case_id in fallback_ids
    )
