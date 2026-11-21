"""Native discovery in review reuses Add Series without guessing file matches."""

from unittest.mock import AsyncMock

import pytest

from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportJob, ImportJobStatus
from pullbox.schemas.metadata_sources import ProviderSeriesRead, SeriesDiscoveryRead, SourceOutcome
from tests.ui.test_import_library_selection import seed_library_choice
from tests.ui.test_import_review_file_routes import csrf, token

pytest_plugins = ["conftest_security"]


def discovery(source=Source.METRON_API, identifier="21"):
    return SeriesDiscoveryRead(
        results=[
            ProviderSeriesRead(
                source=source,
                identity_namespace=source.identity_namespace,
                external_id=identifier,
                title="Harbor Lights",
                year_start=2024,
            ),
            ProviderSeriesRead(
                source=source,
                identity_namespace=source.identity_namespace,
                external_id="999",
                title="Missing Harbor",
                year_start=2025,
            ),
        ],
        sources=[SourceOutcome(source=source, status="ok")],
    )


@pytest.mark.parametrize("source", [Source.METRON_API, Source.GCD_LOCAL])
async def test_review_search_is_native_only_and_keeps_files_unselected(
    authenticated_client, sec_db, tmp_path, monkeypatch, source
):
    from pullbox.ui import series_routes

    search = AsyncMock(return_value=discovery(source))
    monkeypatch.setattr(series_routes, "search_snapshot", search)
    async with sec_db() as session:
        job_id, file_id, series_id, _, comic, original, _ = await seed_library_choice(
            session, tmp_path, namespace=source.identity_namespace.value
        )
        parent_id = (await session.get(ImportedFile, file_id)).import_series_id
    url = f"/import/{job_id}/series/{parent_id}/review-library-search"
    empty = await authenticated_client.get(url)
    assert empty.status_code == 200, empty.text
    search.assert_not_awaited()
    response = await authenticated_client.get(url + "?q=Harbor")
    assert response.status_code == 200, response.text
    query = search.call_args.args[0]
    assert set(query.sources) == {Source.METRON_API, Source.GCD_LOCAL, Source.GCD_API_V2}
    assert query.query == "Harbor"
    assert "Preview &amp; add" in response.text
    assert 'value="comicvine_api"' not in response.text
    assert 'value="comicvine_local"' not in response.text
    assert f'name="library_series_id" value="{series_id}"' in response.text
    assert "No files are selected or imported" in response.text
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        assert file.status == ImportedFileStatus.NO_MATCH
        assert not file.include_in_import and file.matched_issue_id is None
    assert comic.read_bytes() == original
    chosen = await authenticated_client.post(
        f"/import/{job_id}/series/{parent_id}/review-library",
        data={"token": token(response.text), "library_series_id": series_id},
        headers=csrf(authenticated_client),
    )
    assert chosen.status_code == 200, chosen.text
    async with sec_db() as session:
        file = await session.get(ImportedFile, file_id)
        assert not file.include_in_import and file.matched_issue_id is None


async def test_review_search_rejects_other_sources_and_stale_reviews(
    authenticated_client, sec_db, tmp_path, monkeypatch
):
    from pullbox.ui import series_routes

    search = AsyncMock(return_value=discovery())
    monkeypatch.setattr(series_routes, "search_snapshot", search)
    async with sec_db() as session:
        job_id, file_id, *_ = await seed_library_choice(session, tmp_path)
        parent_id = (await session.get(ImportedFile, file_id)).import_series_id
    url = f"/import/{job_id}/series/{parent_id}/review-library-search"
    denied = await authenticated_client.get(url + "?q=Harbor&source=comicvine_api")
    assert denied.status_code == 422
    search.assert_not_awaited()
    picked = await authenticated_client.get(url + "?q=Harbor&source=metron_api")
    assert picked.status_code == 200
    assert search.call_args.args[0].sources == [Source.METRON_API]
    async with sec_db() as session:
        job = await session.get(ImportJob, job_id)
        job.status = ImportJobStatus.COMPLETED
        await session.commit()
    search.reset_mock()
    stale = await authenticated_client.get(url + "?q=Harbor")
    assert stale.status_code == 422
    assert "no longer ready for review" in stale.text
    search.assert_not_awaited()


async def test_review_changed_during_provider_search_does_not_get_choice_tokens(
    authenticated_client, sec_db, tmp_path, monkeypatch
):
    from pullbox.ui import series_routes

    async with sec_db() as session:
        job_id, file_id, *_ = await seed_library_choice(session, tmp_path)
        parent_id = (await session.get(ImportedFile, file_id)).import_series_id

    async def changed(*args, **kwargs):
        async with sec_db() as session:
            job = await session.get(ImportJob, job_id)
            job.status = ImportJobStatus.COMPLETED
            await session.commit()
        return discovery()

    monkeypatch.setattr(series_routes, "search_snapshot", changed)
    response = await authenticated_client.get(
        f"/import/{job_id}/series/{parent_id}/review-library-search?q=Harbor"
    )
    assert response.status_code == 422
    assert 'name="token"' not in response.text


async def test_added_series_choice_is_exact_not_a_same_title_search(
    authenticated_client, sec_db, tmp_path
):
    from pullbox.models.metadata_identity import SeriesExternalIdentity
    from pullbox.models.series import Series

    async with sec_db() as session:
        job_id, file_id, series_id, *_ = await seed_library_choice(session, tmp_path)
        parent_id = (await session.get(ImportedFile, file_id)).import_series_id
        other = Series(title="Harbor Lights", sort_title="Harbor Lights", year_start=2024)
        session.add(other)
        await session.flush()
        session.add(
            SeriesExternalIdentity(
                series_id=other.id,
                identity_namespace="metron",
                external_id="22",
                verification_state="verified",
                evidence_kind="provider_result",
            )
        )
        await session.commit()
    response = await authenticated_client.get(
        f"/import/{job_id}/series/{parent_id}/review-library?library_series_id={series_id}"
    )
    assert response.status_code == 200
    assert response.text.count('name="library_series_id"') == 1
    assert f'name="library_series_id" value="{series_id}"' in response.text
