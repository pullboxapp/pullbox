"""Catalog-backed discovery never labels basic data as a full provider refresh."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from pullbox.models.issue import Issue, IssueType
from pullbox.services.catalog.lookup import CatalogLookupService
from pullbox.services.import_cv_search import search_with_retry
from pullbox.services.import_provider_cache import build_import_scan_metadata_provider
from pullbox.services.metadata_service import MetadataService
from pullbox.services.series_service import SeriesService
from pullbox.ui.comicvine_provider import open_comicvine_ui_provider
from tests.unit.test_catalog_reader import installed_reader


async def test_metadata_hydration_uses_catalog_without_provider_calls(tmp_path):
    reader = installed_reader(tmp_path)
    live = AsyncMock()
    service = MetadataService(live, tmp_path)
    service._catalog = reader
    meta = await service.get_series_metadata(10)
    issues = await service.get_issue_summaries_for_series(10)
    assert meta.title == "Batman"
    assert len(issues) == 1
    live.get_series.assert_not_awaited()
    live.get_issues_for_series.assert_not_awaited()


@pytest.mark.parametrize("ids", [[999, 10, 10], [10, 999]])
async def test_catalog_profile_batch_preserves_matches_when_a_series_is_missing(tmp_path, ids):
    live = AsyncMock()
    service = MetadataService(live, tmp_path, catalog=installed_reader(tmp_path))

    profiles = await service.get_series_metadata_batch(ids)

    assert list(profiles) == [10]
    assert profiles[10].title == "Batman"
    assert live.mock_calls == []


async def test_empty_local_search_does_not_retry_or_sleep(tmp_path, monkeypatch):
    provider = CatalogLookupService(installed_reader(tmp_path))
    sleep = AsyncMock()
    monkeypatch.setattr("pullbox.services.import_cv_search.asyncio.sleep", sleep)
    assert await search_with_retry(provider, "No matching book", None) == []
    sleep.assert_not_awaited()


async def test_ui_search_works_without_comicvine_key(tmp_path, monkeypatch):
    reader = installed_reader(tmp_path)
    monkeypatch.setattr("pullbox.services.catalog.reader.get_catalog_reader", lambda: reader)
    monkeypatch.setattr(
        "pullbox.core.comicvine_key.get_comicvine_api_key", AsyncMock(return_value="")
    )
    async with open_comicvine_ui_provider(AsyncMock(), prefer_catalog=True) as lookup:
        results, _ = await lookup.search_series_globally("Batman")
    assert results[0].provider_id == "10"


async def test_catalog_provenance_and_cutoff_persist_for_new_records(tmp_path, db_session):
    reader = installed_reader(tmp_path)
    service = MetadataService(AsyncMock(), tmp_path)
    service._catalog = reader
    meta = await reader.series(10)
    series = await service.upsert_series_metadata(db_session, 10, meta)
    assert series.metadata_source == "pullbox_catalog"
    assert series.metadata_last_refreshed == meta.source_cutoff_at
    issues = await service.upsert_issue_summaries(db_session, series, await reader.issues(10))
    assert issues[0].metadata_source == "pullbox_catalog"


async def test_post_import_enrichment_keeps_live_batching(tmp_path):
    reader = installed_reader(tmp_path)

    class Live:
        def __init__(self):
            self.calls = []

        async def get_issue_batch(self, ids):
            self.calls.append(ids)
            return {}

    live = Live()
    service = MetadataService(live, tmp_path)
    service._catalog = reader
    assert await service.prefetch_issue_metadata_batch([100]) == {}
    assert live.calls == [["100"]]


async def test_basic_catalog_does_not_demote_full_metadata(tmp_path, db_session):
    reader = installed_reader(tmp_path)
    service = MetadataService(AsyncMock(), tmp_path)
    meta = await reader.series(10)
    series = await service.upsert_series_metadata(db_session, 10, meta)
    series.metadata_source = "comicvine"
    series.description = "Complete live description"
    series.title = "Newer live title"
    original_date = series.metadata_last_refreshed
    await service.upsert_series_metadata(db_session, 10, meta)
    assert series.metadata_source == "comicvine"
    assert series.title == "Newer live title"
    assert series.description == "Complete live description"
    assert series.metadata_last_refreshed == original_date


async def test_basic_catalog_preserves_existing_live_issue_fields(tmp_path, db_session):
    reader = installed_reader(tmp_path)
    service = MetadataService(AsyncMock(), tmp_path)
    series = await service.upsert_series_metadata(db_session, 10, await reader.series(10))
    summaries = await reader.issues(10)
    issue = (await service.upsert_issue_summaries(db_session, series, summaries))[0]
    issue.metadata_source = "comicvine"
    issue.title = "Newer live issue title"
    issue.issue_type = IssueType.SPECIAL
    issue.issue_number = 1.0
    issue.issue_number_text = "1"
    await db_session.flush()
    assert await service.upsert_issue_summaries(db_session, series, summaries) == []
    assert issue.title == "Newer live issue title"
    assert issue.issue_type == IssueType.SPECIAL
    assert issue.issue_number_text == "1"
    assert issue.metadata_source == "comicvine"


async def test_full_issue_refresh_bypasses_basic_catalog(tmp_path, db_session):
    reader = installed_reader(tmp_path)
    live = AsyncMock()
    live.get_issues_for_series.return_value = []
    service = MetadataService(live, tmp_path, catalog=reader)
    series = await service.upsert_series_metadata(db_session, 10, await reader.series(10))
    await service.fetch_issues_for_series(db_session, series.id)
    live.get_issues_for_series.assert_awaited_once_with("10")


async def test_scan_provider_stack_keeps_search_and_identity_local(
    tmp_path, db_session, monkeypatch
):
    reader = installed_reader(tmp_path)
    monkeypatch.setattr("pullbox.services.catalog.reader.get_catalog_reader", lambda: reader)
    live = AsyncMock()
    provider = build_import_scan_metadata_provider(db_session, live)
    first = await search_with_retry(provider, "Batman", None)
    second = await search_with_retry(provider, "Batman", None)
    assert first == second
    assert first[0].provider_id == "10"
    assert (await provider.get_series("10")).title == "Batman"
    assert (await provider.get_issue("100")).series_provider_id == "10"
    assert (await provider.get_issues_for_series("10"))[0].provider_id == "100"
    assert await search_with_retry(provider, "Missing series", None) == []
    assert provider.cache_metrics()["memory_hits"]["search_series_globally"] == 1
    assert live.mock_calls == []


async def test_add_series_materializes_basic_catalog_without_live_metadata(tmp_path, db_session):
    reader = installed_reader(tmp_path)
    live = AsyncMock()
    metadata = MetadataService(live, tmp_path, catalog=reader)
    service = SeriesService(metadata, AsyncMock())
    series = await service.add_from_comicvine(db_session, 10)
    issue = await db_session.scalar(select(Issue).where(Issue.series_id == series.id))
    assert series.comicvine_id == 10
    assert series.metadata_source == "pullbox_catalog"
    assert issue.comicvine_id == 100
    assert issue.issue_number_text == "0.5"
    assert issue.metadata_source == "pullbox_catalog"
    assert live.mock_calls == []
