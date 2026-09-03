"""Unit tests for Mylar3 database reader."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from pullbox.core.exceptions import MylarReadError
from pullbox.core.library_layout import ImportLayoutMode, SourceLayoutSpec
from pullbox.core.mylar3_reader import Mylar3Reader
from pullbox.models.issue import IssueType
from scripts.mylar3_import_fixture import create_minimal_cbz, create_mylar3_db

if TYPE_CHECKING:
    from pathlib import Path


def _create_mylar_db(
    db_path: Path,
    series: list[dict[str, str | int | None]] | None = None,
    issues: list[dict[str, str | int | None]] | None = None,
    annuals: list[dict[str, str | int | None]] | None = None,
) -> None:
    """Create a fake Mylar3 database with a comic table."""
    create_mylar3_db(
        db_path,
        series=series or [],
        issues=issues,
        annuals=annuals,
    )


@pytest.mark.parametrize("file_name", ["mylar#export.db", "mylar%23export.db"])
async def test_mylar_reader_preserves_literal_uri_characters(
    tmp_path: Path, file_name: str
) -> None:
    source = tmp_path / file_name
    _create_mylar_db(source)
    before = source.read_bytes()
    reader = Mylar3Reader(source)
    snapshot = await reader.read_story_arc_preflight()
    assert snapshot.arcs_count == 0
    assert await reader.read_series() == []
    assert not (await reader.read_import_metadata()).storyarcs_present
    assert [page async for page in reader.iter_import_series_pages()] == []
    assert [page async for page in reader.iter_import_story_arc_pages()] == []
    assert source.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == [file_name]


@pytest.mark.parametrize("actual_name", ["Firefly 007 (2019).cbz", "FIREFLY 007  (2019).cbz"])
async def test_in_place_reconciles_unique_stale_mylar_filename(tmp_path, actual_name):
    db = tmp_path / "mylar.db"
    folder = tmp_path / "comics" / "Firefly (2018)"
    comic = folder / actual_name
    create_minimal_cbz(comic)
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-115251",
                "ComicName": "Firefly",
                "ComicYear": "2018",
                "ComicLocation": "/comics/Firefly (2018)",
                "Total": 1,
            }
        ],
        issues=[
            {
                "IssueID": "711865",
                "ComicID": "115251",
                "ComicName": "Firefly",
                "Issue_Number": "7",
                "Location": "Firefly 007 (2019).cbr",
                "IssueDate": "2019-06-19",
            }
        ],
    )
    original_db = db.read_bytes()
    original_comic = comic.read_bytes()
    results = await Mylar3Reader(
        db,
        path_map={"/comics": str(tmp_path / "comics")},
        include_missing_files=True,
    ).read_series()

    assert [file.file_path for file in results[0].files] == [str(comic)]
    file = results[0].files[0]
    assert file.comicvine_issue_id == 711865
    assert file.source_signature
    assert file.metadata_diagnostics["mylar3_path_reconciliation"]["recorded_path"].endswith(
        "Firefly 007 (2019).cbr"
    )
    assert db.read_bytes() == original_db
    assert comic.read_bytes() == original_comic


@pytest.mark.parametrize(
    "actual_names",
    [
        ["Firefly 007 (2019).cbz", "Firefly 007 (2019).pdf"],
        ["Firefly Annual 007 (2019).cbz"],
        ["Firefly 007 (2020).cbz"],
        ["Firefly 007 Variant (2019).cbz"],
        ["Firefly 00 7 (2019).cbz"],
        ["Other/Firefly 007 (2019).cbz"],
    ],
)
async def test_in_place_does_not_guess_stale_mylar_identity(tmp_path, actual_names):
    db = tmp_path / "mylar.db"
    folder = tmp_path / "comics" / "Firefly"
    folder.mkdir(parents=True)
    for name in actual_names:
        create_minimal_cbz(folder / name)
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-115251",
                "ComicName": "Firefly",
                "ComicYear": "2018",
                "ComicLocation": str(folder),
                "Total": 1,
            }
        ],
        issues=[
            {
                "IssueID": "711865",
                "ComicID": "115251",
                "ComicName": "Firefly",
                "Issue_Number": "7",
                "Location": "Firefly 007 (2019).cbr",
            }
        ],
    )
    results = await Mylar3Reader(db, include_missing_files=True).read_series()
    missing = next((file for file in results[0].files if file.file_name.endswith(".cbr")), None)
    assert missing is not None
    assert missing.source_signature == {}
    assert missing.comicvine_issue_id == 711865
    assert not any(
        "mylar3_path_reconciliation" in file.metadata_diagnostics for file in results[0].files
    )


async def test_stale_path_reconciliation_does_not_merge_conflicting_records(tmp_path):
    db = tmp_path / "mylar.db"
    folder = tmp_path / "comics"
    create_minimal_cbz(folder / "Firefly 007 (2019).cbz")
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-115251",
                "ComicName": "Firefly",
                "ComicLocation": str(folder),
            }
        ],
        issues=[
            {
                "IssueID": str(issue_id),
                "ComicID": "115251",
                "Issue_Number": "7",
                "Location": "Firefly 007 (2019).cbr",
            }
            for issue_id in [711865, 711866]
        ],
    )
    results = await Mylar3Reader(db, include_missing_files=True).read_series()
    actual = next(file for file in results[0].files if file.file_format == "cbz")
    assert actual.comicvine_issue_id is None
    assert "mylar3_path_reconciliation" not in actual.metadata_diagnostics


@pytest.mark.asyncio
async def test_selected_layout_applies_to_mapped_mylar_paths_without_overriding_identity(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mylar.db"
    comics_root = tmp_path / "mounted-comics"
    issue_path = (
        comics_root
        / "DC Comics"
        / "Batman (2011)"
        / "Batman The Court of Owls, Part One Issue 001.cbz"
    )
    create_minimal_cbz(issue_path)
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-42721",
                "ComicName": "Batman",
                "ComicYear": "2011",
                "ComicPublisher": "DC Comics",
                "ComicLocation": "/comics/DC Comics/Batman (2011)",
                "Total": 1,
            }
        ],
        issues=[
            {
                "IssueID": "340001",
                "ComicID": "42721",
                "ComicName": "Batman",
                "IssueName": "The Court of Owls, Part One",
                "Issue_Number": "1",
                "Location": issue_path.name,
                "IssueDate": "2011-09-21",
            }
        ],
    )

    results = await Mylar3Reader(
        db,
        path_map={"/comics": str(comics_root)},
        source_layout=SourceLayoutSpec(
            mode=ImportLayoutMode.PRESET,
            preset="publisher_series",
            fallback_to_auto=False,
        ),
    ).read_series()

    assert len(results) == 1
    series = results[0]
    assert series.mylar3_cv_id == 42721
    assert series.raw_series_name == "Batman"
    assert series.raw_year == 2011
    assert series.raw_publisher == "DC Comics"
    assert len(series.files) == 1
    discovered_file = series.files[0]
    assert discovered_file.comicvine_issue_id == 340001
    assert discovered_file.comicvine_series_id == 42721
    assert discovered_file.parsed_series == "Batman"
    assert discovered_file.parsed_issue_number == 1.0
    assert discovered_file.metadata_signals["series_name"] == "mylar3"
    assert discovered_file.metadata_signals["issue_number"] == "mylar3"
    assert discovered_file.metadata_diagnostics["source_layout"] == {
        "fit": True,
        "fallback_used": False,
        "relative_path": (
            "DC Comics/Batman (2011)/Batman The Court of Owls, Part One Issue 001.cbz"
        ),
    }


@pytest.mark.asyncio
async def test_managed_reader_includes_existing_recorded_paths_beyond_series_folder(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mylar.db"
    comics_root = tmp_path / "mounted-comics"
    direct_issue = comics_root / "Batman" / "issue.cbz"
    nested_issue = comics_root / "Batman" / "Annuals" / "issue.cbz"
    mapped_issue = comics_root / "Shared" / "Batman Special 001.cbz"
    for issue_path in (direct_issue, nested_issue, mapped_issue):
        create_minimal_cbz(issue_path)
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-42721",
                "ComicName": "Batman",
                "ComicYear": "2011",
                "ComicPublisher": "DC Comics",
                "ComicLocation": "/comics/Batman",
                "Total": 4,
            }
        ],
        issues=[
            {
                "IssueID": "340001",
                "ComicID": "42721",
                "Issue_Number": "1",
                "Location": direct_issue.name,
            },
            {
                "IssueID": "340002",
                "ComicID": "42721",
                "Issue_Number": "2",
                "Location": "Annuals/issue.cbz",
            },
            {
                "IssueID": "340003",
                "ComicID": "42721",
                "Issue_Number": "3",
                "Location": "/comics/Shared/Batman Special 001.cbz",
            },
            {
                "IssueID": "340004",
                "ComicID": "42721",
                "Issue_Number": "4",
                "Location": "/comics/Shared/Missing 001.cbz",
            },
        ],
    )

    pages = [
        page
        async for page in Mylar3Reader(
            db,
            path_map={"/comics": str(comics_root)},
        ).iter_import_series_pages(page_size=1)
    ]

    assert len(pages) == 1
    assert {item.file_path: item.comicvine_issue_id for item in pages[0][0].files} == {
        str(direct_issue): 340001,
        str(nested_issue): 340002,
        str(mapped_issue): 340003,
    }
    assert sum(item.file_path == str(direct_issue) for item in pages[0][0].files) == 1


@pytest.mark.asyncio
async def test_managed_reader_uses_absolute_issue_location_without_comic_location(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mylar.db"
    comics_root = tmp_path / "mounted-comics"
    issue_path = comics_root / "Shared" / "Batman Special 001.cbz"
    create_minimal_cbz(issue_path)
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-42721",
                "ComicName": "Batman",
                "ComicYear": "2011",
                "ComicPublisher": "DC Comics",
                "ComicLocation": None,
                "Total": 1,
            }
        ],
        issues=[
            {
                "IssueID": "340001",
                "ComicID": "42721",
                "Issue_Number": "1",
                "Location": "/comics/Shared/Batman Special 001.cbz",
            }
        ],
    )

    pages = [
        page
        async for page in Mylar3Reader(
            db,
            path_map={"/comics": str(comics_root)},
        ).iter_import_series_pages(page_size=1)
    ]

    assert len(pages) == 1
    series = pages[0][0]
    assert [(item.file_path, item.comicvine_issue_id) for item in series.files] == [
        (str(issue_path), 340001)
    ]
    assert series.has_files is True
    assert series.file_count == 1
    assert series.diagnostics["mylar3_path"] == {
        "status": "mapped",
        "mapping_applied": True,
    }
    assert series.diagnostics["mylar3_series_location"]["status"] == "missing"
    assert "kind" not in series.diagnostics


@pytest.mark.asyncio
async def test_missing_absolute_issue_without_comic_location_is_retained_only_in_place(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mylar.db"
    comics_root = tmp_path / "mounted-comics"
    comics_root.mkdir()
    missing_issue = comics_root / "Shared" / "Missing 001.cbz"
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-42721",
                "ComicName": "Batman",
                "ComicLocation": None,
                "Total": 1,
            }
        ],
        issues=[
            {
                "IssueID": "340001",
                "ComicID": "42721",
                "Issue_Number": "1",
                "Location": "/comics/Shared/Missing 001.cbz",
            }
        ],
    )
    root_boundaries = ((comics_root.absolute(), comics_root.resolve()),)

    managed = await Mylar3Reader(
        db,
        path_map={"/comics": str(comics_root)},
    ).read_series()
    in_place = await Mylar3Reader(
        db,
        path_map={"/comics": str(comics_root)},
        include_missing_files=True,
        reference_root_boundaries=root_boundaries,
    ).read_series()

    assert managed[0].files == []
    assert [item.file_path for item in in_place[0].files] == [str(missing_issue)]
    assert in_place[0].files[0].comicvine_issue_id == 340001
    assert in_place[0].files[0].source_signature == {}


@pytest.mark.asyncio
async def test_recorded_missing_issue_paths_are_retained_only_for_in_place_review(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mylar.db"
    comics_root = tmp_path / "mounted-comics"
    (comics_root / "Batman").mkdir(parents=True)
    missing_issue = comics_root / "Batman" / "Missing" / "issue.cbz"
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-42721",
                "ComicName": "Batman",
                "ComicYear": "2011",
                "ComicPublisher": "DC Comics",
                "ComicLocation": "/comics/Batman",
                "Total": 1,
            }
        ],
        issues=[
            {
                "IssueID": "340001",
                "ComicID": "42721",
                "Issue_Number": "1",
                "Location": "Missing/issue.cbz",
            }
        ],
    )

    managed = await Mylar3Reader(
        db,
        path_map={"/comics": str(comics_root)},
    ).read_series()
    in_place = await Mylar3Reader(
        db,
        path_map={"/comics": str(comics_root)},
        include_missing_files=True,
    ).read_series()

    assert managed[0].files == []
    assert [item.file_path for item in in_place[0].files] == [str(missing_issue)]
    assert in_place[0].files[0].source_signature == {}


@pytest.mark.asyncio
async def test_mylar_series_identity_wins_while_sidecar_conflict_is_preserved(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mylar.db"
    series_dir = tmp_path / "comics" / "Batman (2011)"
    issue_path = series_dir / "Batman 001.cbz"
    create_minimal_cbz(issue_path)
    (series_dir / "series.json").write_text(
        '{"comicid": 99999, "booktype": "TPB", "status": "Ended", '
        '"total_issues": 12, "name": "Batman", "year": 2011}'
    )
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-42721",
                "ComicName": "Batman",
                "ComicYear": "2011",
                "ComicPublisher": "DC Comics",
                "ComicLocation": str(series_dir),
                "Total": 1,
            }
        ],
    )

    results = await Mylar3Reader(db).read_series()

    discovered_file = results[0].files[0]
    assert discovered_file.comicvine_series_id == 42721
    assert discovered_file.metadata_signals["comicvine_series_id"] == "mylar3"
    assert discovered_file.metadata_diagnostics["sidecar_files_present"] == ["series.json"]
    assert discovered_file.metadata_diagnostics["archive_metadata_deferred"] is True
    assert discovered_file.metadata_diagnostics["sidecar_snapshot"] == {
        "files_present": ["series.json"],
        "series_id": 99999,
        "series_id_source": "series.json",
        "issue_id": None,
        "booktype": IssueType.TPB.value,
        "series_status": "Ended",
        "issue_count": 12,
        "series_name": "Batman",
        "year": 2011,
        "identity_conflicts": [],
    }
    assert discovered_file.metadata_diagnostics["identity_conflicts"] == [
        {
            "field": "comicvine_series_id",
            "mylar3": 42721,
            "sidecar": 99999,
        }
    ]


@pytest.mark.asyncio
async def test_existing_identity_location_wins_over_explicit_alias_mapping(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mylar.db"
    identity_root = tmp_path / "primary" / "comics"
    identity_series = identity_root / "Batman (2011)"
    identity_issue = identity_series / "Batman 001.cbz"
    alias_root = tmp_path / "alias-comics"
    alias_issue = alias_root / identity_series.name / "Batman 099.cbz"
    create_minimal_cbz(identity_issue)
    create_minimal_cbz(alias_issue)
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-42721",
                "ComicName": "Batman",
                "ComicYear": "2011",
                "ComicPublisher": "DC Comics",
                "ComicLocation": str(identity_series),
                "Total": 1,
            }
        ],
    )

    results = await Mylar3Reader(
        db,
        path_map={str(identity_root): str(alias_root)},
    ).read_series()

    assert len(results) == 1
    series = results[0]
    assert series.source_folder == str(identity_series)
    assert [file.file_path for file in series.files] == [str(identity_issue)]
    assert series.diagnostics["mylar3_path"] == {
        "status": "local",
        "mapping_applied": False,
    }


@pytest.mark.asyncio
async def test_in_place_identity_inside_reference_root_wins_over_mapping(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mylar.db"
    identity_root = tmp_path / "reference-comics"
    identity_series = identity_root / "Batman (2011)"
    identity_issue = identity_series / "Batman 001.cbz"
    alias_root = tmp_path / "alias-comics"
    alias_issue = alias_root / identity_series.name / "Batman 099.cbz"
    create_minimal_cbz(identity_issue)
    create_minimal_cbz(alias_issue)
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-42721",
                "ComicName": "Batman",
                "ComicLocation": str(identity_series),
            }
        ],
    )

    results = await Mylar3Reader(
        db,
        path_map={str(identity_root): str(alias_root)},
        include_missing_files=True,
        reference_root_boundaries=((identity_root.absolute(), identity_root.resolve()),),
    ).read_series()

    assert results[0].source_folder == str(identity_series)
    assert [file.file_path for file in results[0].files] == [str(identity_issue)]
    assert results[0].diagnostics["mylar3_path"] == {
        "status": "local",
        "mapping_applied": False,
    }


@pytest.mark.asyncio
async def test_in_place_external_identity_uses_confirmed_reference_root_mapping(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mylar.db"
    external_root = tmp_path / "external-comics"
    external_series = external_root / "Batman (2011)"
    external_issue = external_series / "Batman 001.cbz"
    reference_root = tmp_path / "reference-comics"
    mapped_series = reference_root / external_series.name
    mapped_issue = mapped_series / external_issue.name
    create_minimal_cbz(external_issue)
    create_minimal_cbz(mapped_issue)
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-42721",
                "ComicName": "Batman",
                "ComicLocation": str(external_series),
            }
        ],
        issues=[
            {
                "IssueID": "340001",
                "ComicID": "42721",
                "Issue_Number": "1",
                "Location": str(external_issue),
            }
        ],
    )

    results = await Mylar3Reader(
        db,
        path_map={str(external_root): str(reference_root)},
        include_missing_files=True,
        reference_root_boundaries=((reference_root.absolute(), reference_root.resolve()),),
    ).read_series()

    series = results[0]
    assert series.source_folder == str(mapped_series)
    assert [(file.file_path, file.comicvine_issue_id) for file in series.files] == [
        (str(mapped_issue), 340001)
    ]
    assert series.diagnostics["mylar3_path"] == {
        "status": "mapped",
        "mapping_applied": True,
    }


@pytest.mark.asyncio
async def test_path_mapping_rejects_parent_traversal_outside_mapped_root(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mylar.db"
    mapped_root = tmp_path / "mapped-comics"
    escaped_issue = tmp_path / "escaped" / "Batman 001.cbz"
    create_minimal_cbz(escaped_issue)
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-42721",
                "ComicName": "Batman",
                "ComicYear": "2011",
                "ComicPublisher": "DC Comics",
                "ComicLocation": "/comics/../escaped",
                "Total": 1,
            }
        ],
    )

    results = await Mylar3Reader(
        db,
        path_map={"/comics": str(mapped_root)},
    ).read_series()

    assert len(results) == 1
    series = results[0]
    assert series.files == []
    assert series.has_files is False
    assert series.source_folder == ""
    assert series.diagnostics["kind"] == "mylar3_path_incompatible"
    assert series.diagnostics["reason"] == "unsafe_path_mapping"
    assert series.diagnostics["mylar3_path"] == {
        "status": "invalid",
        "mapping_applied": True,
    }
    assert "outside the configured mapped root" in str(series.diagnostics["rejection_reason"])


@pytest.mark.asyncio
async def test_multiple_path_maps_keep_roots_identities_and_failures_isolated(
    tmp_path: Path,
) -> None:
    db = tmp_path / "mylar.db"
    primary_root = tmp_path / "primary-host"
    secondary_root = tmp_path / "secondary-host"
    primary_series = primary_root / "DC Comics" / "Batman (2011)"
    secondary_series = secondary_root / "Image Comics" / "Saga (2012)"
    primary_issue = primary_series / "Batman 001.cbz"
    secondary_issue = secondary_series / "Saga 001.cbz"
    escaped_issue = tmp_path / "escaped" / "Should Not Import 001.cbz"
    for issue_path in (primary_issue, secondary_issue, escaped_issue):
        create_minimal_cbz(issue_path)

    source_snapshot = {
        issue_path: (
            issue_path.read_bytes(),
            issue_path.stat().st_mtime_ns,
            issue_path.stat().st_mode,
        )
        for issue_path in (primary_issue, secondary_issue, escaped_issue)
    }
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-42721",
                "ComicName": "Batman",
                "ComicYear": "2011",
                "ComicPublisher": "DC Comics",
                "ComicLocation": "/primary/DC Comics/Batman (2011)",
                "Total": 1,
            },
            {
                "ComicID": "CV-67824",
                "ComicName": "Saga",
                "ComicYear": "2012",
                "ComicPublisher": "Image Comics",
                "ComicLocation": "/secondary/Image Comics/Saga (2012)",
                "Total": 1,
            },
            {
                "ComicID": "CV-90001",
                "ComicName": "Escaped",
                "ComicYear": "2020",
                "ComicPublisher": "Unsafe Comics",
                "ComicLocation": "/primary/../escaped",
                "Total": 1,
            },
            {
                "ComicID": "CV-90002",
                "ComicName": "Unmapped",
                "ComicYear": "2021",
                "ComicPublisher": "Unknown Comics",
                "ComicLocation": "/unmapped/Unknown Comics/Unmapped (2021)",
                "Total": 1,
            },
        ],
        issues=[
            {
                "IssueID": "340001",
                "ComicID": "42721",
                "ComicName": "Batman",
                "IssueName": "The Court of Owls",
                "Issue_Number": "1",
                "Location": primary_issue.name,
                "IssueDate": "2011-09-21",
            },
            {
                "IssueID": "500001",
                "ComicID": "67824",
                "ComicName": "Saga",
                "IssueName": "Chapter One",
                "Issue_Number": "1",
                "Location": secondary_issue.name,
                "IssueDate": "2012-03-14",
            },
        ],
    )

    results = await Mylar3Reader(
        db,
        path_map={
            "/primary": str(primary_root),
            "/secondary": str(secondary_root),
        },
    ).read_series()

    by_cv_id = {series.mylar3_cv_id: series for series in results}
    assert set(by_cv_id) == {42721, 67824, 90001, 90002}

    batman = by_cv_id[42721]
    assert batman.source_folder == str(primary_series.resolve())
    assert batman.source_folder_relative == "/primary/DC Comics/Batman (2011)"
    assert batman.diagnostics["mylar3_path"] == {
        "status": "mapped",
        "mapping_applied": True,
    }
    assert len(batman.files) == 1
    assert batman.files[0].comicvine_series_id == 42721
    assert batman.files[0].comicvine_issue_id == 340001
    assert batman.files[0].metadata_signals["comicvine_series_id"] == "mylar3"
    assert batman.files[0].metadata_signals["comicvine_issue_id"] == "mylar3"

    saga = by_cv_id[67824]
    assert saga.source_folder == str(secondary_series.resolve())
    assert saga.source_folder_relative == "/secondary/Image Comics/Saga (2012)"
    assert saga.diagnostics["mylar3_path"] == {
        "status": "mapped",
        "mapping_applied": True,
    }
    assert len(saga.files) == 1
    assert saga.files[0].comicvine_series_id == 67824
    assert saga.files[0].comicvine_issue_id == 500001
    assert saga.files[0].metadata_signals["comicvine_series_id"] == "mylar3"
    assert saga.files[0].metadata_signals["comicvine_issue_id"] == "mylar3"

    escaped = by_cv_id[90001]
    assert escaped.files == []
    assert escaped.source_folder == ""
    assert escaped.diagnostics["reason"] == "unsafe_path_mapping"
    assert escaped.diagnostics["mylar3_path"] == {
        "status": "invalid",
        "mapping_applied": True,
    }

    unmapped = by_cv_id[90002]
    assert unmapped.files == []
    assert unmapped.source_folder == ""
    assert unmapped.diagnostics["reason"] == "unmapped_path"
    assert unmapped.diagnostics["mylar3_path"] == {
        "status": "unmapped",
        "mapping_applied": False,
    }

    assert {
        issue_path: (
            issue_path.read_bytes(),
            issue_path.stat().st_mtime_ns,
            issue_path.stat().st_mode,
        )
        for issue_path in (primary_issue, secondary_issue, escaped_issue)
    } == source_snapshot


@pytest.mark.asyncio
async def test_annual_row_preserves_release_identity_and_issue_type(tmp_path: Path) -> None:
    db = tmp_path / "mylar.db"
    series_dir = tmp_path / "comics" / "X-Men"
    regular_path = series_dir / "X-Men 001 (2021).cbz"
    annual_path = series_dir / "X-Men Annual 001 (2023).cbz"
    create_minimal_cbz(regular_path)
    create_minimal_cbz(annual_path)
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-140553",
                "ComicName": "X-Men",
                "ComicYear": "2021",
                "ComicPublisher": "Marvel",
                "ComicLocation": str(series_dir),
                "Total": 30,
            }
        ],
        issues=[
            {
                "IssueID": "900001",
                "ComicID": "140553",
                "ComicName": "X-Men",
                "IssueName": "Fearless",
                "Issue_Number": "1",
                "Location": regular_path.name,
                "IssueDate": "2021-09-01",
            }
        ],
        annuals=[
            {
                "IssueID": "950001",
                "ComicID": "140553",
                "ComicName": "X-Men",
                "IssueName": "Contest of Chaos",
                "Issue_Number": "1",
                "Location": annual_path.name,
                "IssueDate": "2023-08-16",
                "ReleaseComicID": "CV-153726",
                "ReleaseComicName": "X-Men Annual",
            }
        ],
    )

    results = await Mylar3Reader(db).read_series()

    assert len(results) == 2
    by_cv_id = {item.mylar3_cv_id: item for item in results}

    regular_series = by_cv_id[140553]
    assert regular_series.raw_series_name == "X-Men"
    assert [item.file_name for item in regular_series.files] == [regular_path.name]
    assert regular_series.files[0].issue_type == IssueType.ISSUE
    assert regular_series.files[0].comicvine_series_id == 140553

    annual_series = by_cv_id[153726]
    assert annual_series.raw_series_name == "X-Men Annual"
    assert annual_series.raw_year == 2023
    assert annual_series.raw_publisher == "Marvel"
    assert annual_series.diagnostics["source_issue_type"] == IssueType.ANNUAL.value
    assert [item.file_name for item in annual_series.files] == [annual_path.name]
    assert annual_series.files[0].issue_type == IssueType.ANNUAL
    assert annual_series.files[0].comicvine_series_id == 153726


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_name", "expected_type"),
    [
        ("Fixture Special 001 (2024).cbz", IssueType.SPECIAL),
        ("Fixture One-Shot 001 (2024).cbz", IssueType.ONE_SHOT),
        ("Fixture TPB 001 (2024).cbz", IssueType.TPB),
        ("Fixture Omnibus 001 (2024).cbz", IssueType.OMNIBUS),
        ("Fixture Graphic Novel 001 (2024).cbz", IssueType.GN),
    ],
)
async def test_issue_row_preserves_non_standard_filename_type(
    tmp_path: Path,
    file_name: str,
    expected_type: IssueType,
) -> None:
    db = tmp_path / "mylar.db"
    series_dir = tmp_path / "comics" / "Fixture"
    issue_path = series_dir / file_name
    create_minimal_cbz(issue_path)
    _create_mylar_db(
        db,
        [
            {
                "ComicID": "CV-200001",
                "ComicName": "Fixture",
                "ComicYear": "2024",
                "ComicLocation": str(series_dir),
                "Total": 1,
            }
        ],
        issues=[
            {
                "IssueID": "990001",
                "ComicID": "200001",
                "ComicName": "Fixture",
                "IssueName": "Fixture",
                "Issue_Number": "1",
                "Location": issue_path.name,
                "IssueDate": "2024-01-01",
            }
        ],
    )

    results = await Mylar3Reader(db).read_series()

    assert len(results) == 1
    assert results[0].files[0].issue_type == expected_type


class TestBasicRead:
    """Test 1: Basic read — 3 series, all fields populated."""

    @pytest.mark.asyncio
    async def test_reads_all_series(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {
                    "ComicID": "CV-47050",
                    "ComicName": "Batman",
                    "ComicYear": "2016",
                    "ComicPublisher": "DC Comics",
                    "ComicLocation": str(tmp_path / "comics" / "Batman"),
                    "Status": "Active",
                    "Total": 85,
                },
                {
                    "ComicID": "CV-49030",
                    "ComicName": "Saga",
                    "ComicYear": "2012",
                    "ComicPublisher": "Image Comics",
                    "ComicLocation": str(tmp_path / "comics" / "Saga"),
                    "Status": "Active",
                    "Total": 54,
                },
                {
                    "ComicID": "CV-40796",
                    "ComicName": "Invincible",
                    "ComicYear": "2003",
                    "ComicPublisher": "Image Comics",
                    "ComicLocation": str(tmp_path / "comics" / "Invincible"),
                    "Status": "Ended",
                    "Total": 144,
                },
            ],
        )
        # Create comic dirs with files
        for name in ["Batman", "Saga", "Invincible"]:
            d = tmp_path / "comics" / name
            d.mkdir(parents=True)
            (d / "issue001.cbz").touch()

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 3
        by_name = {r.raw_series_name: r for r in results}
        assert by_name["Batman"].mylar3_cv_id == 47050
        assert by_name["Batman"].raw_year == 2016
        assert by_name["Batman"].raw_publisher == "DC Comics"
        assert by_name["Saga"].mylar3_cv_id == 49030
        assert by_name["Invincible"].mylar3_cv_id == 40796


class TestNullYear:
    """Test 2: NULL year handled gracefully."""

    @pytest.mark.asyncio
    async def test_null_year(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "CV-100", "ComicName": "NoYear", "ComicYear": None},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].raw_year is None

    @pytest.mark.asyncio
    async def test_zero_year(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "CV-100", "ComicName": "ZeroYear", "ComicYear": "0000"},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].raw_year is None


class TestComicVineId:
    """Test 3: ComicVine ID preserved from Mylar3 ComicID field."""

    @pytest.mark.asyncio
    async def test_cv_id_extraction(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "CV-47050", "ComicName": "Batman"},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert results[0].mylar3_cv_id == 47050

    @pytest.mark.asyncio
    async def test_issue_metadata_is_preserved_from_mylar3_issues(
        self,
        tmp_path: Path,
    ) -> None:
        db = tmp_path / "mylar.db"
        series_dir = tmp_path / "comics" / "Batman"
        series_dir.mkdir(parents=True)
        issue_path = series_dir / "Batman 001 (2016).cbz"
        issue_path.touch()
        _create_mylar_db(
            db,
            [
                {
                    "ComicID": "CV-47050",
                    "ComicName": "Batman",
                    "ComicYear": "2016",
                    "ComicPublisher": "DC Comics",
                    "ComicLocation": str(series_dir),
                    "Status": "Ended",
                    "Total": 85,
                },
            ],
            issues=[
                {
                    "IssueID": "500001",
                    "ComicID": "47050",
                    "ComicName": "Batman",
                    "IssueName": "I Am Gotham",
                    "Issue_Number": "1",
                    "Location": issue_path.name,
                    "IssueDate": "2016-08-01",
                },
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        series = results[0]
        assert series.diagnostics["issue_count_hint"] == 85
        assert series.diagnostics["series_status"] == "Ended"
        assert series.files[0].comicvine_issue_id == 500001
        assert series.files[0].comicvine_series_id == 47050
        assert series.files[0].issue_count_hint == 85
        assert series.files[0].series_status == "Ended"
        assert series.files[0].metadata_signals["comicvine_issue_id"] == "mylar3"
        assert series.files[0].metadata_signals["comicvine_series_id"] == "mylar3"
        assert series.files[0].metadata_diagnostics["mylar3_issue"] == {
            "issue_id": 500001,
            "issue_number": "1",
            "title": "I Am Gotham",
            "release_date": "2016-08-01",
        }


class TestStatusHandling:
    """Test 4: Import ALL series regardless of Mylar3 status."""

    @pytest.mark.asyncio
    async def test_all_statuses_imported(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        statuses = ["Active", "Ended", "Paused", "Wanted"]
        series = [
            {"ComicID": f"CV-{i}", "ComicName": f"Series{i}", "Status": s}
            for i, s in enumerate(statuses)
        ]
        _create_mylar_db(db, series)

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 4

    @pytest.mark.asyncio
    async def test_wanted_has_no_files(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {
                    "ComicID": "CV-1",
                    "ComicName": "Wanted",
                    "Status": "Wanted",
                    "ComicLocation": None,
                },
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert results[0].has_files is False


class TestMissingFiles:
    """Test 5: Series with no comic files on disk."""

    @pytest.mark.asyncio
    async def test_nonexistent_location(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {
                    "ComicID": "CV-1",
                    "ComicName": "Gone",
                    "ComicLocation": str(tmp_path / "nonexistent"),
                },
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].file_count == 0
        assert results[0].has_files is False


class TestInvalidDatabase:
    """Test 6: Invalid database file."""

    @pytest.mark.asyncio
    async def test_not_sqlite(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "mylar.db"
        bad_file.write_text("not a database")

        reader = Mylar3Reader(bad_file)
        with pytest.raises(MylarReadError, match="read"):
            await reader.read_series()


class TestWrongDatabase:
    """Test 7: Valid SQLite but no comics table."""

    @pytest.mark.asyncio
    async def test_no_comic_table(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE other_table (id INTEGER)")
        conn.close()

        reader = Mylar3Reader(db)
        with pytest.raises(MylarReadError, match="Not a Mylar3 database"):
            await reader.read_series()


class TestLargeDatabase:
    """Test 8: Large database performance."""

    @pytest.mark.asyncio
    async def test_500_series(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        series = [
            {"ComicID": f"CV-{i}", "ComicName": f"Series {i}", "ComicYear": "2020"}
            for i in range(500)
        ]
        _create_mylar_db(db, series)

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 500


class TestMissingPath:
    """Test 9: Path does not exist."""

    @pytest.mark.asyncio
    async def test_file_not_found(self, tmp_path: Path) -> None:
        reader = Mylar3Reader(tmp_path / "nonexistent.db")
        with pytest.raises(FileNotFoundError):
            await reader.read_series()


class TestDuplicateCvId:
    """Test 10: Duplicate comicvine_id deduplicated."""

    @pytest.mark.asyncio
    async def test_dedup_by_cv_id(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "CV-47050", "ComicName": "Batman"},
                {"ComicID": "CV-47050", "ComicName": "Batman (Duplicate)"},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].raw_series_name == "Batman"


class TestPathMapTranslation:
    """Test 11: Path map translates container paths to host paths."""

    @pytest.mark.asyncio
    async def test_path_map(self, tmp_path: Path) -> None:
        storage = tmp_path / "storage" / "comics" / "Batman (2016)"
        storage.mkdir(parents=True)
        for i in range(3):
            (storage / f"issue{i:03d}.cbz").touch()

        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {
                    "ComicID": "CV-1",
                    "ComicName": "Batman",
                    "ComicYear": "2016",
                    "ComicLocation": "/data/comics/Batman (2016)",
                },
            ],
        )

        reader = Mylar3Reader(db, path_map={"/data": str(tmp_path / "storage")})
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].file_count == 3


class TestPathMapNoMatch:
    """Test 12: Path map doesn't match — file count 0, handled gracefully."""

    @pytest.mark.asyncio
    async def test_unmatched_path_map(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "CV-1", "ComicName": "Batman", "ComicLocation": "/other/path/Batman"},
            ],
        )

        reader = Mylar3Reader(db, path_map={"/data": str(tmp_path / "storage")})
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].file_count == 0
        assert results[0].has_files is False


class TestMalformedCvId:
    """Edge cases for ComicVine ID parsing."""

    @pytest.mark.asyncio
    async def test_malformed_cv_prefix(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "CV-abc", "ComicName": "Broken"},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].mylar3_cv_id is None

    @pytest.mark.asyncio
    async def test_non_cv_non_integer_id(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "xyz", "ComicName": "Weird"},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].mylar3_cv_id is None

    @pytest.mark.asyncio
    async def test_plain_integer_id(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "47050", "ComicName": "PlainId"},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].mylar3_cv_id == 47050


class TestMalformedYear:
    """Edge case: non-numeric year string."""

    @pytest.mark.asyncio
    async def test_non_numeric_year(self, tmp_path: Path) -> None:
        db = tmp_path / "mylar.db"
        _create_mylar_db(
            db,
            [
                {"ComicID": "CV-1", "ComicName": "BadYear", "ComicYear": "abcd"},
            ],
        )

        reader = Mylar3Reader(db)
        results = await reader.read_series()

        assert len(results) == 1
        assert results[0].raw_year is None
