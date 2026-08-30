"""Mylar in-place imports validate comic paths, not the source database root."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

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


async def test_mylar_in_place_creation_accepts_database_outside_selected_root(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    comics = tmp_path / "comics"
    comics.mkdir()
    root = LibraryRoot(name="Existing comics", path=str(comics), enabled=True)
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
            target_library_root_id=root.id,
            file_handling_mode=ImportFileHandlingMode.IN_PLACE,
        ),
        log_event=_log_event,
    )

    assert job.source_path == str(database)
    assert job.target_library_root_id == root.id
    assert job.effective_transfer_method == "leave_in_place"
    assert job.move_to_library is False
    assert job.convert_to_preferred_format is False
    assert job.update_embedded_comicinfo_from_match is False
    assert _file_snapshot(database) == before


@pytest.mark.parametrize("selection", ["none", "disabled", "missing"])
async def test_mylar_in_place_requires_an_existing_enabled_selected_root(
    db_session: AsyncSession, tmp_path: Path, selection: str
) -> None:
    database = tmp_path / "mylar.db"
    _mylar_db(database, [])
    root = LibraryRoot(name="Disabled", path=str(tmp_path), enabled=False)
    db_session.add(root)
    await db_session.flush()
    root_id = None if selection == "none" else root.id if selection == "disabled" else 999999

    with pytest.raises(ValidationError, match=r"[Ll]ibrary root"):
        await create_job(
            db_session,
            ImportJobCreate(
                source_path=str(database),
                source_type=ImportSourceType.MYLAR3,
                target_library_root_id=root_id,
                file_handling_mode=ImportFileHandlingMode.IN_PLACE,
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
                "ComicID": comic_id,
                "ComicName": title,
                "ComicYear": "2016",
                "ComicLocation": "/comics/Batman",
            }
            for comic_id, title in [("CV-97508", "Batman"), ("CV-99999", "Batman Annual")]
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
        assert valid.source_signature == build_file_identity_signature(comic)
        assert valid.status == ImportedFileStatus.PENDING
    else:
        imported_series = (await db_session.scalars(select(ImportedSeries))).one()
        assert imported_series.status == ImportSeriesStatus.NO_MATCH
        assert imported_series.diagnostics["kind"] == "mylar3_path_incompatible"
    assert (_file_snapshot(database), _file_snapshot(comic)) == before
