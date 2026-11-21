"""Native issue choices use saved catalogs and retain import safety boundaries."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from pullbox.core.metadata_identity import IdentityEvidenceKind, IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import ImportedFile, ImportedFileStatus
from pullbox.models.issue import Issue
from pullbox.models.metadata_identity import IssueExternalIdentity
from pullbox.schemas.import_job import ConfirmImportRequest, ImportJobCreate
from tests.integration.test_metron_folder_import import _archive, _service, _setup
from tests.ui.test_import_review_file_routes import csrf, token

pytest_plugins = ["conftest_security"]


async def seed_native_choices(session, root, namespace="metron", identity_base=0, admit=True):
    destination, series, issue = await _setup(
        session,
        root,
        namespace=IdentityNamespace(namespace),
        number="13a",
        series_external_id=str(21 + identity_base),
        issue_external_id=str(101 + identity_base),
        default_destination=False,
    )
    comic = root / "source" / "Unsorted" / "unidentified.cbz"
    original = _archive(
        comic,
        number="13a",
        primary=namespace,
        issue_id=str(999 + identity_base),
        series_id=str(21 + identity_base),
    )
    service = _service()
    request = ImportJobCreate(
        source_path=str(root / "source"),
        source_type="filesystem",
        target_library_root_id=destination.id,
    )
    if admit:
        job = await service.create_job(session, request)
    else:
        # Browser fixtures share a catalog with unrelated seeded review jobs.
        from pullbox.models.import_job import ImportJob

        job = ImportJob(
            source_path=request.source_path,
            source_type=request.source_type,
            target_library_root_id=destination.id,
        )
        session.add(job)
        await session.commit()
    await service.start_scan(session, job.id)
    file = (
        await session.scalars(select(ImportedFile).where(ImportedFile.import_job_id == job.id))
    ).one()
    assert file.status == ImportedFileStatus.NO_MATCH
    # The catalog is corrected after scanning; the user can now choose this issue.
    link = (
        await session.scalars(
            select(IssueExternalIdentity).where(IssueExternalIdentity.issue_id == issue.id)
        )
    ).one()
    link.external_id = str(999 + identity_base)
    for number in range(1, 32):
        extra = Issue(series_id=series.id, issue_number=number, title=f"Chapter {number}")
        session.add(extra)
        await session.flush()
        session.add(
            IssueExternalIdentity(
                issue_id=extra.id,
                identity_namespace=IdentityNamespace(namespace),
                external_id=str(1000 + number + identity_base),
                verification_state=IdentityVerificationState.VERIFIED,
                evidence_kind=IdentityEvidenceKind.PROVIDER_RESULT,
            )
        )
    await session.commit()
    return job.id, file.id, issue.id, comic, original, service


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
async def test_native_picker_pages_selects_and_imports_without_comicvine(
    authenticated_client, sec_db, tmp_path, namespace, monkeypatch
):
    from unittest.mock import AsyncMock

    from pullbox.ui import import_review_file_routes as routes

    provider = AsyncMock(side_effect=AssertionError("Saved catalog must not call providers"))
    monkeypatch.setattr(routes, "build_metadata_service", provider)
    async with sec_db() as session:
        job_id, file_id, issue_id, comic, original, service = await seed_native_choices(
            session, tmp_path, namespace
        )
        file = await session.get(ImportedFile, file_id)
        evidence = deepcopy(file.diagnostics["source_metadata"])
    url = f"/import/{job_id}/files/{file_id}/assign-native"
    preview = await authenticated_client.get(url)
    assert preview.status_code == 200, preview.text
    assert preview.text.count('data-testid="import-review-use-issue"') == 25
    assert 'data-visible-rows="10"' in preview.text
    assert 'name="cv_id"' not in preview.text
    page2 = await authenticated_client.get(url + "?issue_page=2")
    assert page2.text.count('data-testid="import-review-use-issue"') == 7
    selected = await authenticated_client.get(url + "?q=13a")
    assert selected.text.count('data-testid="import-review-use-issue"') == 1
    data = {"token": token(selected.text), "issue_id": issue_id}
    assert (await authenticated_client.post(url, data=data)).status_code == 403
    applied = await authenticated_client.post(url, data=data, headers=csrf(authenticated_client))
    assert applied.status_code == 200, applied.text
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        assert file.status == ImportedFileStatus.MATCHED
        assert file.matched_issue_id == issue_id and file.matched_issue_cv_id is None
        assert file.diagnostics["source_metadata"] == evidence
        await service.update_file_selection(session, job_id, file_id, include_in_import=True)
        await service.confirm_import(
            session, job_id, ConfirmImportRequest(series_ids=[file.import_series_id])
        )
        await service.run_import(session, job_id)
        await session.refresh(file)
        assert file.status == ImportedFileStatus.IMPORTED
        assert await service.rollback_import(session, job_id)
    assert comic.read_bytes() == original
    provider.assert_not_awaited()


@pytest.mark.parametrize("owned", [False, True])
async def test_native_choice_preserves_owned_files_and_exposes_competing_copies(
    authenticated_client, sec_db, tmp_path, owned
):
    from pullbox.models.import_job import ImportJob
    from pullbox.models.library import LibraryFile

    async with sec_db() as session:
        job_id, file_id, issue_id, comic, original, _ = await seed_native_choices(session, tmp_path)
        file = await session.get(ImportedFile, file_id)
        neighbor = ImportedFile(
            import_job_id=job_id,
            import_series_id=file.import_series_id,
            file_path=str(tmp_path / "other.cbz"),
            file_name="other.cbz",
            file_format="cbz",
            matched_issue_id=issue_id,
            status=ImportedFileStatus.MATCHED,
            include_in_import=True,
        )
        session.add(neighbor)
        if owned:
            session.add(
                LibraryFile(
                    issue_id=issue_id,
                    library_root_id=(await session.get(ImportJob, job_id)).target_library_root_id,
                    file_path=str(comic),
                    file_name=comic.name,
                    file_format="cbz",
                    file_size=len(original),
                    file_modified_at=datetime.now(UTC),
                )
            )
        await session.commit()
        neighbor_id = neighbor.id
    url = f"/import/{job_id}/files/{file_id}/assign-native"
    preview = await authenticated_client.get(url + "?q=13a")
    result = await authenticated_client.post(
        url,
        data={"token": token(preview.text), "issue_id": issue_id},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 200, result.text
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        neighbor = await session.get(ImportedFile, neighbor_id)
        assert not file.include_in_import
        if owned:
            assert file.status == ImportedFileStatus.ALREADY_OWNED
            assert neighbor.status == ImportedFileStatus.MATCHED
        else:
            assert file.status == neighbor.status == ImportedFileStatus.CONFLICT
            assert file.conflict_group_id == neighbor.conflict_group_id
            assert not neighbor.include_in_import
    assert comic.read_bytes() == original


async def test_native_choice_rechecks_links_again_before_placement(
    authenticated_client, sec_db, tmp_path
):
    async with sec_db() as session:
        job_id, file_id, issue_id, comic, original, service = await seed_native_choices(
            session, tmp_path
        )
    url = f"/import/{job_id}/files/{file_id}/assign-native"
    preview = await authenticated_client.get(url + "?q=13a")
    result = await authenticated_client.post(
        url,
        data={"token": token(preview.text), "issue_id": issue_id},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 200
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        await service.confirm_import(
            session, job_id, ConfirmImportRequest(series_ids=[file.import_series_id])
        )
        link = await session.scalar(
            select(IssueExternalIdentity).where(IssueExternalIdentity.issue_id == issue_id)
        )
        link.verification_state = IdentityVerificationState.STALE
        await session.commit()
        await service.run_import(session, job_id)
        await session.refresh(file)
        assert file.status == ImportedFileStatus.FAILED
        assert "provider series or issue link changed" in file.error_message
    assert comic.read_bytes() == original


async def test_native_catalog_displays_but_never_allows_conflicting_issue_choices(
    authenticated_client, sec_db, tmp_path
):
    async with sec_db() as session:
        job_id, file_id, issue_id, comic, original, _ = await seed_native_choices(session, tmp_path)
    url = f"/import/{job_id}/files/{file_id}/assign-native"
    wrong = await authenticated_client.get(url + "?q=Chapter%201")
    assert wrong.status_code == 200
    assert 'disabled title="The embedded identity' in wrong.text
    result = await authenticated_client.post(
        url,
        data={"token": token(wrong.text), "issue_id": issue_id + 1},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 422
    assert "do not agree" in result.text
    assert comic.read_bytes() == original


async def test_native_file_cannot_bypass_identity_checks_through_legacy_picker(
    authenticated_client, sec_db, tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pullbox.ui import import_review_file_routes as routes

    metadata = AsyncMock()
    metadata.get_series_metadata.return_value = SimpleNamespace(provider_id="20", title="Other")
    metadata.get_issue_summaries_for_series.return_value = []
    monkeypatch.setattr(routes, "build_metadata_service", AsyncMock(return_value=metadata))
    async with sec_db() as session:
        job_id, file_id, _, _, _, _ = await seed_native_choices(session, tmp_path)
    result = await authenticated_client.get(
        f"/import/{job_id}/files/{file_id}/assign?cv_id=20&inline=true"
    )
    assert result.status_code == 422
    metadata.get_series_metadata.assert_not_awaited()


@pytest.mark.parametrize(
    "change", ["issue_link", "series_link", "number", "source", "safety", "busy", "other_issue"]
)
async def test_native_picker_rejects_changed_or_contradictory_evidence(
    authenticated_client, sec_db, tmp_path, change
):
    from pullbox.models.import_job import ImportJob, ImportJobStatus
    from pullbox.models.metadata_identity import SeriesExternalIdentity

    async with sec_db() as session:
        job_id, file_id, issue_id, comic, original, _ = await seed_native_choices(session, tmp_path)
    url = f"/import/{job_id}/files/{file_id}/assign-native"
    preview = await authenticated_client.get(url + "?q=13a")
    assert preview.status_code == 200
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        if change == "issue_link":
            link = await session.scalar(
                select(IssueExternalIdentity).where(IssueExternalIdentity.issue_id == issue_id)
            )
            link.verification_state = IdentityVerificationState.STALE
        elif change == "series_link":
            link = (await session.scalars(select(SeriesExternalIdentity))).one()
            link.verification_state = IdentityVerificationState.STALE
        elif change == "number":
            issue = await session.get(Issue, issue_id)
            issue.issue_number_text = "13b"
        elif change == "source":
            file.source_signature = {"changed": True}
        elif change == "safety":
            file.status = ImportedFileStatus.SAFETY_BLOCKED
        elif change == "busy":
            job = await session.get(ImportJob, job_id)
            job.status = ImportJobStatus.IMPORTING
        await session.commit()
    applied = await authenticated_client.post(
        url,
        data={
            "token": token(preview.text),
            "issue_id": issue_id + 1 if change == "other_issue" else issue_id,
        },
        headers=csrf(authenticated_client),
    )
    assert applied.status_code == 422, applied.text
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        assert file.matched_issue_id is None
    assert comic.read_bytes() == original
