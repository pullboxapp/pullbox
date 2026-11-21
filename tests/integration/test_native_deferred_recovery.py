"""Completed imports recover exact native identities without changing source evidence."""

from copy import deepcopy
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJobStatus,
)
from pullbox.models.issue import IssueStatus
from pullbox.models.library import LibraryFile
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.models.series import IssueCatalogState
from pullbox.models.user import User
from pullbox.schemas.import_job import ImportJobCreate
from pullbox.services.import_completed_cleanup import (
    CompletedImportCleanupAction,
    apply_completed_import_cleanup,
    preview_completed_import_cleanup,
)
from pullbox.services.import_deferred_recovery import (
    apply_deferred_recovery,
    plan_deferred_recovery,
)
from pullbox.services.import_deferred_recovery_execution import (
    cancel_deferred_preparation,
    prepare_deferred_recovery,
)
from tests.integration.test_compiled_sidecar_import import _comic, _sidecar
from tests.integration.test_metron_folder_import import _archive, _service, _setup
from tests.unit.test_import_deferred_recovery import register


async def _deferred(session, root, namespace, *, sidecar=False, stale_issue=False):
    destination, series, issue = await _setup(session, root, number="13a", namespace=namespace)
    issue.status = IssueStatus.SKIPPED
    issue.release_date = date(2024, 1, 1)
    series.issue_catalog_state = IssueCatalogState.COMPLETE
    link = (
        await session.scalars(
            select(IssueExternalIdentity if stale_issue else SeriesExternalIdentity)
        )
    ).one()
    link.verification_state = IdentityVerificationState.STALE
    session.add(User(id=42, username="native-recovery", password_hash="unused"))
    await session.commit()
    folder = root / "source" / "Unsorted"
    if sidecar:
        _sidecar(folder, namespace)
        comic = _comic(folder, namespace)
    else:
        comic = folder / "Harbor Lights 013a (2024).cbz"
        _archive(comic, number="13a", primary="Metron" if namespace.value == "metron" else "GCD")
    service = _service()
    job = await service.create_job(
        session,
        ImportJobCreate(
            source_path=str(folder.parent),
            source_type="filesystem",
            target_library_root_id=destination.id,
        ),
    )
    await service.start_scan(session, job.id)
    service._metadata_service.reset_mock()
    file = (await session.scalars(select(ImportedFile))).one()
    parent = await session.get(ImportedSeries, file.import_series_id)
    assert file.status == ImportedFileStatus.NO_MATCH
    # Retain the unresolved scan exactly as a completed import's Follow-up does.
    job.status = ImportJobStatus.COMPLETED
    job.import_completed_at = datetime.now(UTC)
    link.verification_state = IdentityVerificationState.VERIFIED
    await session.commit()
    return service, job, parent, file, issue, comic


@pytest.mark.parametrize("namespace", [IdentityNamespace.METRON, IdentityNamespace.GCD])
@pytest.mark.parametrize("sidecar", [False, True])
async def test_completed_native_import_rechecks_exact_links_then_imports_and_rolls_back(
    db_session, tmp_path, namespace, sidecar
):
    service, job, _parent, file, issue, comic = await _deferred(
        db_session, tmp_path, namespace, sidecar=sidecar
    )
    original = comic.read_bytes()
    evidence = deepcopy(file.diagnostics["source_metadata"])
    plans = await plan_deferred_recovery(db_session, job.id)
    assert [(plan.file_id, plan.action, plan.issue_id) for plan in plans] == [
        (file.id, "exact_target", issue.id)
    ]
    assert not db_session.dirty
    action = CompletedImportCleanupAction.RECHECK_DEFERRED_FILES
    preview = await preview_completed_import_cleanup(db_session, job.id, action, actor_id=42)
    result = await apply_completed_import_cleanup(
        db_session, job.id, action, actor_id=42, preview_token=preview.preview_token
    )
    assert result.requires_import_retry
    await db_session.commit()
    assert await prepare_deferred_recovery(
        db_session, job.id, metadata_service=service._metadata_service
    )
    # Execution must work from persisted preparation, not an in-memory identity cache.
    await db_session.commit()
    db_session.expire_all()
    await db_session.refresh(job)
    await db_session.refresh(file)
    await db_session.refresh(issue)
    await service.run_import(db_session, job.id)
    await db_session.refresh(file)
    assert file.status == ImportedFileStatus.IMPORTED
    assert file.match_method == f"{namespace.value}_id"
    assert file.diagnostics["source_metadata"] == evidence
    assert job.total_files_imported == 1 and job.series_imported == 0
    library = (await db_session.scalars(select(LibraryFile))).one()
    assert library.issue_id == issue.id
    assert comic.read_bytes() == original
    assert await plan_deferred_recovery(db_session, job.id) == ()
    assert await service.rollback_import(db_session, job.id)
    await db_session.refresh(issue)
    assert issue.status == IssueStatus.SKIPPED
    assert not (await db_session.scalars(select(LibraryFile))).all()
    assert comic.read_bytes() == original
    assert service._metadata_service.mock_calls == []


@pytest.mark.parametrize("ownership", ["same_path", "other_path", "changed_signature"])
async def test_native_recovery_preserves_owned_files(db_session, tmp_path, ownership):
    _service_instance, job, _parent, file, issue, comic = await _deferred(
        db_session, tmp_path, IdentityNamespace.GCD
    )
    from pullbox.models.library import LibraryRoot

    root = await db_session.get(LibraryRoot, job.target_library_root_id)
    library = await register(db_session, file, issue, root)
    if ownership == "other_path":
        library.file_path = str(tmp_path / "owned.cbz")
    elif ownership == "changed_signature":
        library.source_signature = {**library.source_signature, "mtime_ns": 1}
    await db_session.commit()
    before = (library.file_path, deepcopy(library.source_signature), comic.read_bytes())
    plans = await plan_deferred_recovery(db_session, job.id)
    if ownership == "changed_signature":
        assert plans == ()
    else:
        assert [(plan.file_id, plan.action) for plan in plans] == [
            (file.id, "already_registered" if ownership == "same_path" else "owned_variant")
        ]
        await apply_deferred_recovery(db_session, job)
        assert file.status == (
            ImportedFileStatus.ALREADY_OWNED
            if ownership == "same_path"
            else ImportedFileStatus.CONFLICT
        )
    assert not file.include_in_import
    assert (library.file_path, library.source_signature, comic.read_bytes()) == before


async def test_native_recovery_keeps_two_physical_copies_for_review(db_session, tmp_path):
    _service_instance, job, parent, file, _issue, _comic_path = await _deferred(
        db_session, tmp_path, IdentityNamespace.METRON
    )
    from tests.unit.test_import_deferred_recovery import add_file

    other = await add_file(
        db_session,
        job,
        parent,
        file_path=str(tmp_path / "another.cbz"),
        comicvine_issue_id=None,
        diagnostics=deepcopy(file.diagnostics),
        source_signature=deepcopy(file.source_signature),
    )
    await db_session.commit()
    assert await plan_deferred_recovery(db_session, job.id) == ()
    assert file.status == other.status == ImportedFileStatus.NO_MATCH


async def test_unverified_native_issue_never_falls_back_to_filename_or_live_catalog(
    db_session, tmp_path
):
    service, job, _parent, file, _issue, comic = await _deferred(
        db_session, tmp_path, IdentityNamespace.METRON, stale_issue=True
    )
    (
        await db_session.scalars(select(IssueExternalIdentity))
    ).one().verification_state = IdentityVerificationState.STALE
    await db_session.commit()
    before = (file.status, deepcopy(file.diagnostics), comic.read_bytes())
    assert await plan_deferred_recovery(db_session, job.id) == ()
    action = CompletedImportCleanupAction.RECHECK_DEFERRED_FILES
    preview = await preview_completed_import_cleanup(db_session, job.id, action, actor_id=42)
    await apply_completed_import_cleanup(
        db_session, job.id, action, actor_id=42, preview_token=preview.preview_token
    )
    await db_session.commit()
    assert await prepare_deferred_recovery(
        db_session, job.id, metadata_service=service._metadata_service
    )
    assert (file.status, file.diagnostics, comic.read_bytes()) == before
    assert service._metadata_service.mock_calls == []


@pytest.mark.parametrize("defect", ["series_stale", "issue_stale", "number", "malformed", "skip"])
async def test_native_recheck_never_guesses_or_overrides_decisions(db_session, tmp_path, defect):
    service, job, _parent, file, _issue, comic = await _deferred(
        db_session, tmp_path, IdentityNamespace.METRON
    )
    if defect.endswith("stale"):
        model = SeriesExternalIdentity if defect == "series_stale" else IssueExternalIdentity
        (
            await db_session.scalars(select(model))
        ).one().verification_state = IdentityVerificationState.STALE
    elif defect in {"number", "malformed"}:
        diagnostics = deepcopy(file.diagnostics)
        evidence = diagnostics["source_metadata"]["metron_import"]
        if defect == "number":
            evidence["number"] = "13b"
        else:
            evidence["problems"] = ["invalid_xml"]
        file.diagnostics = diagnostics
    else:
        file.status = ImportedFileStatus.SKIPPED
    await db_session.commit()
    before = (file.status, file.import_series_id, deepcopy(file.diagnostics), comic.read_bytes())
    assert await plan_deferred_recovery(db_session, job.id) == ()
    await apply_deferred_recovery(db_session, job)
    assert (file.status, file.import_series_id, file.diagnostics, comic.read_bytes()) == before
    assert service._metadata_service.mock_calls == []


@pytest.mark.parametrize("change", ["series_link", "issue_link", "source"])
async def test_native_recovery_revalidates_before_file_placement(db_session, tmp_path, change):
    service, job, _parent, file, _issue, comic = await _deferred(
        db_session, tmp_path, IdentityNamespace.METRON
    )
    counts = await apply_deferred_recovery(db_session, job)
    assert counts["exact_target"] == 1
    if change == "source":
        _archive(comic, number="99")
    else:
        model = SeriesExternalIdentity if change == "series_link" else IssueExternalIdentity
        (
            await db_session.scalars(select(model))
        ).one().verification_state = IdentityVerificationState.STALE
    original = comic.read_bytes()
    job.status = ImportJobStatus.IMPORTING
    job.progress_snapshot = {
        **job.progress_snapshot,
        "deferred_recovery": {**job.progress_snapshot["deferred_recovery"], "state": "prepared"},
    }
    await db_session.commit()
    await service.run_import(db_session, job.id)
    await db_session.refresh(file)
    assert file.status == ImportedFileStatus.FAILED
    assert not (await db_session.scalars(select(LibraryFile))).all()
    assert comic.read_bytes() == original


async def test_cancelled_native_recheck_can_resume_without_undoing_prior_imports(
    db_session, tmp_path
):
    _service_instance, job, _parent, file, issue, comic = await _deferred(
        db_session, tmp_path, IdentityNamespace.GCD
    )
    before = (deepcopy(file.diagnostics["source_metadata"]), comic.read_bytes())
    assert (await apply_deferred_recovery(db_session, job))["exact_target"] == 1
    job.progress_snapshot = {
        **job.progress_snapshot,
        "deferred_recovery": {**job.progress_snapshot["deferred_recovery"], "state": "prepared"},
    }
    assert await cancel_deferred_preparation(db_session, job)
    assert job.status == ImportJobStatus.COMPLETED
    assert file.status == ImportedFileStatus.NO_MATCH and not file.include_in_import
    assert (file.diagnostics["source_metadata"], comic.read_bytes()) == before
    plans = await plan_deferred_recovery(db_session, job.id)
    assert [(plan.file_id, plan.action, plan.issue_id) for plan in plans] == [
        (file.id, "exact_target", issue.id)
    ]
