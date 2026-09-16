"""Coverage for DB check preview and repair service helpers."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from pullbox.core.exceptions import ValidationError
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import (
    FileFormat,
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
    MatchConfidence,
)
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series, SeriesStatus
from pullbox.services import db_check_service
from pullbox.services.db_check_service import (
    build_referential_findings,
    normalize_library_path,
    refresh_library_file_filesystem_fields,
    refresh_library_file_metadata,
    register_stale_library_file,
    repair_library_file_root_id,
    repair_series_path,
    repair_series_root_id,
    resolve_enabled_root_for_path,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


def _series(
    *,
    title: str,
    publisher_id: int | None = None,
    library_root_id: int | None = None,
    path: str | None = None,
    comicvine_id: int | None = None,
) -> Series:
    return Series(
        title=title,
        sort_title=title,
        comicvine_id=comicvine_id,
        year_start=2024,
        status=SeriesStatus.CONTINUING,
        monitored=True,
        publisher_id=publisher_id,
        library_root_id=library_root_id,
        path=path,
    )


def _issue(*, series_id: int, number: float = 1.0) -> Issue:
    return Issue(
        series_id=series_id,
        issue_number=number,
        title=f"Issue {number:g}",
        status=IssueStatus.OWNED,
    )


def _library_file(
    *,
    file_path: Path,
    issue_id: int | None = None,
    library_root_id: int | None = None,
    confidence: MatchConfidence = MatchConfidence.HIGH,
) -> LibraryFile:
    return LibraryFile(
        issue_id=issue_id,
        library_root_id=library_root_id,
        file_path=str(file_path),
        file_name=file_path.name,
        file_size=file_path.stat().st_size if file_path.exists() else 1,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(tz=UTC),
        match_confidence=confidence,
    )


def test_path_and_candidate_helpers(tmp_path: Path) -> None:
    root = LibraryRoot(name="Root", path=str(tmp_path / "library"), enabled=True)
    nested = LibraryRoot(name="Nested", path=str(tmp_path / "library" / "deep"), enabled=True)

    assert normalize_library_path(None) is None
    assert db_check_service._path_has_prefix(None, root.path) is False
    assert resolve_enabled_root_for_path(None, [root]) is None
    assert resolve_enabled_root_for_path(tmp_path / "elsewhere.cbz", [root]) is None
    assert (
        resolve_enabled_root_for_path(tmp_path / "library" / "deep" / "issue.cbz", [root, nested])
        is nested
    )

    series = _series(title="Batman", path="/old/Batman")
    assert db_check_service._infer_series_candidate_path(series, None) is None
    assert db_check_service._infer_series_candidate_path(series, Counter({"/new/Batman": 1})) == (
        "/new/Batman"
    )
    assert (
        db_check_service._infer_series_candidate_path(
            series,
            Counter({"/new/Batman": 1, "/other/Detective": 4}),
        )
        == "/new/Batman"
    )
    assert (
        db_check_service._infer_series_candidate_path(
            _series(title="Batman", path=None),
            Counter({"/new/Batman": 4, "/other/Batman": 1}),
        )
        == "/new/Batman"
    )
    assert (
        db_check_service._infer_series_candidate_path(
            _series(title="Batman", path=None),
            Counter({"/new/Batman": 2, "/other/Batman": 2}),
        )
        is None
    )

    assert db_check_service._context_dict({"context": {"repair_kind": "series_path"}}) == {
        "repair_kind": "series_path"
    }
    assert db_check_service._context_dict({"context": "nope"}) == {}
    assert db_check_service._int_or_none(None) is None
    assert db_check_service._int_or_none(True) == 1
    assert db_check_service._int_or_none(2.9) == 2
    assert db_check_service._int_or_none("12") == 12
    assert db_check_service._int_or_none("nope") is None
    assert db_check_service._int_or_none(object()) is None


def test_repaired_library_file_path_resolution(tmp_path: Path) -> None:
    old_folder = tmp_path / "old"
    target_folder = tmp_path / "target"
    old_folder.mkdir()
    target_folder.mkdir()

    already_moved = target_folder / "Issue 001.cbz"
    already_moved.write_text("moved")
    assert db_check_service._resolve_repaired_library_file_path(
        current_path=str(already_moved),
        old_series_path=str(old_folder),
        target_series_path=str(target_folder),
    ) == normalize_library_path(already_moved)

    stale_old_path = old_folder / "Issue 002.cbz"
    assert db_check_service._resolve_repaired_library_file_path(
        current_path=str(stale_old_path),
        old_series_path=str(old_folder),
        target_series_path=str(target_folder),
    ) == normalize_library_path(target_folder / stale_old_path.name)

    renamed_target = target_folder / "Issue 003.cbz"
    renamed_target.write_text("target")
    missing_external = tmp_path / "external" / renamed_target.name
    assert db_check_service._resolve_repaired_library_file_path(
        current_path=str(missing_external),
        old_series_path=str(old_folder),
        target_series_path=str(target_folder),
    ) == normalize_library_path(renamed_target)

    external_existing = tmp_path / "external-existing.cbz"
    external_existing.write_text("external")
    assert db_check_service._resolve_repaired_library_file_path(
        current_path=str(external_existing),
        old_series_path=str(old_folder),
        target_series_path=str(target_folder),
    ) == normalize_library_path(external_existing)

    assert (
        db_check_service._resolve_repaired_library_file_path(
            current_path=str(tmp_path / "gone.cbz"),
            old_series_path=None,
            target_series_path=str(target_folder),
        )
        is None
    )


def test_directory_index_resolves_by_comicvine_id_and_unique_folder_name(tmp_path: Path) -> None:
    root_path = tmp_path / "library"
    root_path.mkdir()
    (root_path / ".trash").mkdir()
    (root_path / ".hidden").mkdir()
    cv_folder = root_path / "Batman [cv-1234]"
    named_folder = root_path / "Superman"
    cv_folder.mkdir()
    named_folder.mkdir()

    index = db_check_service._DirectoryIndex.build(
        [
            LibraryRoot(name="Missing", path=str(tmp_path / "missing"), enabled=True),
            LibraryRoot(name="Library", path=str(root_path), enabled=True),
        ]
    )

    assert index.find_candidate(_series(title="Batman", comicvine_id=1234)) == (
        normalize_library_path(cv_folder)
    )
    assert index.find_candidate(_series(title="Superman", path="/old/Superman")) == (
        normalize_library_path(named_folder)
    )
    assert index.find_candidate(_series(title="Wonder Woman", path="/old/Wonder Woman")) is None


@pytest.mark.asyncio
async def test_build_referential_findings_reports_repairable_path_and_root_issues(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_one_path = tmp_path / "library-one"
    root_two_path = tmp_path / "library-two"
    root_one_path.mkdir()
    root_two_path.mkdir()
    parent_candidate = root_one_path / "Parent Candidate"
    directory_candidate = root_one_path / "Directory Candidate [cv-202]"
    root_mismatch_folder = root_two_path / "Root Mismatch"
    file_mismatch_folder = root_two_path / "File Mismatch"
    for folder in (
        parent_candidate,
        directory_candidate,
        root_mismatch_folder,
        file_mismatch_folder,
    ):
        folder.mkdir()

    parent_file = parent_candidate / "Parent Candidate 001.cbz"
    file_root_mismatch = file_mismatch_folder / "File Mismatch 001.cbz"
    parent_file.write_text("parent")
    file_root_mismatch.write_text("file")

    session = db_session
    publisher = Publisher(name="DC Comics")
    root_one = LibraryRoot(name="One", path=str(root_one_path), enabled=True)
    root_two = LibraryRoot(name="Two", path=str(root_two_path), enabled=True)
    session.add_all([publisher, root_one, root_two])
    await session.flush()

    stale_by_parent = _series(
        title="Parent Candidate",
        publisher_id=publisher.id,
        library_root_id=root_one.id,
        path=str(root_one_path / "Old Parent Candidate"),
        comicvine_id=101,
    )
    stale_by_directory = _series(
        title="Directory Candidate",
        publisher_id=publisher.id,
        library_root_id=root_one.id,
        path=str(root_one_path / "Old Directory Candidate"),
        comicvine_id=202,
    )
    series_root_mismatch = _series(
        title="Root Mismatch",
        publisher_id=publisher.id,
        library_root_id=root_one.id,
        path=str(root_mismatch_folder),
        comicvine_id=303,
    )
    file_root_series = _series(
        title="File Mismatch",
        publisher_id=publisher.id,
        library_root_id=root_two.id,
        path=str(file_mismatch_folder),
        comicvine_id=404,
    )
    session.add_all([stale_by_parent, stale_by_directory, series_root_mismatch, file_root_series])
    await session.flush()

    parent_issue = _issue(series_id=stale_by_parent.id)
    file_issue = _issue(series_id=file_root_series.id)
    session.add_all([parent_issue, file_issue])
    await session.flush()
    file_root_library_file = _library_file(
        file_path=file_root_mismatch,
        issue_id=file_issue.id,
        library_root_id=root_one.id,
    )
    session.add_all(
        [
            _library_file(
                file_path=parent_file,
                issue_id=parent_issue.id,
                library_root_id=root_one.id,
            ),
            file_root_library_file,
        ]
    )
    await session.flush()

    findings = await build_referential_findings(session)

    by_id = {finding["finding_id"]: finding for finding in findings}
    assert by_id[f"referential-series-path-{stale_by_parent.id}"]["context"][
        "target_path"
    ] == normalize_library_path(parent_candidate)
    assert by_id[f"referential-series-path-{stale_by_directory.id}"]["context"][
        "target_path"
    ] == normalize_library_path(directory_candidate)
    assert (
        by_id[f"referential-series-root-{series_root_mismatch.id}"]["context"]["target_root_id"]
        == root_two.id
    )
    assert (
        by_id[f"referential-library-file-root-{file_root_library_file.id}"]["record_type"]
        == "library_file"
    )
    assert (
        by_id[f"referential-library-file-root-{file_root_library_file.id}"]["context"][
            "target_root_id"
        ]
        == root_two.id
    )


@pytest.mark.asyncio
async def test_root_id_repairs_reject_targets_that_do_not_contain_the_record_path(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"
    series_path = first_path / "Series"
    file_path = series_path / "Series 001.cbz"
    series_path.mkdir(parents=True)
    second_path.mkdir()
    file_path.write_bytes(b"comic")
    first = LibraryRoot(name="First", path=str(first_path), enabled=True)
    second = LibraryRoot(name="Second", path=str(second_path), enabled=True)
    series = _series(title="Series", path=str(series_path))
    db_session.add_all([first, second, series])
    await db_session.flush()
    issue = _issue(series_id=series.id)
    db_session.add(issue)
    await db_session.flush()
    library_file = _library_file(
        file_path=file_path,
        issue_id=issue.id,
        library_root_id=first.id,
    )
    db_session.add(library_file)
    await db_session.flush()

    with pytest.raises(ValidationError, match="does not contain"):
        await repair_series_root_id(
            db_session,
            series_id=series.id,
            target_root_id=second.id,
        )
    with pytest.raises(ValidationError, match="does not contain"):
        await repair_library_file_root_id(
            db_session,
            library_file_id=library_file.id,
            target_root_id=second.id,
        )

    assert series.library_root_id is None
    assert library_file.library_root_id == first.id


@pytest.mark.asyncio
async def test_register_stale_library_file_resolves_formats_roots_series_and_issues(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "library"
    root_path.mkdir()
    exact_folder = root_path / "Batman"
    fallback_folder = root_path / "Fallback"
    orphan_folder = root_path / "No Series"
    for folder in (exact_folder, fallback_folder, orphan_folder):
        folder.mkdir()

    exact_file = exact_folder / "Batman 001 (2024).cbz"
    fallback_file = fallback_folder / "Fallback Special.cbz"
    orphan_file = orphan_folder / "No Series 001.cbz"
    unsupported_file = root_path / "notes.txt"
    outside_file = tmp_path / "Outside 001.cbz"
    existing_file = exact_folder / "Batman 002.cbz"
    for path in (
        exact_file,
        fallback_file,
        orphan_file,
        unsupported_file,
        outside_file,
        existing_file,
    ):
        path.write_text(path.name)

    session = db_session
    publisher = Publisher(name="DC Comics")
    root = LibraryRoot(name="Library", path=str(root_path), enabled=True)
    session.add_all([publisher, root])
    await session.flush()

    exact_series = _series(
        title="Batman",
        publisher_id=publisher.id,
        library_root_id=root.id,
        path=str(exact_folder),
    )
    fallback_series = _series(
        title="Fallback",
        publisher_id=publisher.id,
        library_root_id=root.id,
        path=str(tmp_path / "old" / "Fallback"),
    )
    session.add_all([exact_series, fallback_series])
    await session.flush()
    exact_issue = _issue(series_id=exact_series.id)
    session.add(exact_issue)
    await session.flush()
    exact_issue_id = exact_issue.id
    session.add(
        _library_file(
            file_path=existing_file,
            issue_id=exact_issue.id,
            library_root_id=root.id,
        )
    )
    await session.flush()

    missing = await register_stale_library_file(
        session,
        file_path_str=str(root_path / "missing.cbz"),
    )
    unsupported = await register_stale_library_file(session, file_path_str=str(unsupported_file))
    outside = await register_stale_library_file(session, file_path_str=str(outside_file))
    existing = await register_stale_library_file(session, file_path_str=str(existing_file))
    no_series = await register_stale_library_file(session, file_path_str=str(orphan_file))
    exact = await register_stale_library_file(session, file_path_str=str(exact_file))
    fallback = await register_stale_library_file(session, file_path_str=str(fallback_file))
    await session.flush()

    rows = (
        (
            await session.execute(
                select(LibraryFile).where(
                    LibraryFile.file_path.in_([str(exact_file), str(fallback_file)])
                )
            )
        )
        .scalars()
        .all()
    )

    assert missing and missing["reason"] == "File no longer exists on disk."
    assert unsupported and unsupported["reason"] == "Unsupported file format: .txt"
    assert outside and outside["reason"] == "File is not inside any configured library root."
    assert existing is None
    assert no_series and no_series["reason"] == "No matching series found for folder: No Series"
    assert exact is None
    assert fallback is None

    by_name = {row.file_name: row for row in rows}
    assert by_name[exact_file.name].issue_id == exact_issue_id
    assert by_name[exact_file.name].match_confidence == MatchConfidence.HIGH
    assert by_name[exact_file.name].storage_mode is LibraryFileStorageMode.REFERENCED
    assert by_name[exact_file.name].source_signature["resolved_path"] == str(exact_file.resolve())
    assert by_name[fallback_file.name].issue_id is None
    assert by_name[fallback_file.name].match_confidence == MatchConfidence.LOW
    assert by_name[fallback_file.name].storage_mode is LibraryFileStorageMode.REFERENCED


@pytest.mark.asyncio
async def test_register_stale_library_file_refuses_managed_only_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "managed-only"
    series_path = root_path / "Batman"
    series_path.mkdir(parents=True)
    comic = series_path / "Batman 001.cbz"
    comic.write_bytes(b"user-created file")
    root = LibraryRoot(
        name="Managed only",
        path=str(root_path),
        enabled=True,
        allow_referenced_registrations=False,
        allow_managed_writes=True,
    )
    db_session.add(root)
    await db_session.flush()
    db_session.add(_series(title="Batman", library_root_id=root.id, path=str(series_path)))
    await db_session.flush()

    finding = await register_stale_library_file(db_session, file_path_str=str(comic))

    assert finding is not None
    assert "referenced registrations" in finding["reason"]
    assert await db_session.scalar(select(LibraryFile.id)) is None


@pytest.mark.asyncio
async def test_repair_series_path_preserves_split_file_root_associations(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    old_root_path = tmp_path / "old-root"
    target_root_path = tmp_path / "target-root"
    archive_root_path = tmp_path / "archive-root"
    old_series_path = old_root_path / "Batman"
    target_series_path = target_root_path / "Batman"
    archive_series_path = archive_root_path / "Batman"
    for path in (old_series_path, target_series_path, archive_series_path):
        path.mkdir(parents=True)
    moved_file = target_series_path / "Batman 001.cbz"
    archived_file = archive_series_path / "Batman 002.cbz"
    moved_file.write_bytes(b"moved")
    archived_file.write_bytes(b"archive")

    roots = [
        LibraryRoot(name="Old", path=str(old_root_path), enabled=True),
        LibraryRoot(name="Target", path=str(target_root_path), enabled=True),
        LibraryRoot(name="Archive", path=str(archive_root_path), enabled=True),
    ]
    db_session.add_all(roots)
    await db_session.flush()
    series = _series(
        title="Batman",
        library_root_id=roots[0].id,
        path=str(old_series_path),
    )
    db_session.add(series)
    await db_session.flush()
    issues = [_issue(series_id=series.id, number=1), _issue(series_id=series.id, number=2)]
    db_session.add_all(issues)
    await db_session.flush()
    moved = _library_file(
        file_path=old_series_path / moved_file.name,
        issue_id=issues[0].id,
        library_root_id=roots[0].id,
    )
    archived = _library_file(
        file_path=archived_file,
        issue_id=issues[1].id,
        library_root_id=roots[2].id,
    )
    db_session.add_all([moved, archived])
    await db_session.flush()

    await repair_series_path(
        db_session,
        series_id=series.id,
        target_path=str(target_series_path),
    )

    assert series.path == str(target_series_path)
    assert series.library_root_id == roots[1].id
    assert moved.file_path == str(moved_file)
    assert moved.library_root_id == roots[1].id
    assert archived.file_path == str(archived_file)
    assert archived.library_root_id == roots[2].id


@pytest.mark.asyncio
async def test_repair_and_refresh_noop_branches(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "library"
    root_path.mkdir()
    file_path = root_path / "Unmatched 003 (2024).cbz"
    file_path.write_text("comic")

    session = db_session
    root = LibraryRoot(name="Library", path=str(root_path), enabled=True)
    session.add(root)
    await session.flush()

    library_file = LibraryFile(
        issue_id=None,
        library_root_id=root.id,
        file_path=str(file_path),
        file_name="wrong.cbz",
        file_size=1,
        file_format=FileFormat.CBR,
        file_modified_at=datetime(2001, 1, 1, tzinfo=UTC),
        match_confidence=MatchConfidence.UNMATCHED,
    )
    missing_file = LibraryFile(
        issue_id=None,
        library_root_id=root.id,
        file_path=str(root_path / "missing.cbz"),
        file_name="missing.cbz",
        file_size=1,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime(2001, 1, 1, tzinfo=UTC),
        match_confidence=MatchConfidence.UNMATCHED,
    )
    session.add_all([library_file, missing_file])
    await session.flush()

    await repair_series_path(session, series_id=9999, target_path=str(root_path))
    await repair_series_root_id(session, series_id=9999, target_root_id=root.id)
    await repair_library_file_root_id(session, library_file_id=9999, target_root_id=root.id)

    assert await refresh_library_file_metadata(missing_file, enabled_roots=[root]) is False
    assert await refresh_library_file_metadata(
        library_file,
        enabled_roots=[],
        fallback_root=root,
    )
    await refresh_library_file_filesystem_fields(missing_file)

    assert library_file.file_name == file_path.name
    assert library_file.file_size == file_path.stat().st_size
    assert library_file.file_format == FileFormat.CBZ
    assert library_file.parsed_series == "Unmatched"
    assert library_file.parsed_issue_number == 3.0
    assert library_file.parsed_year == 2024
    assert library_file.library_root_id == root.id
    assert library_file.has_comicinfo is False
