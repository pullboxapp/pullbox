"""Mylar in-place imports validate comic paths, not the source database root."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from pullbox.core.collection_scanner import DiscoveredFile, DiscoveredSeries
from pullbox.core.exceptions import ValidationError
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.core.mylar3_reader import Mylar3Reader
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.library import LibraryRoot
from pullbox.schemas.import_job import ImportJobCreate
from pullbox.services.import_job_creation import create_job
from pullbox.services.import_referenced_sources import (
    MYLAR_REFERENCE_ROOT_ID_SIGNATURE_KEY,
    load_mylar_reference_root_boundaries,
    validate_mylar_in_place_files,
)
from pullbox.services.import_scan_helpers import validate_discovered_files_safety
from pullbox.services.import_scan_materialization import materialize_discovered_scan_results
from pullbox.services.import_scan_pipeline import _load_mylar3_discovered_series
from scripts.mylar3_import_fixture import create_minimal_cbz, create_mylar3_db

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


async def _log_event(*_args: object, **_kwargs: object) -> None:
    pass


def _file_snapshot(path: Path) -> tuple[bytes, int, int]:
    contents = path.read_bytes()
    stat_result = path.stat()
    return contents, stat_result.st_mtime_ns, stat_result.st_mode


def _mylar_db(db_path: Path, locations: list[str]) -> None:
    create_mylar3_db(
        db_path,
        series=[
            {
                "ComicID": "CV-97508",
                "ComicName": "Batman",
                "ComicYear": "2016",
                "ComicPublisher": "DC Comics",
                "ComicLocation": "/comics/Batman",
                "Total": len(locations),
            }
        ],
        issues=[
            {
                "IssueID": str(100001 + index),
                "ComicID": "97508",
                "Issue_Number": str(index + 1),
                "Location": location,
            }
            for index, location in enumerate(locations)
        ],
    )


def _discovered_series_for_file(*, title: str, comic: Path, ordinal: int) -> DiscoveredSeries:
    signature = build_file_identity_signature(comic)
    discovered_file = DiscoveredFile(
        file_path=str(comic),
        file_name=comic.name,
        file_size=int(signature["size"]),
        file_format="cbz",
        parsed_series=title,
        parsed_issue_number=float(ordinal),
        parsed_year=2024,
        parsed_publisher=None,
        has_comicinfo=False,
        comicvine_issue_id=100000 + ordinal,
        issue_number_raw=str(ordinal),
        source_signature=signature,
    )
    return DiscoveredSeries(
        raw_series_name=title,
        raw_year=2024,
        raw_publisher=None,
        file_count=1,
        sample_paths=[str(comic)],
        source_folder=str(comic.parent),
        source_folder_relative=title,
        files=[discovered_file],
        mylar3_cv_id=200000 + ordinal,
        diagnostics={
            "mylar3_path": {
                "status": "local",
                "mapping_applied": False,
            }
        },
    )


async def test_mylar_in_place_creation_accepts_database_outside_reference_root_without_destination(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    comics = tmp_path / "comics"
    comics.mkdir()
    root = LibraryRoot(
        name="Existing library",
        path=str(comics),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=False,
    )
    db_session.add(root)
    await db_session.flush()
    database = tmp_path / "mylar.db"
    _mylar_db(database, [])
    before = _file_snapshot(database)

    job = await create_job(
        db_session,
        ImportJobCreate(
            source_path=str(database),
            source_type=ImportSourceType.MYLAR3,
            file_handling_mode=ImportFileHandlingMode.IN_PLACE,
            mylar3_path_map={"/comics": str(comics)},
            mylar3_path_map_confirmed=True,
        ),
        log_event=_log_event,
    )

    assert job.source_path == str(database)
    assert job.target_library_root_id is None
    assert job.effective_transfer_method == "leave_in_place"
    assert job.move_to_library is False
    assert job.convert_to_preferred_format is False
    assert job.update_embedded_comicinfo_from_match is False
    assert _file_snapshot(database) == before


async def test_mylar_in_place_scan_freezes_each_files_unique_containing_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    preferred_path = tmp_path / "preferred"
    first_root_path = tmp_path / "current"
    second_root_path = tmp_path / "archive"
    preferred_path.mkdir()
    first_comic = first_root_path / "Batman" / "Batman 001.cbz"
    second_comic = second_root_path / "Saga" / "Saga 001.cbz"
    create_minimal_cbz(first_comic)
    create_minimal_cbz(second_comic)
    before = _file_snapshot(first_comic), _file_snapshot(second_comic)
    preferred_root = LibraryRoot(
        name="Preferred managed destination",
        path=str(preferred_path),
        enabled=True,
        allow_referenced_registrations=False,
        allow_managed_writes=True,
    )
    first_root = LibraryRoot(name="Current", path=str(first_root_path), enabled=True)
    second_root = LibraryRoot(name="Archive", path=str(second_root_path), enabled=True)
    db_session.add_all([preferred_root, first_root, second_root])
    await db_session.flush()
    database = tmp_path / "mylar.db"
    database.touch()
    job = ImportJob(
        source_path=str(database),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.SCANNING,
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
        target_library_root_id=preferred_root.id,
        mylar3_path_map_confirmed=True,
    )
    db_session.add(job)
    await db_session.flush()
    discovered = [
        _discovered_series_for_file(title="Batman", comic=first_comic, ordinal=1),
        _discovered_series_for_file(title="Saga", comic=second_comic, ordinal=2),
    ]
    captured_reference_boundaries: list[tuple[tuple[Path, Path], ...]] = []

    class ReaderDouble:
        def __init__(self, **kwargs: object) -> None:
            captured_reference_boundaries.append(
                tuple(kwargs["reference_root_boundaries"])  # type: ignore[arg-type]
            )

        async def read_series(self) -> list[DiscoveredSeries]:
            return discovered

    await _load_mylar3_discovered_series(
        db_session,
        job,
        job_id=job.id,
        mylar3_reader_cls=ReaderDouble,
        auto_detect_mylar3_path_map=lambda _path: None,
        log_event=_log_event,
        validate_discovered_files_safety=validate_discovered_files_safety,
        materialize_discovered_scan_results=materialize_discovered_scan_results,
    )

    rows = list((await db_session.scalars(select(ImportedFile).order_by(ImportedFile.id))).all())
    assert [row.status for row in rows] == [
        ImportedFileStatus.PENDING,
        ImportedFileStatus.PENDING,
    ]
    assert [row.source_signature["mylar_reference_root_id"] for row in rows] == [
        first_root.id,
        second_root.id,
    ]
    assert captured_reference_boundaries == [
        (
            (first_root_path.absolute(), first_root_path.resolve()),
            (second_root_path.absolute(), second_root_path.resolve()),
        )
    ]
    assert job.target_library_root_id == preferred_root.id
    assert (_file_snapshot(first_comic), _file_snapshot(second_comic)) == before


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("disabled", "source_outside_root"),
        ("references_disabled", "source_outside_root"),
        ("alias", "source_root_ambiguous"),
        ("nested_alias", "source_root_ambiguous"),
        ("nested", "source_root_ambiguous"),
        ("symlink_escape", "source_outside_root"),
    ],
)
async def test_mylar_in_place_root_selection_fails_closed_for_unsafe_boundaries(
    db_session: AsyncSession,
    tmp_path: Path,
    failure: str,
    expected_code: str,
) -> None:
    root_path = tmp_path / "library"
    comic = root_path / "Batman" / "Batman 001.cbz"
    create_minimal_cbz(comic)
    source = comic
    root = LibraryRoot(
        name="Library",
        path=str(root_path),
        enabled=failure != "disabled",
        allow_referenced_registrations=failure != "references_disabled",
    )
    roots = [root]
    if failure == "alias":
        alias_path = tmp_path / "library-alias"
        alias_path.symlink_to(root_path, target_is_directory=True)
        roots.append(LibraryRoot(name="Alias", path=str(alias_path), enabled=True))
    elif failure == "nested_alias":
        alias_path = tmp_path / "batman-alias"
        alias_path.symlink_to(comic.parent, target_is_directory=True)
        roots.append(LibraryRoot(name="Nested alias", path=str(alias_path), enabled=True))
    elif failure == "nested":
        roots.append(
            LibraryRoot(
                name="Nested",
                path=str(comic.parent),
                enabled=True,
            )
        )
    elif failure == "symlink_escape":
        outside = tmp_path / "outside" / "Batman 002.cbz"
        create_minimal_cbz(outside)
        source = comic.parent / "Batman 002.cbz"
        source.symlink_to(outside)
    db_session.add_all(roots)
    await db_session.flush()
    before = _file_snapshot(comic)
    discovered = [_discovered_series_for_file(title="Batman", comic=source, ordinal=1)]

    boundaries = await load_mylar_reference_root_boundaries(db_session)
    validate_mylar_in_place_files(discovered, boundaries)

    safety = discovered[0].files[0].metadata_diagnostics["file_safety"]
    assert safety["code"] == expected_code
    assert safety["overrideable"] is False
    assert MYLAR_REFERENCE_ROOT_ID_SIGNATURE_KEY not in discovered[0].files[0].source_signature
    assert _file_snapshot(comic) == before


async def test_mylar_in_place_rejects_invalid_explicit_future_destination(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    database = tmp_path / "mylar.db"
    _mylar_db(database, [])
    source_path = tmp_path / "existing-library"
    source_path.mkdir()
    source_root = LibraryRoot(
        name="Existing library",
        path=str(source_path),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=False,
    )
    invalid_destination_path = tmp_path / "reference-only"
    invalid_destination_path.mkdir()
    invalid_destination = LibraryRoot(
        name="Reference only",
        path=str(invalid_destination_path),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=False,
    )
    db_session.add_all([source_root, invalid_destination])
    await db_session.flush()

    with pytest.raises(ValidationError, match=r"[Ll]ibrary (?:root|destination)"):
        await create_job(
            db_session,
            ImportJobCreate(
                source_path=str(database),
                source_type=ImportSourceType.MYLAR3,
                target_library_root_id=invalid_destination.id,
                file_handling_mode=ImportFileHandlingMode.IN_PLACE,
                mylar3_path_map={"/comics": str(source_path)},
                mylar3_path_map_confirmed=True,
            ),
            log_event=_log_event,
        )


async def test_mylar_reader_records_scan_signature_for_mapped_comic(tmp_path: Path) -> None:
    database = tmp_path / "mylar.db"
    comic = tmp_path / "library" / "Batman" / "Issue 001.cbz"
    create_minimal_cbz(comic)
    _mylar_db(database, [comic.name])

    discovered = await Mylar3Reader(
        database, path_map={"/comics": str(comic.parent.parent)}
    ).read_series()

    assert discovered[0].files[0].source_signature == build_file_identity_signature(comic)


async def test_mylar_in_place_recorded_paths_do_not_collapse_same_named_issues(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mylar.db"
    library = tmp_path / "library"
    first = library / "Batman" / "issue.cbz"
    second = library / "Batman" / "Annuals" / "issue.cbz"
    create_minimal_cbz(first)
    create_minimal_cbz(second)
    (first.parent / "series.json").write_text('{"comicid": 97508}', encoding="utf-8")
    (second.parent / "series.json").write_text('{"comicid": 99999}', encoding="utf-8")
    _mylar_db(database, ["issue.cbz", "/comics/Batman/Annuals/issue.cbz"])

    results = await Mylar3Reader(
        database,
        path_map={"/comics": str(library)},
        include_missing_files=True,
    ).read_series()

    assert {file.file_path: file.comicvine_issue_id for file in results[0].files} == {
        str(first): 100001,
        str(second): 100002,
    }
    by_path = {file.file_path: file for file in results[0].files}
    assert "identity_conflicts" not in by_path[str(first)].metadata_diagnostics
    assert by_path[str(second)].metadata_diagnostics["identity_conflicts"] == [
        {"field": "comicvine_series_id", "mylar3": 97508, "sidecar": 99999}
    ]


async def test_mylar_in_place_paging_retains_missing_cross_series_annual(tmp_path: Path) -> None:
    database = tmp_path / "mylar.db"
    root = tmp_path / "library"
    regular = root / "Batman" / "Issue 001.cbz"
    create_minimal_cbz(regular)
    missing = regular.parent / "Annual 001.cbz"
    create_mylar3_db(
        database,
        series=[
            {
                "ComicID": "CV-97508",
                "ComicName": "Batman",
                "ComicYear": "2016",
                "ComicLocation": None,
            },
            {
                "ComicID": "CV-99999",
                "ComicName": "Batman Annual",
                "ComicYear": "2016",
                "ComicLocation": "/comics/Batman",
            },
        ],
        issues=[
            {
                "IssueID": "100001",
                "ComicID": "97508",
                "Issue_Number": "1",
                "Location": regular.name,
            }
        ],
        annuals=[
            {
                "IssueID": "100002",
                "ComicID": "97508",
                "Issue_Number": "1",
                "Location": "/comics/Batman/Annual 001.cbz",
                "ReleaseComicID": "CV-99999",
                "ReleaseComicName": "Batman Annual",
            }
        ],
    )
    reader = Mylar3Reader(database, path_map={"/comics": str(root)}, include_missing_files=True)

    series = [item async for page in reader.iter_import_series_pages(page_size=1) for item in page]

    annual = next(item for item in series if item.mylar3_cv_id == 99999)
    missing_file = next((file for file in annual.files if file.file_path == str(missing)), None)
    assert missing_file is not None
    assert missing_file.comicvine_issue_id == 100002
    assert missing_file.source_signature == {}


async def test_mylar_in_place_exact_paths_do_not_casefold_distinct_issue_identities(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mylar.db"
    root = tmp_path / "library"
    (root / "Batman").mkdir(parents=True)
    # Recorded missing paths make the identity test portable even on a
    # case-insensitive host; Linux may have two real files at these paths.
    _mylar_db(database, ["Annuals/issue.cbz", "annuals/issue.cbz"])
    results = await Mylar3Reader(
        database, path_map={"/comics": str(root)}, include_missing_files=True
    ).read_series()

    assert {file.file_path: file.comicvine_issue_id for file in results[0].files} == {
        str(root / "Batman" / "Annuals" / "issue.cbz"): 100001,
        str(root / "Batman" / "annuals" / "issue.cbz"): 100002,
    }


@pytest.mark.parametrize("failure", ["missing", "outside", "file_symlink", "folder_symlink"])
async def test_mylar_in_place_scan_retains_ineligible_files_for_nonoverrideable_review(
    db_session: AsyncSession, tmp_path: Path, failure: str
) -> None:
    database = tmp_path / "mylar.db"
    root_path = tmp_path / "comics"
    root_path.mkdir()
    root = LibraryRoot(name="Existing comics", path=str(root_path), enabled=True)
    db_session.add(root)
    await db_session.flush()
    mapped_root = root_path
    comic = root_path / "Batman" / "Issue 001.cbz"
    create_minimal_cbz(comic)
    outside = tmp_path / "comics-old" / "Batman" / "Issue 002.cbz"
    create_minimal_cbz(outside)
    locations = [comic.name]
    if failure == "missing":
        locations.append("Issue 002.cbz")
    elif failure == "outside":
        mapped_root = outside.parent.parent
        locations = [outside.name]
    elif failure == "file_symlink":
        (comic.parent / outside.name).symlink_to(outside)
        locations.append(outside.name)
    else:
        mapped_root = tmp_path / "alias"
        mapped_root.symlink_to(root_path, target_is_directory=True)
    _mylar_db(database, locations)
    before = _file_snapshot(database), _file_snapshot(comic)
    job = ImportJob(
        source_path=str(database),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.SCANNING,
        file_handling_mode=ImportFileHandlingMode.IN_PLACE,
        target_library_root_id=root.id,
        mylar3_path_map={"/comics": str(mapped_root)},
        mylar3_path_map_confirmed=True,
    )
    db_session.add(job)
    await db_session.flush()

    await _load_mylar3_discovered_series(
        db_session,
        job,
        job_id=job.id,
        mylar3_reader_cls=Mylar3Reader,
        auto_detect_mylar3_path_map=lambda _path: None,
        log_event=_log_event,
        validate_discovered_files_safety=validate_discovered_files_safety,
        materialize_discovered_scan_results=materialize_discovered_scan_results,
    )

    rows = list((await db_session.scalars(select(ImportedFile))).all())
    blocked = [row for row in rows if row.status == ImportedFileStatus.SAFETY_BLOCKED]
    assert len(blocked) == 1
    assert blocked[0].include_in_import is False
    block = blocked[0].diagnostics["safety_block"]
    assert block["code"] == ("source_missing" if failure == "missing" else "source_outside_root")
    assert block["overrideable"] is False
    if failure in {"missing", "file_symlink"}:
        assert len(rows) == 2
        valid = next(row for row in rows if row.file_name == comic.name)
        expected_signature = build_file_identity_signature(comic)
        expected_signature[MYLAR_REFERENCE_ROOT_ID_SIGNATURE_KEY] = root.id
        assert valid.source_signature == expected_signature
        assert valid.status == ImportedFileStatus.PENDING
    else:
        imported_series = (await db_session.scalars(select(ImportedSeries))).one()
        assert imported_series.status == ImportSeriesStatus.NO_MATCH
        assert imported_series.diagnostics["kind"] == "mylar3_path_incompatible"
    assert (_file_snapshot(database), _file_snapshot(comic)) == before
