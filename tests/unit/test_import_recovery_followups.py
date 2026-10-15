"""Recovery of interrupted, approved work without changing source artifacts."""

import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pullbox.models.import_job import ImportedFileStatus, ImportJobStatus, ImportSourceType
from pullbox.models.user import User
from pullbox.services.import_completed_cleanup import (
    CompletedImportCleanupAction,
    apply_completed_import_cleanup,
    preview_completed_import_cleanup,
    summarize_completed_import_cleanup_scope,
)
from pullbox.services.import_deferred_recovery_execution import prepare_deferred_recovery
from pullbox.services.import_review_recheck import prepare_completed_import_file_recheck
from tests.unit.test_import_completed_cleanup import _seed_mixed_folder_candidate
from tests.unit.test_import_deferred_recovery import add_file, seed
from tests.unit.test_import_recovery_contention import recovery_factory  # noqa: F401
from tests.unit.test_import_review_recheck import _fixture


@pytest.mark.parametrize("source_type", list(ImportSourceType))
async def test_recheck_resumes_selected_files_stranded_in_completed_series(db_session, source_type):
    job, item, _, issue, _ = await seed(db_session, source_type=source_type)
    db_session.add(User(id=42, username="recovery-operator", password_hash="unused"))
    item.diagnostics = {"kind": "known_series_recovery"}
    file = await add_file(
        db_session,
        job,
        item,
        status=ImportedFileStatus.CONFIRMED,
        include_in_import=True,
        matched_issue_cv_id=issue.comicvine_id,
        diagnostics={
            "target_issue_summary": {
                "provider_id": str(issue.comicvine_id),
                "issue_number": 104,
                "issue_number_text": "104",
            }
        },
    )
    unrelated = await add_file(
        db_session,
        job,
        item,
        status=ImportedFileStatus.MATCHED,
        file_path="/comics/Batman/other.cbz",
        include_in_import=False,
    )
    await db_session.commit()
    summary = await summarize_completed_import_cleanup_scope(
        db_session, job.id, CompletedImportCleanupAction.RECHECK_DEFERRED_FILES
    )
    assert summary.affected_count == 1
    preview = await preview_completed_import_cleanup(
        db_session, job.id, CompletedImportCleanupAction.RECHECK_DEFERRED_FILES, actor_id=42
    )
    await apply_completed_import_cleanup(
        db_session,
        job.id,
        CompletedImportCleanupAction.RECHECK_DEFERRED_FILES,
        actor_id=42,
        preview_token=preview.preview_token,
    )
    await db_session.commit()
    await prepare_deferred_recovery(db_session, job.id, metadata_service=AsyncMock())
    assert file.import_series_id != item.id
    assert file.import_series_id in job.progress_snapshot["deferred_recovery"]["series_ids"]
    assert file.status is ImportedFileStatus.CONFIRMED
    assert file.include_in_import
    assert unrelated.import_series_id == item.id
    assert unrelated.status is ImportedFileStatus.MATCHED


async def test_mixed_folder_request_queues_without_applying_file_changes(db_session):
    job, source, _, file, _ = await _seed_mixed_folder_candidate(db_session)
    action = CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES
    preview = await preview_completed_import_cleanup(db_session, job.id, action, actor_id=42)
    result = await apply_completed_import_cleanup(
        db_session,
        job.id,
        action,
        actor_id=42,
        preview_token=preview.preview_token,
        background=True,
    )
    assert file.import_series_id == source.id
    assert file.status is ImportedFileStatus.NO_MATCH
    assert result.requires_import_retry
    assert job.progress_snapshot["deferred_recovery"]["state"] == "mixed_folder"
    await db_session.commit()
    await prepare_deferred_recovery(db_session, job.id, metadata_service=AsyncMock())
    assert file.import_series_id != source.id
    assert file.status is ImportedFileStatus.CONFIRMED


async def test_mixed_folder_api_queues_and_dispatches(db_session, monkeypatch):
    from starlette.requests import Request

    from pullbox.api.v1.import_completed_cleanup import apply_completed_import_cleanup_route
    from pullbox.schemas.import_completed_cleanup import CompletedImportCleanupApplyRequest

    job, source, _, file, _ = await _seed_mixed_folder_candidate(db_session)
    action = CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES
    preview = await preview_completed_import_cleanup(db_session, job.id, action, actor_id=42)
    dispatched = []
    monkeypatch.setattr(
        "pullbox.api.v1.import_completed_cleanup.trigger_import_execute", dispatched.append
    )
    result = await apply_completed_import_cleanup_route(
        job.id,
        action,
        CompletedImportCleanupApplyRequest(
            preview_token=preview.preview_token, confirmation="APPLY CLEANUP"
        ),
        Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)}),
        await db_session.get(User, 42),
        db_session,
    )
    assert dispatched == [job.id]
    assert result.requires_import_retry
    assert job.progress_snapshot["deferred_recovery"]["state"] == "mixed_folder"
    assert file.import_series_id == source.id
    assert file.status is ImportedFileStatus.NO_MATCH


@pytest.mark.parametrize("source_type", list(ImportSourceType))
@pytest.mark.parametrize("legacy_identity_failure", [False, True])
async def test_completed_recheck_preserves_size_approval_and_legacy_retry_scope(
    db_session, tmp_path, monkeypatch, source_type, legacy_identity_failure
):
    job, item, files = await _fixture(db_session, tmp_path, source_type)
    job.status = ImportJobStatus.COMPLETED
    item.cv_id = 115251
    file = files[1]
    file.status = ImportedFileStatus.FAILED
    file.matched_issue_cv_id = 100008
    file.diagnostics = {
        **file.diagnostics,
        "target_issue_summary": {"provider_id": "100008", "issue_number": 8},
        "source_revalidation": {
            "code": "source_identity_changed" if legacy_identity_failure else "source_changed",
            "category": "source_changed",
            "retryable": True,
            "source": "completed_import_recheck",
            "reason": (
                "The source changed or became unavailable after scanning. Rescan before retrying."
            ),
        },
        "safety_exception": {
            "allowed_once": True,
            "previous_block": {
                "code": "archive_decompressed_size_limit",
                "overrideable": True,
            },
        },
    }
    monkeypatch.setattr(
        "pullbox.services.import_review_recheck.get_archive_size_limit_bytes",
        AsyncMock(return_value=1),
    )
    await db_session.flush()
    report = await prepare_completed_import_file_recheck(
        db_session,
        job.id,
        source_roots=[tmp_path],
        apply=True,
        accept_replaced_files=True,
    )
    assert report["files_checked"] == 1
    assert report["files_prepared"] == 1
    assert file.diagnostics["safety_exception"]["allowed_once"]
    assert "source_revalidation" not in file.diagnostics


@pytest.mark.parametrize("source_type", list(ImportSourceType))
@pytest.mark.parametrize(
    "case", ["mixed", "approved", "different_title", "different_number", "wrong_id"]
)
@pytest.mark.parametrize("via_recheck", [False, True])
async def test_recovery_revalidates_exact_mixed_identity_or_size_approval(
    db_session, monkeypatch, source_type, case, via_recheck
):
    from pullbox.core.library_file_ownership import (
        ReferencedFileValidationError,
        build_file_identity_signature,
    )
    from pullbox.models.library import LibraryRoot
    from pullbox.services.import_recovery_source import refresh_recovery_source
    from pullbox.services.import_referenced_sources import MYLAR_REFERENCE_ROOT_ID_SIGNATURE_KEY
    from tests.unit.test_import_file_execution import _setup_full_scenario

    job, item, files, _, issues = await _setup_full_scenario(db_session, num_issues=1)
    job.source_type = source_type
    file, issue = files[0], issues[0]
    path = Path(file.file_path)
    title = "Superman" if case == "different_title" else "Batman"
    number = "2" if case == "different_number" else "1"
    web = (
        "<Web>https://comicvine.gamespot.com/issue/4000-999999/</Web>" if case == "wrong_id" else ""
    )
    if case == "approved":
        web = "<Web>https://comicvine.gamespot.com/issue/4000-100001/</Web>"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ComicInfo.xml",
            f"<ComicInfo><Series>{title}</Series><Number>{number}</Number>{web}</ComicInfo>",
        )
        archive.writestr("1.jpg", b"image")
        archive.writestr("2.jpg", b"image")
    root = LibraryRoot(name="Source", path=str(path.parent), enabled=True)
    db_session.add(root)
    await db_session.flush()
    signature = build_file_identity_signature(path)
    if source_type is ImportSourceType.MYLAR3:
        signature[MYLAR_REFERENCE_ROOT_ID_SIGNATURE_KEY] = root.id
    file.source_signature = {**signature, "device": int(signature["device"]) + 1}
    file.matched_issue_cv_id = issue.comicvine_id
    file.diagnostics = {
        "target_issue_summary": {"provider_id": "100001", "issue_number": 1},
        "completed_import_cleanup": {
            "action": "resolve_mixed_folder_files",
            "evidence_source": "comicinfo",
            "target_issue_id": issue.id,
            "target_series_id": item.series_id,
        },
    }
    item.diagnostics = {"kind": "completed_import_mixed_folder_recovery"}
    if case == "approved":
        item.diagnostics = {}
        file.diagnostics.pop("completed_import_cleanup")
        file.diagnostics["safety_exception"] = {
            "allowed_once": True,
            "previous_block": {
                "code": "archive_decompressed_size_limit",
                "overrideable": True,
            },
        }
        monkeypatch.setattr(
            "pullbox.services.import_recovery_source.get_archive_size_limit_bytes",
            AsyncMock(return_value=1),
        )
    await db_session.flush()
    before = path.read_bytes()
    if via_recheck and case != "approved":
        job.status = ImportJobStatus.COMPLETED
        file.status = ImportedFileStatus.FAILED
        file.error_message = (
            "Fresh ComicInfo does not prove the selected recovery issue. Review it in Follow-up."
        )
        file.diagnostics = {
            **file.diagnostics,
            "source_revalidation": {
                "code": "source_identity_changed",
                "category": "source_changed",
                "retryable": False,
                "source": "source_revalidation",
            },
        }
        await db_session.flush()
        report = await prepare_completed_import_file_recheck(
            db_session,
            job.id,
            source_roots=[path.parent],
            apply=True,
            accept_replaced_files=True,
        )
        assert report["files_checked"] == 1
        assert report["files_prepared"] == (1 if case == "mixed" else 0)
    if case in {"mixed", "approved"}:
        await refresh_recovery_source(db_session, job, item, file)
        assert file.source_signature == signature
    else:
        with pytest.raises(ReferencedFileValidationError):
            await refresh_recovery_source(db_session, job, item, file)
        assert file.source_signature != signature
    assert path.read_bytes() == before


@pytest.mark.parametrize("control", ["restart", "pause", "cancel"])
async def test_mixed_recovery_checkpoints_release_writer_and_resume_exact_scope(
    recovery_factory,  # noqa: F811
    monkeypatch,
    control,
):
    from sqlalchemy import update

    from pullbox.core.exceptions import JobCancelledError, JobPausedError
    from pullbox.models.import_job import ImportControlRequest, ImportedFile, ImportJob
    from pullbox.models.issue import Issue
    from pullbox.services.import_deferred_recovery_execution import cancel_deferred_preparation

    monkeypatch.setattr(
        "pullbox.services.import_mixed_recovery_execution.MIXED_RECOVERY_BATCH_SIZE", 1
    )
    async with recovery_factory() as session:
        job, source, target, first, issue = await _seed_mixed_folder_candidate(session)
        session.add(
            Issue(
                series_id=issue.series_id,
                issue_number=1003,
                issue_number_text="1003",
                comicvine_id=7001003,
            )
        )
        second = await add_file(
            session,
            job,
            source,
            file_path="/comics/Fritzi Ritz (1953)/second.cbz",
            file_name="Action Comics 1003.cbz",
            diagnostics={
                **first.diagnostics,
                "source_metadata": {
                    "comicinfo": {
                        "series": "Action Comics",
                        "number": "1003",
                        "year": 2026,
                    }
                },
            },
            comicvine_issue_id=None,
        )
        unrelated = await add_file(
            session,
            job,
            target,
            file_path="/comics/other.cbz",
            status=ImportedFileStatus.CONFIRMED,
            include_in_import=True,
        )
        action = CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES
        preview = await preview_completed_import_cleanup(session, job.id, action, actor_id=42)
        assert preview.affected_count == 2
        await apply_completed_import_cleanup(
            session,
            job.id,
            action,
            actor_id=42,
            preview_token=preview.preview_token,
            background=True,
        )
        job_id, first_id, second_id, unrelated_id, target_id = (
            job.id,
            first.id,
            second.id,
            unrelated.id,
            target.id,
        )
        await session.commit()

    async def interrupt(_event):
        # A separate writer must be able to save cancellation between batches.
        async with recovery_factory() as writer:
            await writer.execute(
                update(ImportJob)
                .where(ImportJob.id == job_id)
                .values(
                    control_request={
                        "restart": ImportControlRequest.NONE,
                        "pause": ImportControlRequest.PAUSE,
                        "cancel": ImportControlRequest.CANCEL,
                    }[control]
                )
            )
            await writer.commit()
        raise RuntimeError("worker interrupted after durable checkpoint")

    async with recovery_factory() as session:
        with pytest.raises(RuntimeError, match="worker interrupted"):
            await prepare_deferred_recovery(
                session, job_id, metadata_service=AsyncMock(), progress_callback=interrupt
            )

    async with recovery_factory() as session:
        job = await session.get(ImportJob, job_id)
        assert job.progress_snapshot["deferred_recovery"]["mixed_cursor"] == 1
        first = await session.get(ImportedFile, first_id)
        second = await session.get(ImportedFile, second_id)
        assert first.status is ImportedFileStatus.CONFIRMED
        assert second.status is ImportedFileStatus.NO_MATCH
        if control != "restart":
            with pytest.raises(JobPausedError if control == "pause" else JobCancelledError):
                await prepare_deferred_recovery(session, job_id, metadata_service=AsyncMock())
        if control == "cancel":
            await cancel_deferred_preparation(session, job)
            assert first.status is ImportedFileStatus.NO_MATCH
            assert second.status is ImportedFileStatus.NO_MATCH
        else:
            job.control_request = ImportControlRequest.NONE
            await session.commit()
            await prepare_deferred_recovery(session, job_id, metadata_service=AsyncMock())
            assert second.status is ImportedFileStatus.CONFIRMED
            state = job.progress_snapshot["deferred_recovery"]
            assert state["mixed_applied"] == 2
            assert target_id not in state["series_ids"]
            assert "mixed_resolutions" not in state
        unrelated = await session.get(ImportedFile, unrelated_id)
        assert unrelated.import_series_id == target_id
        assert unrelated.status is ImportedFileStatus.CONFIRMED
        assert unrelated.include_in_import


async def test_mixed_background_does_not_apply_changed_or_new_candidates(db_session):
    job, source, _, file, _ = await _seed_mixed_folder_candidate(db_session)
    action = CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES
    preview = await preview_completed_import_cleanup(db_session, job.id, action, actor_id=42)
    await apply_completed_import_cleanup(
        db_session,
        job.id,
        action,
        actor_id=42,
        preview_token=preview.preview_token,
        background=True,
    )
    await db_session.commit()
    extra = await add_file(
        db_session,
        job,
        source,
        file_path="/comics/extra.cbz",
        file_name="Action Comics 1002.cbz",
        diagnostics=file.diagnostics,
        comicvine_issue_id=None,
    )
    file.status = ImportedFileStatus.SKIPPED
    await db_session.commit()
    await prepare_deferred_recovery(db_session, job.id, metadata_service=AsyncMock())
    assert file.status is ImportedFileStatus.SKIPPED
    assert extra.status is ImportedFileStatus.NO_MATCH
    assert job.status is ImportJobStatus.COMPLETED
    assert job.progress_snapshot["deferred_recovery"]["mixed_changed"] == 1


async def test_mixed_registered_files_reuse_existing_group_without_importing_siblings(db_session):
    job, _, target, file, _ = await _seed_mixed_folder_candidate(db_session, with_library_file=True)
    sibling = await add_file(
        db_session,
        job,
        target,
        status=ImportedFileStatus.CONFIRMED,
        include_in_import=True,
        file_path="/comics/unrelated.cbz",
    )
    action = CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES
    preview = await preview_completed_import_cleanup(db_session, job.id, action, actor_id=42)
    await apply_completed_import_cleanup(
        db_session,
        job.id,
        action,
        actor_id=42,
        preview_token=preview.preview_token,
        background=True,
    )
    await db_session.commit()
    await prepare_deferred_recovery(db_session, job.id, metadata_service=AsyncMock())
    assert file.import_series_id == target.id
    assert file.status is ImportedFileStatus.ALREADY_OWNED
    assert sibling.status is ImportedFileStatus.CONFIRMED
    assert job.status is ImportJobStatus.COMPLETED
    assert job.progress_snapshot["deferred_recovery"]["series_ids"] == []


@pytest.mark.parametrize("failure", ["once", "always", "not_locked"])
async def test_mixed_recovery_database_lock_retries_only_uncommitted_batch(
    recovery_factory,  # noqa: F811
    monkeypatch,
    failure,
):
    from sqlalchemy.exc import OperationalError

    from pullbox.core.exceptions import JobPausedError
    from pullbox.services import import_mixed_recovery_execution as mixed
    from pullbox.services import import_recovery_checkpoint

    retry_delays: list[float] = []
    production_delay = import_recovery_checkpoint.sqlite_lock_retry_delay
    assert import_recovery_checkpoint.SQLITE_LOCK_RETRY_ATTEMPTS == 8

    def record_retry_delay(attempt: int) -> float:
        retry_delays.append(production_delay(attempt))
        return 0.0

    monkeypatch.setattr(import_recovery_checkpoint, "sqlite_lock_retry_delay", record_retry_delay)
    async with recovery_factory() as session:
        job, source, _, file, _ = await _seed_mixed_folder_candidate(session)
        action = CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES
        preview = await preview_completed_import_cleanup(session, job.id, action, actor_id=42)
        await apply_completed_import_cleanup(
            session,
            job.id,
            action,
            actor_id=42,
            preview_token=preview.preview_token,
            background=True,
        )
        await session.commit()
        original = mixed._apply_mixed_folder_resolutions
        attempts = 0

        async def locked(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            result = await original(*args, **kwargs)
            if failure != "once" or attempts == 1:
                raise OperationalError(
                    "UPDATE",
                    {},
                    RuntimeError(
                        "database is locked" if failure != "not_locked" else "no such table"
                    ),
                )
            return result

        monkeypatch.setattr(mixed, "_apply_mixed_folder_resolutions", locked)
        if failure == "once":
            await prepare_deferred_recovery(session, job.id, metadata_service=AsyncMock())
            assert attempts == 2
            assert file.status is ImportedFileStatus.CONFIRMED
            assert job.progress_snapshot["deferred_recovery"]["mixed_applied"] == 1
        else:
            with pytest.raises(JobPausedError if failure == "always" else OperationalError):
                await prepare_deferred_recovery(session, job.id, metadata_service=AsyncMock())
            await session.rollback()
            await session.refresh(file)
            await session.refresh(source)
            assert file.import_series_id == source.id
            assert file.status is ImportedFileStatus.NO_MATCH

        if failure == "always":
            assert attempts == 8
            assert retry_delays == [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75]
        elif failure == "once":
            assert retry_delays == [0.25]
        else:
            assert attempts == 1
            assert retry_delays == []


@pytest.mark.parametrize(
    "problem", ["replacement", "unsafe", "identity_conflict", "replacement_race"]
)
async def test_completed_recheck_does_not_extend_approval_to_changed_or_unsafe_sources(
    db_session, tmp_path, monkeypatch, problem
):
    from pullbox.core.library_file_ownership import build_file_identity_signature

    job, item, files = await _fixture(db_session, tmp_path, ImportSourceType.FILESYSTEM)
    job.status = ImportJobStatus.COMPLETED
    item.cv_id = 115251
    file = files[1]
    file.status = ImportedFileStatus.FAILED
    file.matched_issue_cv_id = 100008
    file.diagnostics = {
        **file.diagnostics,
        "target_issue_summary": {"provider_id": "100008", "issue_number": 8},
        "source_revalidation": {
            "code": "source_identity_changed",
            "category": "source_changed",
            "retryable": True,
            "source": "completed_import_recheck",
            "reason": (
                "The source changed or became unavailable after scanning. Rescan before retrying."
            ),
        },
        "safety_exception": {
            "allowed_once": True,
            "previous_block": {
                "code": "archive_decompressed_size_limit",
                "overrideable": True,
            },
        },
    }
    path = Path(file.file_path)
    if problem == "identity_conflict":
        file.diagnostics["source_revalidation"]["identity_conflicts"] = [
            {"field": "comicvine_issue_id", "source": 999}
        ]
    elif problem == "replacement_race":
        from pullbox.services import import_review_recheck

        inspect = import_review_recheck.inspect_review_source

        def replace_then_inspect(path, *args, **kwargs):
            with zipfile.ZipFile(path, "a") as archive:
                archive.writestr("extra.jpg", b"image")
            return inspect(path, *args, **kwargs)

        monkeypatch.setattr(import_review_recheck, "inspect_review_source", replace_then_inspect)
    else:
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("../unsafe.jpg" if problem == "unsafe" else "extra.jpg", b"image")
        if problem == "unsafe":
            file.source_signature = build_file_identity_signature(path)
    monkeypatch.setattr(
        "pullbox.services.import_review_recheck.get_archive_size_limit_bytes",
        AsyncMock(return_value=1),
    )
    await db_session.flush()
    report = await prepare_completed_import_file_recheck(
        db_session, job.id, source_roots=[tmp_path], apply=True, accept_replaced_files=True
    )
    assert report["files_prepared"] == 0
    assert file.status is ImportedFileStatus.FAILED
    if problem == "identity_conflict":
        assert report["files_checked"] == 0
    else:
        assert report["blocked_files"] == 1
