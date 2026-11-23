"""Recorded Mylar ownership must not override proven native identity conflicts."""

from pathlib import Path

import pytest
from sqlalchemy import select

from pullbox.core.metadata_identity import IdentityEvidenceKind, IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJobStatus,
)
from pullbox.models.library import LibraryFile
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.schemas.import_job import ConfirmImportRequest
from tests.integration.test_metron_folder_import import _archive
from tests.integration.test_mylar_native_extras import seed_mylar_native_extra


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
async def test_completed_mylar_conflict_links_only_current_verified_issue(
    db_session, tmp_path, namespace
):
    job, service, _, native, _, recorded, _, extra = await seed_mylar_native_extra(
        db_session, tmp_path, namespace
    )
    tracked = extra.parent / "Old Harbor 001.cbz"
    _archive(tracked, primary=namespace, number="1")
    await service.start_scan(db_session, job.id)
    file = await db_session.scalar(
        select(ImportedFile).where(ImportedFile.file_path == str(tracked))
    )
    group = file.import_series_id
    job.status = ImportJobStatus.COMPLETED
    await db_session.commit()
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


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("in_place", [False, True])
async def test_recorded_mylar_rechecks_native_links_before_placement(
    db_session, tmp_path, namespace, in_place
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
    ) = await seed_mylar_native_extra(db_session, tmp_path, namespace, in_place)
    tracked = extra.parent / "Old Harbor 001.cbz"
    originals[tracked] = _archive(
        tracked, primary=namespace, number="1", series_id="22", issue_id="102"
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
    link.external_id = "102"
    await db_session.commit()
    await service.run_import(db_session, job.id)
    await db_session.refresh(file)
    assert file.status == ImportedFileStatus.FAILED
    assert "Mylar" in file.error_message and "unchanged" in file.error_message
    await db_session.refresh(recorded_issue)
    assert (
        await db_session.scalar(
            select(LibraryFile.id).where(LibraryFile.issue_id == recorded_issue.id)
        )
        is None
    )
    assert all(path.read_bytes() == content for path, content in originals.items())
