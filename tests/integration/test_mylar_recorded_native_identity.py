"""Recorded Mylar ownership must not override proven native identity conflicts."""

from pathlib import Path

import pytest
from sqlalchemy import func, select

from pullbox.core.exceptions import ValidationError
from pullbox.core.metadata_identity import IdentityEvidenceKind, IdentityNamespace
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
from pullbox.schemas.import_job import (
    ConfirmImportRequest,
    OrphanRecoveryDecision,
    RecoverOrphanRequest,
)
from tests.integration.test_metron_folder_import import _archive
from tests.integration.test_mylar_native_extras import seed_mylar_native_extra


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
async def test_completed_mylar_conflict_links_only_current_verified_issue(
    db_session, tmp_path, namespace
):
    job, service, _, native, _, recorded, originals, extra = await seed_mylar_native_extra(
        db_session, tmp_path, namespace
    )
    tracked = extra.parent / "Old Harbor 001.cbz"
    originals[tracked] = _archive(tracked, primary=namespace, number="1")
    await service.start_scan(db_session, job.id)
    file = await db_session.scalar(
        select(ImportedFile).where(ImportedFile.file_path == str(tracked))
    )
    group = file.import_series_id
    neighbor = await db_session.scalar(
        select(ImportedFile).where(ImportedFile.file_path == str(extra))
    )
    await service.update_file_selection(db_session, job.id, neighbor.id, include_in_import=True)
    await service.confirm_import(
        db_session, job.id, ConfirmImportRequest(series_ids=[neighbor.import_series_id])
    )
    await service.run_import(db_session, job.id)
    await db_session.refresh(job)
    assert job.status == ImportJobStatus.COMPLETED
    placed = (await db_session.scalars(select(LibraryFile))).one()
    assert placed.issue_id == native.id
    destination = Path(placed.file_path)
    neighbor_bytes = destination.read_bytes()
    context = await service.get_orphan_recovery_context(db_session, group)
    [row] = context["files"]
    assert row["recorded_issue_id"] == recorded.id
    assert row["provider_issues"] == [{"provider": namespace, "library_issue_id": native.id}]
    link = await db_session.scalar(
        select(IssueExternalIdentity).where(IssueExternalIdentity.issue_id == native.id)
    )
    link.verification_state = IdentityVerificationState.STALE
    await db_session.commit()
    context = await service.get_orphan_recovery_context(db_session, group)
    assert context["files"][0]["provider_issues"] == []
    assert context["files"][0]["recorded_issue_id"] == recorded.id
    result = await service.recover_orphan(
        db_session,
        group,
        RecoverOrphanRequest(
            decisions=[OrphanRecoveryDecision(imported_file_id=file.id, action="skip")]
        ),
    )
    assert (result["skipped_count"], result["files_remaining"], result["imported_count"]) == (
        1,
        0,
        0,
    )
    await db_session.commit()
    await db_session.refresh(file)
    assert file.status == ImportedFileStatus.SKIPPED and file.matched_issue_id is None
    assert (await db_session.scalars(select(LibraryFile.issue_id))).all() == [native.id]
    assert destination.read_bytes() == neighbor_bytes
    assert await service.rollback_import(db_session, job.id)
    assert not destination.exists()
    assert await db_session.scalar(select(LibraryFile.id)) is None
    assert all(path.read_bytes() == content for path, content in originals.items())


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("in_place", [False, True])
@pytest.mark.parametrize("primary_comicvine", [False, True])
async def test_recorded_mylar_file_blocks_known_foreign_native_identity(
    db_session, tmp_path, namespace, in_place, primary_comicvine
):
    (
        job,
        service,
        _native,
        native_issue,
        legacy,
        _recorded_issue,
        originals,
        extra,
    ) = await seed_mylar_native_extra(db_session, tmp_path, namespace, in_place)
    tracked = extra.parent / "Old Harbor 001.cbz"
    originals[tracked] = (
        _archive(
            tracked,
            primary="ComicVine",
            number="1",
            series_id="900",
            issue_id="901",
            extra_ids=f'<ID source="{namespace}">101</ID>',
        )
        if primary_comicvine
        else _archive(tracked, primary=namespace, number="1")
    )
    await service.start_scan(db_session, job.id)
    files = {
        Path(file.file_path): file
        for file in (await db_session.scalars(select(ImportedFile))).all()
    }
    file = files[tracked]
    parent = await db_session.get(ImportedSeries, file.import_series_id)
    assert parent.series_id == legacy.id
    assert parent.cv_id == 900
    assert file.status == ImportedFileStatus.NO_MATCH
    assert file.matched_issue_id is None
    assert not file.include_in_import
    assert file.diagnostics["conflict_type"] == "mylar_native_identity_conflict"
    assert files[extra].matched_issue_id == native_issue.id
    assert all(path.read_bytes() == content for path, content in originals.items())
    for method in ("search_series", "get_series", "get_issues_for_series"):
        getattr(service._metadata_service._provider, method).assert_not_awaited()


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("in_place", [False, True])
@pytest.mark.parametrize("identity_state", ["agree", "unknown", "stale", "foreign_stale"])
async def test_recorded_mylar_match_preserved_without_proven_native_disagreement(
    db_session, tmp_path, namespace, in_place, identity_state
):
    job, service, _, _, legacy, issue, originals, extra = await seed_mylar_native_extra(
        db_session, tmp_path, namespace, in_place
    )
    tracked = extra.parent / "Old Harbor 001.cbz"
    originals[tracked] = _archive(
        tracked, primary=namespace, number="1", series_id="22", issue_id="102"
    )
    if identity_state == "foreign_stale":
        originals[tracked] = _archive(tracked, primary=namespace, number="1")
        for model in (SeriesExternalIdentity, IssueExternalIdentity):
            link = await db_session.scalar(select(model))
            link.verification_state = IdentityVerificationState.STALE
        await db_session.commit()
    elif identity_state != "unknown":
        state = (
            IdentityVerificationState.VERIFIED
            if identity_state == "agree"
            else IdentityVerificationState.STALE
        )
        db_session.add_all(
            [
                SeriesExternalIdentity(
                    series_id=legacy.id,
                    identity_namespace=IdentityNamespace(namespace),
                    external_id="22",
                    verification_state=state,
                    evidence_kind=IdentityEvidenceKind.PROVIDER_RESULT,
                ),
                IssueExternalIdentity(
                    issue_id=issue.id,
                    identity_namespace=IdentityNamespace(namespace),
                    external_id="102",
                    verification_state=state,
                    evidence_kind=IdentityEvidenceKind.PROVIDER_RESULT,
                ),
            ]
        )
        await db_session.commit()
    await service.start_scan(db_session, job.id)
    file = await db_session.scalar(
        select(ImportedFile).where(ImportedFile.file_path == str(tracked))
    )
    assert file.status == ImportedFileStatus.MATCHED
    assert file.matched_issue_id == issue.id
    assert file.diagnostics["source_metadata"]["metron_import"]["identities"]
    await service.update_file_selection(db_session, job.id, file.id, include_in_import=True)
    await service.confirm_import(
        db_session, job.id, ConfirmImportRequest(series_ids=[file.import_series_id])
    )
    await service.run_import(db_session, job.id)
    await db_session.refresh(file)
    assert file.status == ImportedFileStatus.IMPORTED, file.error_message
    assert await db_session.scalar(select(LibraryFile.id).where(LibraryFile.issue_id == issue.id))
    assert await service.rollback_import(db_session, job.id)
    assert all(path.read_bytes() == content for path, content in originals.items())
    for method in ("search_series", "get_series", "get_issues_for_series"):
        getattr(service._metadata_service._provider, method).assert_not_awaited()


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
async def test_recorded_mylar_known_issue_conflict_within_same_series(
    db_session, tmp_path, namespace
):
    job, service, _, native_issue, legacy, _, _, extra = await seed_mylar_native_extra(
        db_session, tmp_path, namespace
    )
    native_issue.series_id = legacy.id
    series_link = await db_session.scalar(
        select(SeriesExternalIdentity).where(SeriesExternalIdentity.external_id == "21")
    )
    series_link.series_id = legacy.id
    await db_session.commit()
    tracked = extra.parent / "Old Harbor 001.cbz"
    _archive(tracked, primary=namespace, number="1")
    await service.start_scan(db_session, job.id)
    file = await db_session.scalar(
        select(ImportedFile).where(ImportedFile.file_path == str(tracked))
    )
    assert file.status == ImportedFileStatus.NO_MATCH
    assert file.diagnostics["conflict_type"] == "mylar_native_identity_conflict"


async def run_late_mylar_conflict(
    db_session, tmp_path, namespace, in_place, *, identity_base=0, admit=True
):
    (
        job,
        service,
        _,
        native_issue,
        _,
        recorded_issue,
        originals,
        extra,
    ) = await seed_mylar_native_extra(
        db_session, tmp_path, namespace, in_place, identity_base=identity_base, admit=admit
    )
    tracked = extra.parent / "Old Harbor 001.cbz"
    originals[tracked] = _archive(
        tracked,
        primary=namespace,
        number="1",
        series_id=str(identity_base + 22),
        issue_id=str(identity_base + 102),
    )
    await service.start_scan(db_session, job.id)
    file = await db_session.scalar(
        select(ImportedFile).where(ImportedFile.file_path == str(tracked))
    )
    assert file.status == ImportedFileStatus.MATCHED
    await service.update_file_selection(db_session, job.id, file.id, include_in_import=True)
    await service.confirm_import(
        db_session, job.id, ConfirmImportRequest(series_ids=[file.import_series_id])
    )
    link = await db_session.scalar(
        select(IssueExternalIdentity).where(IssueExternalIdentity.issue_id == native_issue.id)
    )
    link.external_id = str(identity_base + 102)
    await db_session.commit()
    await service.run_import(db_session, job.id)
    await db_session.commit()
    await db_session.refresh(file)
    return job, service, file, native_issue, recorded_issue, originals


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("in_place", [False, True])
async def test_recorded_mylar_rechecks_native_links_before_placement(
    db_session, tmp_path, namespace, in_place
):
    job, service, file, native_issue, recorded_issue, originals = await run_late_mylar_conflict(
        db_session, tmp_path, namespace, in_place
    )
    assert file.status == ImportedFileStatus.NO_MATCH
    assert "Mylar" in file.error_message and "unchanged" in file.error_message
    assert file.diagnostics["conflict_type"] == "mylar_native_identity_conflict"
    assert not file.include_in_import and not file.diagnostics["review_selection"]
    assert file.matched_issue_id is None and file.matched_issue_cv_id is None
    group_id = file.import_series_id
    visible, _ = await service.get_orphaned_series(db_session, job_id=job.id)
    assert group_id in {item.id for item in visible}
    await db_session.refresh(job)
    assert job.total_files_failed == 0
    assert job.total_files_no_match == await db_session.scalar(
        select(func.count(ImportedFile.id)).where(
            ImportedFile.import_job_id == job.id, ImportedFile.status == ImportedFileStatus.NO_MATCH
        )
    )
    context = await service.get_orphan_recovery_context(db_session, group_id)
    assert context["mylar_conflicts_only"]
    [row] = context["files"]
    assert row["recorded_issue_id"] == recorded_issue.id
    assert row["provider_issues"] == [{"provider": namespace, "library_issue_id": native_issue.id}]
    with pytest.raises(ValidationError, match="cannot be assigned"):
        await service.recover_orphan(
            db_session,
            group_id,
            RecoverOrphanRequest(
                decisions=[
                    OrphanRecoveryDecision(
                        imported_file_id=file.id, action="assign", issue_cv_id=901
                    )
                ]
            ),
        )
    result = await service.recover_orphan(
        db_session,
        group_id,
        RecoverOrphanRequest(
            decisions=[OrphanRecoveryDecision(imported_file_id=file.id, action="skip")]
        ),
    )
    assert result["skipped_count"] == 1 and result["files_remaining"] == 0
    await db_session.commit()
    await db_session.refresh(file)
    assert file.status == ImportedFileStatus.SKIPPED
    await db_session.refresh(recorded_issue)
    await db_session.refresh(native_issue)
    assert (recorded_issue.status, native_issue.status) == (IssueStatus.WANTED, IssueStatus.SKIPPED)
    visible, _ = await service.get_orphaned_series(db_session, job_id=job.id)
    assert group_id not in {item.id for item in visible}
    assert (
        await db_session.scalar(
            select(LibraryFile.id).where(LibraryFile.issue_id == recorded_issue.id)
        )
        is None
    )
    assert all(path.read_bytes() == content for path, content in originals.items())
