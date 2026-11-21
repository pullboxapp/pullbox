"""Exact GCD review shortcuts reuse source policy and the regular Add workflow."""

import hashlib
import os
import sys
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from pullbox.config import get_settings
from pullbox.core.events import EventBus
from pullbox.models import Issue, Series
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.schemas.import_job import ConfirmImportRequest, ImportJobCreate
from tests.api.test_gcd_local import activate, gcd_dump
from tests.api.test_metadata_sources_api import csrf
from tests.integration.test_metron_folder_import import _archive, _service

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest_plugins = ["conftest_security"]


async def review(session, tmp_path):
    destination = tmp_path / "library"
    destination.mkdir()
    root = LibraryRoot(name="GCD test", path=str(destination), allow_managed_writes=True)
    session.add(root)
    await session.commit()
    source = tmp_path / "incoming"
    comic = source / "Unsorted" / "test.cbz"
    original = _archive(comic, primary="GCD", series_id="2999", issue_id="11", number="13b")
    service = _service()
    job = await service.create_job(
        session,
        ImportJobCreate(
            source_type="filesystem", source_path=str(source), target_library_root_id=root.id
        ),
    )
    await service.start_scan(session, job.id)
    file = (await session.scalars(select(ImportedFile))).one()
    assert file.status == ImportedFileStatus.NO_MATCH and not file.include_in_import
    await session.commit()
    return service, job.id, file.import_series_id, file.id, root.id, comic, original


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("local", "gcd_local"),
        ("api_flagged", "gcd_local"),
        ("api", "gcd_api_v2"),
        ("domain", "gcd_local"),
        ("api_unconfigured", "gcd_local"),
        ("disabled", None),
        ("unconfigured", None),
    ],
)
async def test_gcd_import_preview_selects_enabled_source_by_priority_without_fetching(
    authenticated_client, sec_app, sec_db, tmp_path, mode, expected, monkeypatch
):
    from pullbox.api.deps import get_settings_dep

    if mode != "unconfigured":
        assert (
            await activate(authenticated_client, gcd_dump(tmp_path / "gcd.db"))
        ).status_code == 200
    settings = get_settings().model_copy(
        update={"metadata_gcd_api_v2_enabled": mode in {"api", "domain", "api_unconfigured"}}
    )
    sec_app.dependency_overrides[get_settings_dep] = lambda: settings
    async with sec_db() as session:
        if mode in {"api", "api_flagged", "domain", "api_unconfigured"}:
            from pullbox.core.encryption import encrypt_secret

            session.add(
                MetadataSourceConfig(
                    source="gcd_api_v2",
                    enabled=True,
                    priority=1,
                    credential_secret=None
                    if mode == "api_unconfigured"
                    else encrypt_secret("synthetic-token"),
                )
            )
        local = await session.scalar(
            select(MetadataSourceConfig).where(MetadataSourceConfig.source == "gcd_local")
        )
        if mode == "domain":
            local.domain_priorities = {"core": 0}
        if mode == "disabled":
            local.enabled = False
        await session.commit()
        _, job_id, group_id, file_id, root_id, _, _ = await review(session, tmp_path)

    def no_client(*_args, **_kwargs):
        pytest.fail("Preview context must not construct a provider client")

    monkeypatch.setattr("pullbox.providers.metadata.sources.GcdLocalSource", no_client)
    monkeypatch.setattr("pullbox.providers.metadata.sources.GcdApiV2Source", no_client)
    response = await authenticated_client.get(
        f"/import/{job_id}/series/{group_id}/identity-preview"
    )
    assert response.status_code == (200 if expected else 409), response.text
    if expected:
        assert response.json()["source"] == expected
        assert response.json()["external_id"] == "2999"
        assert response.json()["library_root_id"] == root_id
        assert "no-store" in response.headers["cache-control"]
    else:
        assert "Metadata settings" in response.json()["detail"]
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        assert file.status == ImportedFileStatus.NO_MATCH and not file.include_in_import
        assert await session.scalar(select(func.count()).select_from(Series)) == 0


@pytest.mark.parametrize("blocked", ["busy", "skipped", "mixed", "other_job", "mylar", "invalid"])
async def test_gcd_import_preview_rejects_changed_scope_and_ambiguous_identity(
    authenticated_client, sec_db, tmp_path, blocked
):
    from pullbox.models.import_job import (
        ImportJob,
        ImportJobStatus,
        ImportSeriesStatus,
        ImportSourceType,
    )

    async with sec_db() as session:
        _, job_id, group_id, file_id, _, _, _ = await review(session, tmp_path)
        job = await session.get(ImportJob, job_id)
        group = await session.get(ImportedSeries, group_id)
        if blocked == "busy":
            job.status = ImportJobStatus.IMPORTING
        elif blocked == "skipped":
            group.status = ImportSeriesStatus.SKIPPED
        elif blocked in {"mixed", "invalid"}:
            group.diagnostics = {
                "metron_import": {
                    "identities": [
                        {"namespace": "gcd", "id": value}
                        for value in (["2999", "3001"] if blocked == "mixed" else ["../1"])
                    ]
                }
            }
        elif blocked == "mylar":
            job.source_type = ImportSourceType.MYLAR3
        await session.commit()
    response = await authenticated_client.get(
        f"/import/{job_id + (blocked == 'other_job')}/series/{group_id}/identity-preview"
    )
    assert response.status_code == (404 if blocked == "other_job" else 409), response.text
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        assert file.status == ImportedFileStatus.NO_MATCH and not file.include_in_import
        assert await session.scalar(select(func.count()).select_from(Series)) == 0


async def test_gcd_import_preview_add_recheck_import_and_rollback(
    authenticated_client, sec_db, tmp_path, monkeypatch
):
    dump = gcd_dump(tmp_path / "gcd.db")
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    assert (await activate(authenticated_client, dump)).status_code == 200
    monkeypatch.setattr("pullbox.api.v1.series.get_event_bus", EventBus)
    async with sec_db() as session:
        service, job_id, group_id, file_id, _, comic, original = await review(session, tmp_path)
    context = await authenticated_client.get(f"/import/{job_id}/series/{group_id}/identity-preview")
    assert context.status_code == 200, context.text
    body = {key: context.json()[key] for key in ("source", "external_id", "library_root_id")}
    preview = await authenticated_client.post(
        "/api/v1/metadata/series/preview", json=body, headers=csrf(authenticated_client)
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["series"]["data"]["title"] == "Swamp Thing"
    body["source_revision"] = preview.json()["source_revision"]
    added = await authenticated_client.post(
        "/api/v1/series", json=body, headers=csrf(authenticated_client)
    )
    assert added.status_code == 201, added.text
    series_id = added.json()["id"]
    trigger = Mock()
    monkeypatch.setattr("pullbox.tasks.import_task.trigger_import_series_rematch", trigger)
    result = await authenticated_client.post(
        f"/import/{job_id}/series/{group_id}/identity-recheck", headers=csrf(authenticated_client)
    )
    assert result.status_code == 202, result.text
    trigger.assert_called_once_with(job_id, group_id)
    async with sec_db() as session:
        await service.rematch_imported_series_files(session, job_id, group_id)
        file = await session.get(ImportedFile, file_id)
        group = await session.get(ImportedSeries, group_id)
        assert file.status == ImportedFileStatus.MATCHED and not file.include_in_import
        assert group.series_id == series_id and file.match_method == "gcd_id"
        await service.update_file_selection(session, job_id, file_id, include_in_import=True)
        await service.confirm_import(session, job_id, ConfirmImportRequest(series_ids=[group_id]))
        await service.run_import(session, job_id)
        await session.refresh(file)
        assert file.status == ImportedFileStatus.IMPORTED
        assert await service.rollback_import(session, job_id)
        assert await session.get(Series, series_id) is not None
        assert await session.scalar(select(func.count()).select_from(Issue)) == 3
        assert await session.scalar(select(LibraryFile.id)) is None
    assert comic.read_bytes() == original
    assert hashlib.sha256(dump.read_bytes()).hexdigest() == digest
