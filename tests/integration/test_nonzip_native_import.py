"""Native identities survive real RAR/7z/TAR import and source rechecks."""

import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

from pullbox.core.archive import ArchiveError, ArchiveReader
from pullbox.core.file_safety import FileSafetyError, run_safety_checks
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.source_metadata import SourceMetadataExtractor
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.schemas.import_job import ConfirmImportRequest, ImportJobCreate
from pullbox.services.import_content_inspection import inspect_import_content
from pullbox.services.import_review_recheck import inspect_review_source
from tests.integration.test_metron_folder_import import _archive, _service, _setup
from tests.integration.test_mylar_native_extras import seed_mylar_native_extra
from tests.unit.test_nonzip_metadata_writing import nonzip_archive


@pytest.fixture(autouse=True)
def stored_rar_backend(monkeypatch):
    # Real stored RAR entries need no external decoder; compressed RAR still uses UnRAR.
    monkeypatch.setattr("pullbox.core.rar_backend.configure_rarfile_backend", lambda: None)
    monkeypatch.setattr("pullbox.core.archive.configure_rarfile_backend", lambda: None)


def native_archive(path, namespace="metron", *, number="3", series_id="21", issue_id="101"):
    _archive(path, primary=namespace, number=number, series_id=series_id, issue_id=issue_id)
    with zipfile.ZipFile(path) as archive:
        members = [(name, archive.read(name)) for name in archive.namelist()]
    nonzip_archive(path, members)
    return path.read_bytes()


@pytest.mark.parametrize("suffix", [".cbr", ".cb7", ".cbt"])
@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("in_place", [False, True])
async def test_nonzip_native_import_and_rollback(db_session, tmp_path, suffix, namespace, in_place):
    destination, series, issue = await _setup(
        db_session, tmp_path, namespace=IdentityNamespace(namespace)
    )
    source = tmp_path / "source"
    path = source / "Unsorted" / f"unidentified{suffix}"
    original = native_archive(path, namespace)
    if in_place:
        db_session.add(
            LibraryRoot(
                name="Reference",
                path=str(source),
                enabled=True,
                allow_managed_writes=False,
                allow_referenced_registrations=True,
            )
        )
        await db_session.commit()
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_type="filesystem",
            source_path=str(source),
            file_handling_mode="in_place" if in_place else "managed_copy",
            target_library_root_id=None if in_place else destination.id,
        ),
    )
    await service.start_scan(db_session, job.id)
    file = (await db_session.scalars(select(ImportedFile))).one()
    group = await db_session.get(ImportedSeries, file.import_series_id)
    assert file.status == ImportedFileStatus.MATCHED, file.diagnostics
    assert group.series_id == series.id and group.cv_id is None
    assert file.matched_issue_id == issue.id
    assert file.match_method == f"{namespace}_id"
    await service.update_file_selection(db_session, job.id, file.id, include_in_import=True)
    await service.confirm_import(db_session, job.id, ConfirmImportRequest(series_ids=[group.id]))
    await service.run_import(db_session, job.id)
    await db_session.refresh(file)
    assert file.status == ImportedFileStatus.IMPORTED, file.error_message
    placed = (await db_session.scalars(select(LibraryFile))).one()
    assert Path(placed.file_path).read_bytes() == original
    assert await service.rollback_import(db_session, job.id)
    assert path.read_bytes() == original
    assert not (await db_session.scalars(select(LibraryFile))).all()
    for method in ("search_series", "get_series", "get_issues_for_series"):
        getattr(service._metadata_service._provider, method).assert_not_awaited()


@pytest.mark.parametrize("suffix", [".cbr", ".cb7", ".cbt"])
@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("known_conflict", [False, True])
async def test_nonzip_mylar_preserves_unknown_ids_but_blocks_proven_conflicts(
    db_session, tmp_path, suffix, namespace, known_conflict
):
    job, service, _, _, legacy, issue, _, extra = await seed_mylar_native_extra(
        db_session, tmp_path, namespace
    )
    tracked = extra.parent / "Old Harbor 001.cbz"
    path = tracked.with_suffix(suffix)
    original = native_archive(
        path,
        namespace,
        number="1",
        series_id="21" if known_conflict else "22",
        issue_id="101" if known_conflict else "102",
    )
    tracked.unlink()
    with sqlite3.connect(job.source_path) as source_db:
        source_db.execute("UPDATE issues SET Location = ?", (path.name,))
    await service.start_scan(db_session, job.id)
    file = await db_session.scalar(select(ImportedFile).where(ImportedFile.file_path == str(path)))
    group = await db_session.get(ImportedSeries, file.import_series_id)
    assert file.diagnostics["source_metadata"].get("metron_import", {}).get("identities")
    assert group.series_id == legacy.id
    if known_conflict:
        assert file.status == ImportedFileStatus.NO_MATCH
        assert file.matched_issue_id is None
        assert file.diagnostics["conflict_type"] == "mylar_native_identity_conflict"
    else:
        assert file.status == ImportedFileStatus.MATCHED
        assert file.matched_issue_id == issue.id
    assert path.read_bytes() == original
    for method in ("search_series", "get_series", "get_issues_for_series"):
        getattr(service._metadata_service._provider, method).assert_not_awaited()


@pytest.mark.parametrize("suffix", [".cbr", ".cb7", ".cbt"])
@pytest.mark.parametrize("namespace", ["metron", "gcd"])
def test_nonzip_source_recheck_keeps_fresh_native_identity(tmp_path, suffix, namespace):
    path = tmp_path / f"unidentified{suffix}"
    original = native_archive(path, namespace)
    signature = build_file_identity_signature(path)
    base = SourceMetadataExtractor().from_path(path, include_archive_comicinfo=False)
    _, content, current = inspect_review_source(
        path,
        base,
        signature,
        roots=[(tmp_path, tmp_path.resolve())],
        block_dangerous=True,
        max_archive_size=1024 * 1024,
        accept_replaced_files=False,
        sidecars={},
    )
    assert "file_safety" not in content
    assert content.get("metron_import", {}).get("identities") == [
        {"namespace": namespace, "kind": "issue", "id": "101"},
        {"namespace": namespace, "kind": "series", "id": "21"},
    ]
    assert current == signature and path.read_bytes() == original


@pytest.mark.parametrize("suffix", [".cbr", ".cb7", ".cbt"])
def test_nonzip_metadata_read_failure_is_not_empty_success(tmp_path, suffix):
    path = tmp_path / f"unidentified{suffix}"
    original = native_archive(path)
    inspection = run_safety_checks(path, block_dangerous=True, max_archive_size=100000)
    with (
        patch.object(ArchiveReader, "read_metadata_files", side_effect=ArchiveError("unreadable")),
        pytest.raises(FileSafetyError, match="Archive inspection failed"),
    ):
        inspect_import_content(path, inspection, max_archive_size=100000)
    assert path.read_bytes() == original


def test_nonzip_metadata_reader_uses_import_budget(tmp_path):
    path = tmp_path / "unidentified.cb7"
    native_archive(path)
    inspection = run_safety_checks(path, block_dangerous=True, max_archive_size=100000)
    original_read = ArchiveReader.read_metadata_files
    budgets = []

    def read(reader, *, max_solid_scan_bytes):
        budgets.append(max_solid_scan_bytes)
        return original_read(reader, max_solid_scan_bytes=max_solid_scan_bytes)

    with patch.object(ArchiveReader, "read_metadata_files", read):
        inspect_import_content(path, inspection, max_archive_size=100000)
    assert budgets == [100000]


@pytest.mark.parametrize("suffix", [".cbr", ".cb7", ".cbt"])
@pytest.mark.parametrize("problem", ["duplicate", "oversize", "number_conflict"])
async def test_ambiguous_nonzip_native_metadata_is_not_auto_matched(
    db_session, tmp_path, suffix, problem
):
    destination, _, _ = await _setup(db_session, tmp_path)
    source = tmp_path / "source"
    path = source / "Unsorted" / f"unidentified{suffix}"
    _archive(path)
    with zipfile.ZipFile(path) as archive:
        members = [(name, archive.read(name)) for name in archive.namelist()]
    if problem == "duplicate":
        members.append(("nested/MetronInfo.xml", members[-1][1]))
    elif problem == "oversize":
        members[-1] = ("MetronInfo.xml", members[-1][1] + b" " * (2 * 1024 * 1024))
    else:
        members.append(("ComicInfo.xml", b"<ComicInfo><Number>4</Number></ComicInfo>"))
    nonzip_archive(path, members)
    original = path.read_bytes()
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_type="filesystem", source_path=str(source), target_library_root_id=destination.id
        ),
    )
    await service.start_scan(db_session, job.id)
    file = (await db_session.scalars(select(ImportedFile))).one()
    assert file.status == ImportedFileStatus.NO_MATCH
    assert file.matched_issue_id is None
    assert file.diagnostics["source_metadata"]["metron_import"]["problems"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("suffix", [".cbr", ".cb7", ".cbt"])
async def test_unsafe_nonzip_never_reads_native_metadata(db_session, tmp_path, suffix):
    destination, _, _ = await _setup(db_session, tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    # py7zr itself refuses to create traversal members; exercise its payload gate instead.
    member = "payload.exe" if suffix == ".cb7" else "../escape.jpg"
    path = nonzip_archive(source / f"unsafe{suffix}", [(member, b"unsafe")])
    original = path.read_bytes()
    service = _service()
    job = await service.create_job(
        db_session,
        ImportJobCreate(
            source_type="filesystem", source_path=str(source), target_library_root_id=destination.id
        ),
    )
    with patch.object(ArchiveReader, "read_metadata_files") as read:
        await service.start_scan(db_session, job.id)
    read.assert_not_called()
    file = (await db_session.scalars(select(ImportedFile))).one()
    assert file.status == ImportedFileStatus.SAFETY_BLOCKED
    assert file.matched_issue_id is None
    assert path.read_bytes() == original


def test_zip_inspection_does_not_open_another_metadata_session(tmp_path):
    path = tmp_path / "ordinary.cbz"
    _archive(path)
    inspection = run_safety_checks(path, block_dangerous=True, max_archive_size=100000)
    with patch.object(ArchiveReader, "read_metadata_files") as read:
        inspect_import_content(path, inspection, max_archive_size=100000)
    read.assert_not_called()


@pytest.mark.parametrize("suffix", [".cbr", ".cb7", ".cbt"])
def test_untagged_nonzip_avoids_an_extra_metadata_session(tmp_path, suffix):
    path = nonzip_archive(tmp_path / f"ordinary{suffix}", [("1.jpg", b"one"), ("2.jpg", b"two")])
    inspection = run_safety_checks(path, block_dangerous=True, max_archive_size=100000)
    with patch.object(ArchiveReader, "read_metadata_files") as read:
        result = inspect_import_content(path, inspection, max_archive_size=100000)
    read.assert_not_called()
    assert "metron_import" not in result
    assert result["content_inspection"]["page_count"] == 2
