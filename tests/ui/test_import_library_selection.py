"""Explicit library choices let untagged files use native catalogs safely."""

import zipfile
from copy import deepcopy

import pytest
from sqlalchemy import select

from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.models.metadata_identity import IssueExternalIdentity
from pullbox.schemas.import_job import ConfirmImportRequest, ImportJobCreate
from tests.integration.test_metron_folder_import import _service, _setup
from tests.ui.test_import_review_file_routes import csrf, token

pytest_plugins = ["conftest_security"]


async def seed_library_choice(
    session, root, namespace="metron", identity_base=0, admit=True, xml=None, sibling=False
):
    destination, series, issue = await _setup(
        session,
        root,
        namespace=IdentityNamespace(namespace),
        number="13a",
        series_external_id=str(identity_base + 21),
        issue_external_id=str(identity_base + 101),
        default_destination=False,
    )
    comic = root / "source" / "Unsorted" / "unidentified.cbz"
    comic.parent.mkdir(parents=True)
    with zipfile.ZipFile(comic, "w") as archive:
        archive.writestr("page001.jpg", b"page one")
        archive.writestr("page002.jpg", b"page two")
        if xml is not None:
            archive.writestr("ComicInfo.xml", xml)
    original = comic.read_bytes()
    if sibling:
        (comic.parent / "Unidentified 13a.cbz").write_bytes(original)
    service = _service()
    request = ImportJobCreate(
        source_path=str(root / "source"),
        source_type="filesystem",
        target_library_root_id=destination.id,
    )
    if admit:
        job = await service.create_job(session, request)
    else:
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
        await session.scalars(
            select(ImportedFile).where(
                ImportedFile.import_job_id == job.id, ImportedFile.file_path == str(comic)
            )
        )
    ).one()
    return job.id, file.id, series.id, issue.id, comic, original, service


async def choose_series(client, job_id, parent_id, series_id):
    url = f"/import/{job_id}/series/{parent_id}/review-library"
    preview = await client.get(url + "?q=Harbor")
    assert preview.status_code == 200, preview.text
    result = await client.post(
        url,
        data={"token": token(preview.text), "library_series_id": series_id},
        headers=csrf(client),
    )
    assert result.status_code == 200, result.text


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
@pytest.mark.parametrize("xml", [None, "<ComicInfo><Number>13a</Number></ComicInfo>"])
async def test_untagged_file_uses_explicit_library_choices_and_preserves_rollback(
    authenticated_client, sec_db, tmp_path, namespace, xml
):
    async with sec_db() as session:
        job_id, file_id, series_id, issue_id, comic, original, service = await seed_library_choice(
            session, tmp_path, namespace, xml=xml, sibling=True
        )
        file = await session.get(ImportedFile, file_id)
        parent_id = file.import_series_id
        evidence = deepcopy(file.diagnostics["source_metadata"])
    await choose_series(authenticated_client, job_id, parent_id, series_id)
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        parent = await session.get(ImportedSeries, parent_id)
        assert parent.series_id == series_id
        assert file.status == ImportedFileStatus.NO_MATCH and not file.include_in_import
        assert "metron_import" not in parent.diagnostics
    url = f"/import/{job_id}/files/{file_id}/assign-native"
    preview = await authenticated_client.get(url + "?q=13a")
    assert preview.status_code == 200, preview.text
    applied = await authenticated_client.post(
        url,
        data={"token": token(preview.text), "issue_id": issue_id},
        headers=csrf(authenticated_client),
    )
    assert applied.status_code == 200, applied.text
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        assert file.matched_issue_id == issue_id
        assert file.match_method == "library_review"
        assert file.diagnostics["source_metadata"] == evidence
        # Explicit choices survive rechecking; unchosen neighbors are never inferred.
        file.status = ImportedFileStatus.PENDING
        await session.commit()
        await service.rematch_imported_series_files(session, job_id, parent_id)
        await session.refresh(file)
        assert file.status == ImportedFileStatus.MATCHED
        assert file.matched_issue_id == issue_id
        neighbor = await session.scalar(
            select(ImportedFile).where(
                ImportedFile.import_series_id == parent_id, ImportedFile.id != file_id
            )
        )
        assert neighbor is not None
        assert neighbor.status == ImportedFileStatus.NO_MATCH
        assert neighbor.matched_issue_id is None
        assert not neighbor.include_in_import
        await service.update_file_selection(session, job_id, file_id, include_in_import=True)
        await service.confirm_import(session, job_id, ConfirmImportRequest(series_ids=[parent_id]))
        await service.run_import(session, job_id)
        await session.refresh(file)
        assert file.status == ImportedFileStatus.IMPORTED, file.error_message
        assert await service.rollback_import(session, job_id)
    assert comic.read_bytes() == original
    assert (comic.parent / "Unidentified 13a.cbz").read_bytes() == original
    service._metadata_service._provider.get_series.assert_not_awaited()
    service._metadata_service._provider.get_issues_for_series.assert_not_awaited()


@pytest.mark.parametrize(
    "xml",
    [
        "<ComicInfo><Number>13b</Number></ComicInfo>",
        "<ComicInfo><Number>13a</Number><Number>13b</Number></ComicInfo>",
        "<ComicInfo><Web>https://comicvine.gamespot.com/issue/4000-999/</Web></ComicInfo>",
        "<ComicInfo><Number>13a",
    ],
)
async def test_explicit_library_choice_cannot_override_xml(
    authenticated_client, sec_db, tmp_path, xml
):
    async with sec_db() as session:
        job_id, file_id, series_id, issue_id, comic, original, _ = await seed_library_choice(
            session, tmp_path, xml=xml
        )
        parent_id = (await session.get(ImportedFile, file_id)).import_series_id
    await choose_series(authenticated_client, job_id, parent_id, series_id)
    url = f"/import/{job_id}/files/{file_id}/assign-native"
    preview = await authenticated_client.get(url)
    assert preview.status_code == 200
    assert 'disabled title="The embedded identity' in preview.text
    result = await authenticated_client.post(
        url,
        data={"token": token(preview.text), "issue_id": issue_id},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 422
    assert comic.read_bytes() == original


@pytest.mark.parametrize("change", ["source", "actor", "series_link", "busy", "other_series"])
async def test_library_series_preview_is_bound_to_current_review(
    authenticated_client, sec_db, tmp_path, change
):
    from pullbox.models.import_job import ImportJob, ImportJobStatus
    from pullbox.models.metadata_identity import SeriesExternalIdentity
    from pullbox.services.import_review_scope import sign_review_scope

    async with sec_db() as session:
        job_id, file_id, series_id, _, comic, original, _ = await seed_library_choice(
            session, tmp_path
        )
        parent_id = (await session.get(ImportedFile, file_id)).import_series_id
    url = f"/import/{job_id}/series/{parent_id}/review-library"
    preview = await authenticated_client.get(url)
    data = {"token": token(preview.text), "library_series_id": series_id}
    assert (await authenticated_client.post(url, data=data)).status_code == 403
    async with sec_db() as session:
        if change == "source":
            file = await session.get(ImportedFile, file_id)
            file.source_signature = {"changed": True}
        elif change == "actor":
            data["token"] = await sign_review_scope(session, {"actor": 999})
        elif change == "series_link":
            link = await session.scalar(
                select(SeriesExternalIdentity).where(SeriesExternalIdentity.series_id == series_id)
            )
            link.verification_state = IdentityVerificationState.STALE
        elif change == "busy":
            job = await session.get(ImportJob, job_id)
            job.status = ImportJobStatus.IMPORTING
        else:
            data["library_series_id"] = 999
        await session.commit()
    result = await authenticated_client.post(url, data=data, headers=csrf(authenticated_client))
    assert result.status_code == 422, result.text
    async with sec_db() as session:
        assert (await session.get(ImportedSeries, parent_id)).series_id is None
    assert comic.read_bytes() == original


async def test_changed_native_link_after_manual_choice_blocks_placement(
    authenticated_client, sec_db, tmp_path
):
    async with sec_db() as session:
        job_id, file_id, series_id, issue_id, comic, original, service = await seed_library_choice(
            session, tmp_path
        )
        parent_id = (await session.get(ImportedFile, file_id)).import_series_id
    await choose_series(authenticated_client, job_id, parent_id, series_id)
    url = f"/import/{job_id}/files/{file_id}/assign-native"
    preview = await authenticated_client.get(url)
    result = await authenticated_client.post(
        url,
        data={"token": token(preview.text), "issue_id": issue_id},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 200, result.text
    async with sec_db() as session:
        await service.confirm_import(session, job_id, ConfirmImportRequest(series_ids=[parent_id]))
        link = await session.scalar(
            select(IssueExternalIdentity).where(IssueExternalIdentity.issue_id == issue_id)
        )
        link.verification_state = IdentityVerificationState.STALE
        await session.commit()
        await service.run_import(session, job_id)
        file = await session.get(ImportedFile, file_id)
        assert file.status == ImportedFileStatus.FAILED
        assert "changed" in file.error_message.lower()
    assert comic.read_bytes() == original


async def test_library_series_choices_page_only_verified_native_series(
    authenticated_client, sec_db, tmp_path
):
    from pullbox.core.metadata_identity import IdentityEvidenceKind
    from pullbox.models.metadata_identity import SeriesExternalIdentity
    from pullbox.models.series import Series

    async with sec_db() as session:
        job_id, file_id, _, _, _, _, _ = await seed_library_choice(session, tmp_path)
        parent_id = (await session.get(ImportedFile, file_id)).import_series_id
        for index in range(28):
            series = Series(title=f"Catalog {index:02}", sort_title=f"Catalog {index:02}")
            session.add(series)
            await session.flush()
            session.add(
                SeriesExternalIdentity(
                    series_id=series.id,
                    identity_namespace=(
                        IdentityNamespace.COMICVINE if index == 27 else IdentityNamespace.GCD
                    ),
                    external_id=str(500 + index),
                    verification_state=(
                        IdentityVerificationState.STALE
                        if index == 26
                        else IdentityVerificationState.VERIFIED
                    ),
                    evidence_kind=IdentityEvidenceKind.PROVIDER_RESULT,
                )
            )
        await session.commit()
    url = f"/import/{job_id}/series/{parent_id}/review-library?q=Catalog"
    first = await authenticated_client.get(url)
    assert first.status_code == 200
    assert first.text.count('data-testid="import-library-series-use"') == 25
    assert "Catalog 00" in first.text and "Catalog 24" in first.text
    assert "Catalog 25" not in first.text
    second = await authenticated_client.get(url + "&page=2")
    assert second.status_code == 200
    assert second.text.count('data-testid="import-library-series-use"') == 1
    assert "Catalog 25" in second.text
    assert "Catalog 26" not in second.text and "Catalog 27" not in second.text
    clamped = await authenticated_client.get(url + "&page=999")
    assert clamped.status_code == 200 and "2 of 2" in clamped.text
    literal = await authenticated_client.get(url + "%25")
    assert literal.status_code == 200 and "No verified Metron or GCD series found" in literal.text
