"""Completed-import series choices retain explicit, provider-neutral file recovery."""

from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from pullbox.core.metadata_identity import MetadataSource
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.library import LibraryFile
from tests.ui.test_import_library_search import discovery
from tests.ui.test_import_library_selection import seed_library_choice
from tests.ui.test_import_review_file_routes import csrf, token

pytest_plugins = ["conftest_security"]


async def completed_group(session, root, namespace="metron", **kwargs):
    values = await seed_library_choice(session, root, namespace=namespace, **kwargs)
    job_id, file_id, *_ = values
    job = await session.get(ImportJob, job_id)
    file = await session.get(ImportedFile, file_id)
    parent = await session.get(ImportedSeries, file.import_series_id)
    assert parent.series_id is None and file.status == ImportedFileStatus.NO_MATCH
    job.status = ImportJobStatus.COMPLETED
    job.import_completed_at = datetime.now(UTC)
    parent.status = ImportSeriesStatus.NO_MATCH
    if not kwargs.get("admit", True):
        from pullbox.core.library_policy import load_library_ingest_policy
        from pullbox.services.import_policy_snapshot import apply_ingest_policy_to_import_job

        apply_ingest_policy_to_import_job(job, await load_library_ingest_policy(session))
    await session.commit()
    values[-1]._metadata_service.reset_mock()
    return values, parent.id


@pytest.mark.parametrize("namespace", ["metron", "gcd"])
async def test_completed_group_can_choose_native_series_and_recover(
    authenticated_client, sec_db, tmp_path, monkeypatch, namespace
):
    async with sec_db() as session:
        values, parent_id = await completed_group(session, tmp_path, namespace)
        job_id, file_id, series_id, issue_id, comic, original, service = values
        evidence = deepcopy((await session.get(ImportedFile, file_id)).diagnostics)
    monkeypatch.setattr(
        "pullbox.composition.services.build_import_service", AsyncMock(return_value=service)
    )
    url = f"/import/{job_id}/series/{parent_id}/review-library?follow_up=true"
    preview = await authenticated_client.get(url)
    assert preview.status_code == 200, preview.text
    assert 'name="follow_up" value="true"' in preview.text
    assert "Search Metron / GCD" in preview.text
    assert "Search ComicVine" in preview.text
    body = {"token": token(preview.text), "library_series_id": series_id}
    denied = await authenticated_client.post(url, data=body)
    assert denied.status_code == 403
    response = await authenticated_client.post(url, data=body, headers=csrf(authenticated_client))
    assert response.status_code == 200, response.text
    async with sec_db() as session:
        parent = await session.get(ImportedSeries, parent_id)
        file = await session.get(ImportedFile, file_id)
        assert parent.series_id == series_id
        assert parent.status == ImportSeriesStatus.RECOVERY_PENDING
        assert not file.include_in_import and file.matched_issue_id is None
        assert file.diagnostics == evidence
        assert not (await session.scalars(select(LibraryFile))).all()
    choices = await authenticated_client.get(
        f"/import/{job_id}/files/{file_id}/assign-native?follow_up=true"
    )
    assert choices.status_code == 200, choices.text
    result = await authenticated_client.post(
        f"/api/v1/import/orphaned/{parent_id}/recover",
        headers=csrf(authenticated_client),
        json={
            "decisions": [
                {
                    "imported_file_id": file_id,
                    "action": "assign",
                    "library_issue_id": issue_id,
                    "review_token": token(choices.text),
                }
            ]
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["imported_count"] == 1 and result.json()["files_remaining"] == 0
    assert comic.read_bytes() == original
    async with sec_db() as session:
        assert (await session.scalars(select(LibraryFile))).one().issue_id == issue_id
        assert await service.rollback_import(session, job_id)
        assert not (await session.scalars(select(LibraryFile))).all()
    assert comic.read_bytes() == original
    assert service._metadata_service.mock_calls == []


@pytest.mark.parametrize(
    "change",
    [
        "token",
        "actor",
        "link",
        "file",
        "imported",
        "archived",
        "revision",
        "busy",
        "mylar",
        "source_check",
        "rematch",
        "rollback",
    ],
)
async def test_follow_up_series_choice_revalidates_before_changing_anything(
    authenticated_client, sec_db, tmp_path, change
):
    from pullbox.core.exceptions import ValidationError
    from pullbox.core.metadata_identity_state import IdentityVerificationState
    from pullbox.models.import_job import ImportControlRequest, ImportSourceType
    from pullbox.models.metadata_identity import SeriesExternalIdentity
    from pullbox.models.series import Series
    from pullbox.services.import_library_selection import (
        choose_library_series,
        library_selection_context,
        library_series_identity,
    )
    from pullbox.services.import_review_scope import sign_review_scope

    async with sec_db() as session:
        values, parent_id = await completed_group(session, tmp_path)
        job_id, file_id, series_id, _, comic, original, _ = values
        job, parent, scope = await library_selection_context(
            session, job_id, parent_id, 42, follow_up=True
        )
        series = await session.get(Series, series_id)
        signed = await sign_review_scope(
            session,
            {
                **scope,
                "target": await library_series_identity(session, series),
                "title": series.title,
                "year": series.year_start,
            },
        )
        file = await session.get(ImportedFile, file_id)
        if change == "token":
            signed = "invalid"
        elif change == "link":
            (
                await session.scalars(select(SeriesExternalIdentity))
            ).one().verification_state = IdentityVerificationState.STALE
        elif change == "file":
            file.include_in_import = True
        elif change == "imported":
            file.status = ImportedFileStatus.IMPORTED
        elif change == "archived":
            job.archived_at = datetime.now(UTC)
        elif change == "revision":
            job.progress_revision += 1
        elif change == "busy":
            job.control_request = ImportControlRequest.CANCEL
        elif change == "mylar":
            job.source_type = ImportSourceType.MYLAR3
        elif change == "source_check":
            file.diagnostics = {**file.diagnostics, "review_source_action": {"state": "pending"}}
        elif change == "rematch":
            parent.diagnostics = {**parent.diagnostics, "rematch_pending": True}
        elif change == "rollback":
            job.status = ImportJobStatus.ROLLED_BACK
        await session.commit()
        before = (
            parent.status,
            deepcopy(parent.diagnostics),
            file.status,
            deepcopy(file.diagnostics),
        )
        with pytest.raises(ValidationError):
            await choose_library_series(
                session,
                job_id,
                parent_id,
                series_id,
                43 if change == "actor" else 42,
                signed,
                follow_up=True,
            )
        assert parent.series_id is None
        assert (parent.status, parent.diagnostics, file.status, file.diagnostics) == before
        assert comic.read_bytes() == original
        assert not (await session.scalars(select(LibraryFile))).all()


@pytest.mark.parametrize("matches", [True, False])
async def test_follow_up_cannot_override_known_native_series_evidence(
    authenticated_client, sec_db, tmp_path, matches
):
    from pullbox.core.metadata_identity import IdentityNamespace
    from tests.integration.test_native_orphan_recovery import _follow_up

    async with sec_db() as session:
        _, job, parent, file, series, _, comic = await _follow_up(
            session, tmp_path, IdentityNamespace.GCD
        )
        parent.series_id = None
        if not matches:
            parent.diagnostics = {
                **parent.diagnostics,
                "metron_import": {
                    **parent.diagnostics["metron_import"],
                    "identities": [{"namespace": "gcd", "id": "99999"}],
                },
            }
        await session.commit()
        original = comic.read_bytes()
        job_id, parent_id, series_id, file_id = job.id, parent.id, series.id, file.id
    url = f"/import/{job_id}/series/{parent_id}/review-library?follow_up=true"
    preview = await authenticated_client.get(url)
    assert preview.status_code == 200, preview.text
    assert "Search ComicVine" not in preview.text
    result = await authenticated_client.post(
        url,
        data={"token": token(preview.text), "library_series_id": series_id},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == (200 if matches else 422), result.text
    async with sec_db() as session:
        parent = await session.get(ImportedSeries, parent_id)
        file = await session.get(ImportedFile, file_id)
        assert parent.series_id == (series_id if matches else None)
        assert file.matched_issue_id is None and not file.include_in_import
        assert not (await session.scalars(select(LibraryFile))).all()
    assert comic.read_bytes() == original


async def test_find_series_keeps_mylar_on_existing_comicvine_workflow(
    authenticated_client, sec_db, tmp_path, monkeypatch
):
    from starlette.responses import HTMLResponse

    from pullbox.models.import_job import ImportSourceType

    legacy = AsyncMock(return_value=HTMLResponse("Existing ComicVine search"))
    monkeypatch.setattr("pullbox.ui.import_orphaned_routes.import_orphaned_cv_search", legacy)
    async with sec_db() as session:
        values, parent_id = await completed_group(session, tmp_path)
        job_id = values[0]
    url = f"/import/orphaned/{parent_id}/find-series"
    response = await authenticated_client.get(url)
    assert response.status_code == 200, response.text
    assert "Choose a library series" in response.text
    legacy.assert_not_awaited()
    async with sec_db() as session:
        (await session.get(ImportJob, job_id)).source_type = ImportSourceType.MYLAR3
        await session.commit()
    response = await authenticated_client.get(url)
    assert response.status_code == 200
    assert response.text == "Existing ComicVine search"
    legacy.assert_awaited_once()


async def test_follow_up_search_rechecks_completion_after_provider_io(
    authenticated_client, sec_db, tmp_path, monkeypatch
):
    async with sec_db() as session:
        values, parent_id = await completed_group(session, tmp_path)
        job_id = values[0]

    async def archive_during_search(*args, **kwargs):
        async with sec_db() as session:
            (await session.get(ImportJob, job_id)).archived_at = datetime.now(UTC)
            await session.commit()
        return discovery()

    monkeypatch.setattr("pullbox.ui.series_routes.search_snapshot", archive_during_search)
    response = await authenticated_client.get(
        f"/import/{job_id}/series/{parent_id}/review-library-search?follow_up=true&q=Harbor"
    )
    assert response.status_code == 422
    assert 'name="token"' not in response.text


@pytest.mark.parametrize("source", [MetadataSource.METRON_API, MetadataSource.GCD_LOCAL])
async def test_follow_up_search_reuses_discovery_only_on_request(
    authenticated_client, sec_db, tmp_path, monkeypatch, source
):
    search = AsyncMock(return_value=discovery(source))
    monkeypatch.setattr("pullbox.ui.series_routes.search_snapshot", search)
    async with sec_db() as session:
        values, parent_id = await completed_group(
            session, tmp_path, source.identity_namespace.value
        )
        job_id, file_id, series_id, _, comic, original, _ = values
    url = f"/import/{job_id}/series/{parent_id}/review-library-search?follow_up=true"
    response = await authenticated_client.get(url)
    assert response.status_code == 200, response.text
    search.assert_not_awaited()
    response = await authenticated_client.get(url + f"&q=Harbor&source={source.value}")
    assert response.status_code == 200, response.text
    assert search.call_args.args[0].sources == [source]
    assert 'name="follow_up" value="true"' in response.text
    assert "Preview &amp; add" in response.text
    assert f'name="library_series_id" value="{series_id}"' in response.text
    async with sec_db() as session:
        assert (await session.get(ImportedSeries, parent_id)).series_id is None
        assert not (await session.get(ImportedFile, file_id)).include_in_import
    assert comic.read_bytes() == original


@pytest.mark.parametrize("blocked", [None, "imported", "embedded", "changed", "external_change"])
async def test_switch_from_native_selection_to_comicvine_revalidates_and_clears_choice(
    authenticated_client, sec_db, tmp_path, blocked
):
    from pullbox.core.exceptions import ValidationError
    from pullbox.providers.base import SeriesMetadata
    from pullbox.services.import_orphans import assign_cv_to_orphan

    async with sec_db() as session:
        values, parent_id = await completed_group(session, tmp_path)
        job_id, file_id, series_id, _, comic, original, _ = values
    url = f"/import/{job_id}/series/{parent_id}/review-library?follow_up=true"
    picked = await authenticated_client.get(url)
    assert (
        await authenticated_client.post(
            url,
            data={
                "library_series_id": series_id,
                "token": token(picked.text),
            },
            headers=csrf(authenticated_client),
        )
    ).status_code == 200
    metadata = AsyncMock()
    metadata.get_series_metadata.return_value = SeriesMetadata(
        provider_id="777",
        title="Corrected series",
        sort_title="Corrected series",
        year_start=2024,
        year_end=None,
        status="continuing",
        publisher=None,
        description=None,
        cover_url=None,
        issue_count=1,
        comicvine_url=None,
    )
    async with sec_db() as session:
        parent = await session.get(ImportedSeries, parent_id)
        file = await session.get(ImportedFile, file_id)
        if blocked == "imported":
            file.status = ImportedFileStatus.IMPORTED
        elif blocked == "embedded":
            parent.diagnostics = {
                **parent.diagnostics,
                "metron_import": {
                    "identities": [
                        {"namespace": "metron", "id": "21"},
                    ]
                },
            }
        elif blocked == "changed":

            async def change_during_lookup(*args, **kwargs):
                parent.status = ImportSeriesStatus.SKIPPED
                return metadata.get_series_metadata.return_value

            metadata.get_series_metadata.side_effect = change_during_lookup
        elif blocked == "external_change":

            async def external_change(*args, **kwargs):
                async with sec_db() as other:
                    (await other.get(ImportedSeries, parent_id)).status = ImportSeriesStatus.SKIPPED
                    await other.commit()
                return metadata.get_series_metadata.return_value

            metadata.get_series_metadata.side_effect = external_change
        await session.commit()
        if blocked:
            with pytest.raises(ValidationError):
                await assign_cv_to_orphan(
                    session, parent_id, 777, metadata_service=metadata, log_event=AsyncMock()
                )
            assert parent.series_id == series_id and parent.cv_id is None
            assert parent.diagnostics.get("manual_library_series")
            if blocked not in {"changed", "external_change"}:
                metadata.get_series_metadata.assert_not_awaited()
        else:
            await assign_cv_to_orphan(
                session, parent_id, 777, metadata_service=metadata, log_event=AsyncMock()
            )
            assert parent.series_id is None and parent.cv_id == 777
            assert "manual_library_series" not in parent.diagnostics
            assert "existing_series_id" not in parent.diagnostics
            assert parent.status == ImportSeriesStatus.RECOVERY_PENDING
            assert parent.cv_match_method == "user_override"
        assert file.matched_issue_id is None
        assert not (await session.scalars(select(LibraryFile))).all()
        assert comic.read_bytes() == original
        await session.commit()
    if blocked is None:
        picked = await authenticated_client.get(url)
        assert picked.status_code == 200
        restored = await authenticated_client.post(
            url,
            data={"library_series_id": series_id, "token": token(picked.text)},
            headers=csrf(authenticated_client),
        )
        assert restored.status_code == 200, restored.text
        async with sec_db() as session:
            parent = await session.get(ImportedSeries, parent_id)
            assert parent.series_id == series_id
            assert parent.cv_id is None and parent.user_selected_cv_id is None
            assert parent.cv_url is None
            assert not (await session.get(ImportedFile, file_id)).include_in_import
