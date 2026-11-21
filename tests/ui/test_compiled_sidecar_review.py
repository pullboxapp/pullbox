"""A series-only sidecar leaves per-file issue decisions explicit and actionable."""

import json

import pytest
from sqlalchemy import select

from pullbox.core.metadata_identity import IdentityEvidenceKind, IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.models.library import LibraryFile
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.schemas.import_job import ConfirmImportRequest, ImportJobCreate
from tests.integration.test_compiled_sidecar_import import _comic, _sidecar
from tests.integration.test_metron_folder_import import _service, _setup
from tests.ui.test_import_review_file_routes import csrf, token

pytest_plugins = ["conftest_security"]


@pytest.mark.parametrize(
    "namespace,extra_link",
    [
        (IdentityNamespace.METRON, False),
        (IdentityNamespace.GCD, False),
        (IdentityNamespace.METRON, True),
        (IdentityNamespace.GCD, True),
    ],
)
@pytest.mark.parametrize(
    "change",
    [None, "series_link", "issue_link", "evidence", "execute_series_link", "execute_issue_link"],
)
async def test_series_sidecar_requires_explicit_issue_choice(
    authenticated_client, sec_db, tmp_path, namespace, extra_link, change
):
    async with sec_db() as session:
        root, series, issue = await _setup(
            session, tmp_path, number="13a", namespace=namespace, default_destination=False
        )
        folder = tmp_path / "source" / "Unsorted"
        sidecar = _sidecar(folder, namespace)
        if extra_link:
            other = (
                IdentityNamespace.GCD
                if namespace == IdentityNamespace.METRON
                else IdentityNamespace.METRON
            )
            session.add(
                SeriesExternalIdentity(
                    series_id=series.id,
                    identity_namespace=other,
                    external_id="22",
                    verification_state=IdentityVerificationState.VERIFIED,
                    evidence_kind=IdentityEvidenceKind.PROVIDER_RESULT,
                )
            )
            await session.commit()
            data = json.loads(sidecar)
            data["pullbox"]["snapshot"]["identities"].append(
                {"namespace": other.value, "entity_kind": "series", "external_id": "22"}
            )
            sidecar = json.dumps(data).encode()
            (folder / "series.json").write_bytes(sidecar)
        comic = _comic(folder, namespace, tagged=False)
        original = comic.read_bytes()
        (folder / "neighbor.cbz").write_bytes(original)
        service = _service()
        job = await service.create_job(
            session,
            ImportJobCreate(
                source_path=str(folder.parent),
                source_type="filesystem",
                target_library_root_id=root.id,
            ),
        )
        await service.start_scan(session, job.id)
        file = await session.scalar(
            select(ImportedFile).where(ImportedFile.file_path == str(comic))
        )
        parent = await session.get(ImportedSeries, file.import_series_id)
        assert parent.series_id == series.id
        assert file.status not in {ImportedFileStatus.MATCHED, ImportedFileStatus.CONFIRMED}
        assert not file.include_in_import and file.matched_issue_id is None
        job_id, parent_id, file_id, issue_id = job.id, parent.id, file.id, issue.id
    url = f"/import/{job_id}/files/{file_id}/assign-native"
    preview = await authenticated_client.get(url + "?q=13a")
    assert preview.status_code == 200, preview.text
    assert "Use this" in preview.text
    assert 'disabled title="The embedded identity' not in preview.text
    assert "series.json identifies the series, not this individual issue" in preview.text
    early_change = change in {"series_link", "issue_link", "evidence"}
    if early_change:
        async with sec_db() as session:
            if change == "evidence":
                file = await session.get(ImportedFile, file_id)
                metadata = dict(file.diagnostics["source_metadata"])
                metadata["metron_import"] = {**metadata["metron_import"], "number": "13b"}
                file.diagnostics = {**file.diagnostics, "source_metadata": metadata}
            else:
                model = SeriesExternalIdentity if change == "series_link" else IssueExternalIdentity
                link = (
                    await session.scalars(
                        select(model).where(model.identity_namespace == namespace)
                    )
                ).one()
                link.verification_state = IdentityVerificationState.STALE
            await session.commit()
    response = await authenticated_client.post(
        url,
        data={"token": token(preview.text), "issue_id": issue_id},
        headers=csrf(authenticated_client),
    )
    if early_change:
        assert response.status_code == 422, response.text
        assert comic.read_bytes() == original
        assert (folder / "series.json").read_bytes() == sidecar
        return
    assert response.status_code == 200, response.text
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        assert file.match_method == "library_review"
        assert file.matched_issue_id == issue_id
        file.status = ImportedFileStatus.PENDING
        await session.commit()
        await service.rematch_imported_series_files(session, job_id, parent_id)
        await session.refresh(file)
        assert file.status == ImportedFileStatus.MATCHED
        neighbor = await session.scalar(
            select(ImportedFile).where(ImportedFile.file_name == "neighbor.cbz")
        )
        assert neighbor.matched_issue_id is None and not neighbor.include_in_import
        await service.update_file_selection(session, job_id, file_id, include_in_import=True)
        await service.confirm_import(session, job_id, ConfirmImportRequest(series_ids=[parent_id]))
        if change in {"execute_series_link", "execute_issue_link"}:
            model = (
                SeriesExternalIdentity if change == "execute_series_link" else IssueExternalIdentity
            )
            link = (
                await session.scalars(select(model).where(model.identity_namespace == namespace))
            ).one()
            link.verification_state = IdentityVerificationState.STALE
            await session.commit()
        await service.run_import(session, job_id)
        await session.refresh(file)
        if change:
            assert file.status == ImportedFileStatus.FAILED
            assert not (await session.scalars(select(LibraryFile))).all()
        else:
            assert file.status == ImportedFileStatus.IMPORTED, file.error_message
            assert await service.rollback_import(session, job_id)
    assert comic.read_bytes() == original
    assert (folder / "neighbor.cbz").read_bytes() == original
    assert (folder / "series.json").read_bytes() == sidecar


async def test_invalid_sidecar_explains_rescan_and_keeps_skip_available(
    authenticated_client, sec_db, tmp_path
):
    async with sec_db() as session:
        root, _, _ = await _setup(session, tmp_path, default_destination=False)
        folder = tmp_path / "source" / "Unsorted"
        data = json.loads(_sidecar(folder, IdentityNamespace.METRON))
        data["pullbox"]["schema_version"] = 2
        (folder / "series.json").write_text(json.dumps(data))
        _comic(folder, IdentityNamespace.METRON)
        service = _service()
        job = await service.create_job(
            session,
            ImportJobCreate(
                source_path=str(folder.parent),
                source_type="filesystem",
                target_library_root_id=root.id,
            ),
        )
        await service.start_scan(session, job.id)
        job_id = job.id
    response = await authenticated_client.get(
        f"/import/{job_id}/review-partial?status=needs_decision"
    )
    assert response.status_code == 200
    assert "series.json needs review" in response.text
    assert "Correct the source sidecar and rescan" in response.text
    assert "Skip series" in response.text
    assert "Preview Metron series" not in response.text
    assert "Recheck linked metadata" not in response.text
