"""Guardrails for physically cleaning up verified misplaced Mylar files."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING

import pytest

from pullbox.core.exceptions import ValidationError
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.models.config import SystemConfig
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobAction,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.library import (
    FileFormat,
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
    MatchConfidence,
)
from pullbox.models.series import Series
from pullbox.models.user import User
from pullbox.services.import_misplaced_source_cleanup import (
    MisplacedSourceCleanupAction,
    apply_misplaced_source_cleanup,
    apply_verified_misplaced_source_cleanup,
    count_misplaced_source_cleanup_files,
    list_misplaced_source_cleanup_files,
    preview_misplaced_source_cleanup,
    preview_verified_misplaced_source_cleanup,
)

if TYPE_CHECKING:
    from pathlib import Path


async def _seed_recovered_file(db_session, tmp_path: Path, *, writable: bool = True):
    root_path = tmp_path / "comics"
    expected = root_path / "Absolute Batman (2024)" / "Absolute Batman (2024) #001.cbz"
    source = root_path / "Crossed Badlands (2012)" / expected.name
    expected.parent.mkdir(parents=True, exist_ok=True)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"absolute batman issue one")
    trash = tmp_path / "trash"
    root = LibraryRoot(
        name="Mylar",
        path=str(root_path),
        enabled=True,
        allow_referenced_registrations=True,
        allow_managed_writes=writable,
    )
    user = User(id=42, username="operator", password_hash="unused")
    db_session.add_all(
        [
            root,
            user,
            SystemConfig(key="utility_trash_folder", value=str(trash), value_type="string"),
        ]
    )
    await db_session.flush()
    series = Series(
        title="Absolute Batman",
        sort_title="absolute batman",
        year_start=2024,
        comicvine_id=160294,
        library_root_id=root.id,
    )
    db_session.add(series)
    await db_session.flush()
    issue = Issue(
        series_id=series.id,
        issue_number=1,
        issue_number_text="1",
        comicvine_id=1073108,
    )
    job = ImportJob(
        source_path=str(tmp_path / "mylar.db"),
        source_type=ImportSourceType.MYLAR3,
        status=ImportJobStatus.COMPLETED,
    )
    db_session.add_all([issue, job])
    await db_session.flush()
    imported_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name=series.title,
        status=ImportSeriesStatus.IMPORTED,
        series_id=series.id,
    )
    db_session.add(imported_series)
    await db_session.flush()
    signature = build_file_identity_signature(source)
    library_file = LibraryFile(
        issue_id=issue.id,
        library_root_id=root.id,
        file_path=str(source),
        file_name=source.name,
        file_size=source.stat().st_size,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(UTC),
        match_confidence=MatchConfidence.HIGH,
        storage_mode=LibraryFileStorageMode.REFERENCED,
        source_signature=signature,
    )
    db_session.add(library_file)
    await db_session.flush()
    imported_file = ImportedFile(
        import_job_id=job.id,
        import_series_id=imported_series.id,
        file_path=str(source),
        file_name=source.name,
        file_size=source.stat().st_size,
        file_format="cbz",
        source_signature=signature,
        status=ImportedFileStatus.IMPORTED,
        matched_issue_id=issue.id,
        matched_issue_cv_id=issue.comicvine_id,
        library_file_id=library_file.id,
        diagnostics={
            "mylar3_cross_folder_reconciliation": {
                "recorded_path": str(expected),
                "actual_path": str(source),
                "comicvine_issue_id": 1073108,
                "comicvine_series_id": 160294,
                "method": "verified_cross_folder_issue_identity",
                "role": "canonical",
                "source_series": "Crossed Badlands",
            }
        },
    )
    db_session.add(imported_file)
    await db_session.flush()
    action = ImportJobAction(
        import_job_id=job.id,
        sequence_no=1,
        phase="import",
        action_type="library_file_registered",
        payload={
            "imported_file_id": imported_file.id,
            "library_file_id": library_file.id,
            "destination_path": str(source),
            "destination_signature": signature,
            "original_source_path": str(source),
            "transfer_method": "leave_in_place",
            "storage_mode": "referenced",
        },
    )
    db_session.add(action)
    await db_session.commit()
    return job, imported_file, library_file, action, source, expected, trash


@pytest.mark.asyncio
async def test_restore_preview_and_apply_move_to_exact_missing_mylar_path(
    db_session,
    tmp_path: Path,
) -> None:
    job, imported_file, library_file, action, source, expected, _trash = await _seed_recovered_file(
        db_session, tmp_path
    )

    preview = await preview_misplaced_source_cleanup(
        db_session,
        job.id,
        imported_file.id,
        MisplacedSourceCleanupAction.RESTORE_RECORDED_PATH,
        actor_id=42,
    )
    assert preview.can_apply is True
    assert preview.destination_path == str(expected)
    assert preview.preview_token is not None
    assert source.exists()
    assert not expected.exists()

    result = await apply_misplaced_source_cleanup(
        db_session,
        job.id,
        imported_file.id,
        MisplacedSourceCleanupAction.RESTORE_RECORDED_PATH,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    await db_session.refresh(imported_file)
    await db_session.refresh(library_file)
    await db_session.refresh(action)
    assert result.final_path == expected.resolve()
    assert not source.exists()
    assert expected.read_bytes() == b"absolute batman issue one"
    assert imported_file.file_path == str(expected.resolve())
    assert library_file.file_path == str(expected.resolve())
    assert library_file.source_signature == build_file_identity_signature(expected)
    assert action.payload["destination_path"] == str(expected.resolve())
    assert action.payload["original_source_path"] == str(expected.resolve())


@pytest.mark.asyncio
async def test_restore_uses_explicit_preview_as_one_time_reference_root_authorization(
    db_session,
    tmp_path: Path,
) -> None:
    (
        job,
        imported_file,
        _library_file,
        _action,
        source,
        expected,
        _trash,
    ) = await _seed_recovered_file(db_session, tmp_path, writable=False)

    preview = await preview_misplaced_source_cleanup(
        db_session,
        job.id,
        imported_file.id,
        MisplacedSourceCleanupAction.RESTORE_RECORDED_PATH,
        actor_id=42,
    )

    assert preview.can_apply is True
    assert preview.preview_token is not None

    await apply_misplaced_source_cleanup(
        db_session,
        job.id,
        imported_file.id,
        MisplacedSourceCleanupAction.RESTORE_RECORDED_PATH,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    assert not source.exists()
    assert expected.exists()
    root = await db_session.get(LibraryRoot, 1)
    assert root is not None
    await db_session.refresh(root)
    assert root.allow_managed_writes is False


@pytest.mark.asyncio
async def test_bulk_restore_is_actor_bound_and_restores_source_on_commit_failure(
    db_session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, _file, _library_file, _action, source, expected, _trash = await _seed_recovered_file(
        db_session, tmp_path, writable=False
    )
    preview = await preview_verified_misplaced_source_cleanup(
        db_session,
        job.id,
        actor_id=42,
    )
    assert preview.preview_token is not None

    with pytest.raises(ValidationError, match="scope changed"):
        await apply_verified_misplaced_source_cleanup(
            db_session,
            job.id,
            actor_id=43,
            preview_token=preview.preview_token,
        )
    assert source.exists()
    assert not expected.exists()

    async def fail_commit() -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db_session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await apply_verified_misplaced_source_cleanup(
            db_session,
            job.id,
            actor_id=42,
            preview_token=preview.preview_token,
        )

    assert source.exists()
    assert not expected.exists()


@pytest.mark.asyncio
async def test_bulk_restore_moves_all_verified_files_and_excludes_unavailable_candidates(
    db_session,
    tmp_path: Path,
) -> None:
    (
        job,
        first,
        _library_file,
        _action,
        first_source,
        first_expected,
        _trash,
    ) = await _seed_recovered_file(db_session, tmp_path, writable=False)
    root = await db_session.get(LibraryRoot, 1)
    assert root is not None
    first_issue = await db_session.get(Issue, first.matched_issue_id)
    assert first_issue is not None

    second_source = first_source.with_name("Absolute Batman (2024) #002.cbz")
    second_expected = first_expected.with_name("Absolute Batman (2024) #002.cbz")
    second_source.write_bytes(b"absolute batman issue two")
    issue = Issue(
        series_id=first_issue.series_id,
        issue_number=2,
        issue_number_text="2",
        comicvine_id=1073109,
    )
    db_session.add(issue)
    await db_session.flush()
    signature = build_file_identity_signature(second_source)
    library_file = LibraryFile(
        issue_id=issue.id,
        library_root_id=root.id,
        file_path=str(second_source),
        file_name=second_source.name,
        file_size=second_source.stat().st_size,
        file_format=FileFormat.CBZ,
        file_modified_at=datetime.now(UTC),
        match_confidence=MatchConfidence.HIGH,
        storage_mode=LibraryFileStorageMode.REFERENCED,
        source_signature=signature,
    )
    db_session.add(library_file)
    await db_session.flush()
    second = ImportedFile(
        import_job_id=job.id,
        import_series_id=first.import_series_id,
        file_path=str(second_source),
        file_name=second_source.name,
        file_size=second_source.stat().st_size,
        file_format="cbz",
        source_signature=signature,
        status=ImportedFileStatus.IMPORTED,
        matched_issue_id=issue.id,
        matched_issue_cv_id=issue.comicvine_id,
        library_file_id=library_file.id,
        diagnostics={
            "mylar3_cross_folder_reconciliation": {
                "recorded_path": str(second_expected),
                "actual_path": str(second_source),
                "comicvine_issue_id": issue.comicvine_id,
                "comicvine_series_id": 160294,
                "method": "verified_cross_folder_issue_identity",
                "role": "canonical",
                "source_series": "Crossed Badlands",
            }
        },
    )
    db_session.add(second)
    await db_session.flush()
    db_session.add(
        ImportJobAction(
            import_job_id=job.id,
            sequence_no=2,
            phase="import",
            action_type="library_file_registered",
            payload={
                "imported_file_id": second.id,
                "library_file_id": library_file.id,
                "destination_path": str(second_source),
                "destination_signature": signature,
                "original_source_path": str(second_source),
                "transfer_method": "leave_in_place",
                "storage_mode": "referenced",
            },
        )
    )
    blocked_source = first_source.with_name("Absolute Batman (2024) #003.cbz")
    blocked_expected = first_expected.with_name("Absolute Batman (2024) #003.cbz")
    blocked_source.write_bytes(b"source")
    blocked_expected.write_bytes(b"occupied")
    blocked = ImportedFile(
        import_job_id=job.id,
        import_series_id=first.import_series_id,
        file_path=str(blocked_source),
        file_name=blocked_source.name,
        file_size=blocked_source.stat().st_size,
        file_format="cbz",
        source_signature=build_file_identity_signature(blocked_source),
        status=ImportedFileStatus.IMPORTED,
        matched_issue_id=issue.id,
        matched_issue_cv_id=issue.comicvine_id,
        library_file_id=library_file.id,
        diagnostics={
            "mylar3_cross_folder_reconciliation": {
                "recorded_path": str(blocked_expected),
                "actual_path": str(blocked_source),
                "method": "verified_cross_folder_issue_identity",
                "role": "canonical",
            }
        },
    )
    db_session.add(blocked)
    await db_session.commit()

    preview = await preview_verified_misplaced_source_cleanup(
        db_session,
        job.id,
        actor_id=42,
    )

    assert preview.affected_count == 2
    assert preview.unavailable_count == 1
    assert preview.preview_token is not None

    result = await apply_verified_misplaced_source_cleanup(
        db_session,
        job.id,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    assert result.moved_count == 2
    assert result.skipped_count == 1
    assert first_expected.exists()
    assert second_expected.exists()
    assert blocked_source.exists()
    assert blocked_expected.read_bytes() == b"occupied"


@pytest.mark.asyncio
async def test_restore_accepts_series_filename_fallback_evidence(
    db_session,
    tmp_path: Path,
) -> None:
    (
        job,
        imported_file,
        _library_file,
        _action,
        _source,
        _expected,
        _trash,
    ) = await _seed_recovered_file(db_session, tmp_path)
    diagnostics = dict(imported_file.diagnostics)
    evidence = dict(diagnostics["mylar3_cross_folder_reconciliation"])
    evidence["method"] = "verified_cross_folder_series_issue_filename"
    diagnostics["mylar3_cross_folder_reconciliation"] = evidence
    imported_file.diagnostics = diagnostics
    await db_session.commit()

    preview = await preview_misplaced_source_cleanup(
        db_session,
        job.id,
        imported_file.id,
        MisplacedSourceCleanupAction.RESTORE_RECORDED_PATH,
        actor_id=42,
    )

    assert preview.can_apply is True
    assert preview.preview_token is not None


@pytest.mark.asyncio
async def test_restore_refuses_an_occupied_recorded_path(db_session, tmp_path: Path) -> None:
    (
        job,
        imported_file,
        _library_file,
        _action,
        source,
        expected,
        _trash,
    ) = await _seed_recovered_file(db_session, tmp_path)
    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.write_bytes(b"different user file")

    preview = await preview_misplaced_source_cleanup(
        db_session,
        job.id,
        imported_file.id,
        MisplacedSourceCleanupAction.RESTORE_RECORDED_PATH,
        actor_id=42,
    )

    assert preview.can_apply is False
    assert "already exists" in preview.unavailable_reason
    assert source.exists()
    with pytest.raises(ValidationError, match="already exists"):
        await apply_misplaced_source_cleanup(
            db_session,
            job.id,
            imported_file.id,
            MisplacedSourceCleanupAction.RESTORE_RECORDED_PATH,
            actor_id=42,
            preview_token="invalid",
        )


@pytest.mark.asyncio
async def test_restore_puts_source_back_when_database_commit_fails(
    db_session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        job,
        imported_file,
        _library_file,
        _action,
        source,
        expected,
        _trash,
    ) = await _seed_recovered_file(db_session, tmp_path)
    preview = await preview_misplaced_source_cleanup(
        db_session,
        job.id,
        imported_file.id,
        MisplacedSourceCleanupAction.RESTORE_RECORDED_PATH,
        actor_id=42,
    )

    async def fail_commit() -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db_session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await apply_misplaced_source_cleanup(
            db_session,
            job.id,
            imported_file.id,
            MisplacedSourceCleanupAction.RESTORE_RECORDED_PATH,
            actor_id=42,
            preview_token=preview.preview_token or "",
        )

    assert source.read_bytes() == b"absolute batman issue one"
    assert not expected.exists()


@pytest.mark.asyncio
async def test_identical_duplicate_can_be_moved_to_trash_separately(
    db_session,
    tmp_path: Path,
) -> None:
    job, canonical, _library_file, _action, source, _expected, trash = await _seed_recovered_file(
        db_session, tmp_path
    )
    duplicate_source = source.with_name("Absolute Batman (2024) #001 variant.cbz")
    duplicate_source.write_bytes(source.read_bytes())
    content_hash = sha256(source.read_bytes()).hexdigest()
    canonical.content_hash = content_hash
    duplicate = ImportedFile(
        import_job_id=job.id,
        import_series_id=canonical.import_series_id,
        file_path=str(duplicate_source),
        file_name=duplicate_source.name,
        file_size=duplicate_source.stat().st_size,
        file_format="cbz",
        source_signature=build_file_identity_signature(duplicate_source),
        status=ImportedFileStatus.DUPLICATE_FILE,
        duplicate_of_file_id=canonical.id,
        content_hash=content_hash,
        diagnostics={
            "mylar3_cross_folder_reconciliation": {
                "recorded_path": canonical.diagnostics["mylar3_cross_folder_reconciliation"][
                    "recorded_path"
                ],
                "actual_path": str(duplicate_source),
                "comicvine_issue_id": 1073108,
                "comicvine_series_id": 160294,
                "method": "verified_cross_folder_issue_identity",
                "role": "identical_duplicate",
                "canonical_path": str(source),
                "source_series": "Crossed Badlands",
            }
        },
    )
    db_session.add(duplicate)
    await db_session.commit()

    preview = await preview_misplaced_source_cleanup(
        db_session,
        job.id,
        duplicate.id,
        MisplacedSourceCleanupAction.TRASH_IDENTICAL_DUPLICATE,
        actor_id=42,
    )
    assert preview.can_apply is True
    assert preview.preview_token is not None

    result = await apply_misplaced_source_cleanup(
        db_session,
        job.id,
        duplicate.id,
        MisplacedSourceCleanupAction.TRASH_IDENTICAL_DUPLICATE,
        actor_id=42,
        preview_token=preview.preview_token,
    )

    await db_session.refresh(duplicate)
    assert not duplicate_source.exists()
    assert result.final_path.is_relative_to(trash)
    assert result.final_path.read_bytes() == source.read_bytes()
    assert duplicate.status is ImportedFileStatus.DUPLICATE_FILE
    assert duplicate.diagnostics["misplaced_source_cleanup"]["action"] == (
        "trash_identical_duplicate"
    )
    assert source.exists()


@pytest.mark.asyncio
async def test_cleanup_scope_lists_only_pending_actions(db_session, tmp_path: Path) -> None:
    job, canonical, _library_file, _action, source, _expected, _trash = await _seed_recovered_file(
        db_session, tmp_path
    )
    duplicate_source = source.with_name("Absolute Batman (2024) #001 variant.cbz")
    duplicate_source.write_bytes(source.read_bytes())
    content_hash = sha256(source.read_bytes()).hexdigest()
    canonical.content_hash = content_hash
    duplicate = ImportedFile(
        import_job_id=job.id,
        import_series_id=canonical.import_series_id,
        file_path=str(duplicate_source),
        file_name=duplicate_source.name,
        file_size=duplicate_source.stat().st_size,
        file_format="cbz",
        source_signature=build_file_identity_signature(duplicate_source),
        status=ImportedFileStatus.DUPLICATE_FILE,
        duplicate_of_file_id=canonical.id,
        content_hash=content_hash,
        diagnostics={
            "mylar3_cross_folder_reconciliation": {
                "method": "verified_cross_folder_issue_identity",
                "role": "identical_duplicate",
            }
        },
    )
    completed_duplicate = ImportedFile(
        import_job_id=job.id,
        import_series_id=canonical.import_series_id,
        file_path=str(source.with_name("already-cleaned.cbz")),
        file_name="already-cleaned.cbz",
        file_size=1,
        file_format="cbz",
        status=ImportedFileStatus.DUPLICATE_FILE,
        duplicate_of_file_id=canonical.id,
        content_hash=content_hash,
        diagnostics={
            "mylar3_cross_folder_reconciliation": {
                "method": "verified_cross_folder_issue_identity",
                "role": "identical_duplicate",
            },
            "misplaced_source_cleanup": {
                "action": "trash_identical_duplicate",
            },
        },
    )
    db_session.add_all([duplicate, completed_duplicate])
    await db_session.commit()

    assert (
        await count_misplaced_source_cleanup_files(
            db_session,
            job.id,
            MisplacedSourceCleanupAction.RESTORE_RECORDED_PATH,
        )
        == 1
    )
    assert (
        await count_misplaced_source_cleanup_files(
            db_session,
            job.id,
            MisplacedSourceCleanupAction.TRASH_IDENTICAL_DUPLICATE,
        )
        == 1
    )

    page = await list_misplaced_source_cleanup_files(
        db_session,
        job.id,
        MisplacedSourceCleanupAction.TRASH_IDENTICAL_DUPLICATE,
        page=1,
        page_size=25,
    )
    assert page.total == 1
    assert page.items == (duplicate,)
