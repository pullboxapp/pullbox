"""Recent windows cannot masquerade as complete or cross-series issue catalogs."""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.providers.metadata.sources import ComicVineLocalSource, metadata_sources
from pullbox.schemas.metadata_sources import (
    MetadataFetch,
    RecentIssueWindow,
    SourceCapability,
    SourceStatus,
)
from pullbox.services.metadata_discovery import MetadataSourceError, MetadataSourceRegistry
from tests.catalog_fixtures import TABLES, content_hash
from tests.unit.test_catalog_reader import installed_reader
from tests.unit.test_metadata_discovery import registration, runtime
from tests.unit.test_metadata_source_adapters import api_adapter
from tests.unit.test_metadata_source_reads import ReadAdapter, issue_row, registry
from tests.unit.test_metron_source import envelope, source
from tests.unit.test_metron_source import issue_row as metron_issue

SINCE = datetime(2026, 9, 15, tzinfo=UTC)


def window(**changes):
    return RecentIssueWindow(
        results=[issue_row(source_updated_at=SINCE + timedelta(days=1))],
        matched_total=1,
        scope="modified_since",
        since=SINCE,
        truncated=False,
    ).model_copy(update=changes)


class RecentAdapter(ReadAdapter):
    async def recent_issues(self, external_id, *, since):
        self.calls.append((external_id, since))
        self.started.set()
        if self.wait:
            await self.wait.wait()
        if self.error:
            raise self.error
        return self.result or MetadataFetch(status=SourceStatus.OK, data=window())


async def test_registry_preserves_bounded_window_without_claiming_full_membership():
    adapter = RecentAdapter()
    result = await registry(adapter).recent_issues(adapter.source, "00042", since=SINCE)
    assert result.status is SourceStatus.OK
    assert result.data.full_catalog is False
    assert result.data.results[0].issue_number_text == "50-x"
    assert adapter.calls == [("42", SINCE)] and adapter.closed == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"results": [issue_row(series_external_id="43")]},
        {"results": [issue_row(), issue_row()], "matched_total": 2},
        {"matched_total": True},
        {"matched_total": 2},
        {"truncated": True},
        {"full_catalog": True},
        {"since": SINCE - timedelta(days=1)},
        {"results": [issue_row(source_updated_at=SINCE)]},
        {"results": [issue_row(source=Source.COMICVINE_API)]},
    ],
)
async def test_registry_rejects_wrong_parent_incomplete_or_different_window(changes):
    adapter = RecentAdapter(result=MetadataFetch(status=SourceStatus.OK, data=window(**changes)))
    result = await registry(adapter).recent_issues(adapter.source, "42", since=SINCE)
    assert result.status is SourceStatus.INCOMPATIBLE_RESPONSE and result.data is None
    assert adapter.closed == 1


@pytest.mark.parametrize("since", [datetime(2026, 9, 15), "2026-09-15", None])
async def test_invalid_checkpoint_never_constructs_adapter(since):
    adapter = RecentAdapter()
    with pytest.raises(ValueError):
        await registry(adapter).recent_issues(adapter.source, "42", since=since)
    assert not adapter.calls and adapter.closed == 0


async def test_disabled_and_unadvertised_recent_reads_never_contact_provider():
    adapter = RecentAdapter()
    for enabled, capabilities, expected in (
        (False, list(SourceCapability), SourceStatus.DISABLED),
        (True, [SourceCapability.ISSUE_LIST], SourceStatus.UNSUPPORTED),
    ):
        instance = MetadataSourceRegistry(
            [runtime(adapter.source, enabled=enabled)],
            factories={adapter.source: registration(adapter, capabilities=capabilities)},
        )
        assert (await instance.recent_issues(adapter.source, "42", since=SINCE)).status is expected
    assert not adapter.calls and adapter.closed == 0


async def test_recent_timeout_cancel_and_retry_after_keep_owned_client_lifecycle():
    adapter = RecentAdapter(error=MetadataSourceError(SourceStatus.RATE_LIMITED, 720))
    result = await registry(adapter).recent_issues(adapter.source, "42", since=SINCE)
    assert result.status is SourceStatus.RATE_LIMITED and result.retry_after_seconds == 720
    assert adapter.closed == 1
    adapter = RecentAdapter(wait=asyncio.Event())
    result = await registry(adapter, per_source_timeout=0.01).recent_issues(
        adapter.source, "42", since=SINCE
    )
    assert result.status is SourceStatus.TIMEOUT and adapter.closed == 1
    task = asyncio.create_task(registry(adapter).recent_issues(adapter.source, "42", since=SINCE))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert adapter.closed == 2


@pytest.mark.parametrize("total", [0, 1, 100, 101])
async def test_metron_modified_window_is_one_filtered_request_even_when_overflowing(total):
    calls = []
    params = {"series_id": "8", "modified_gt": SINCE.isoformat(), "page": "1"}

    def handle(request):
        calls.append(request)
        next_url = str(request.url.copy_set_param("page", "2")) if total > 100 else None
        return httpx.Response(
            200,
            json=envelope(
                [metron_issue(i) for i in range(1, min(total, 100) + 1)],
                count=total,
                next_url=next_url,
            ),
        )

    adapter = source(handle)
    try:
        result = await adapter.recent_issues("8", since=SINCE)
        assert result.status is SourceStatus.OK
        assert len(calls) == 1 and calls[0].url.path == "/api/issue/"
        assert dict(calls[0].url.params) == params
        assert result.data.matched_total == total
        assert result.data.scope == "modified_since" and result.data.since == SINCE
        assert result.data.truncated is (total > 100) and not result.data.full_catalog
        assert len(result.data.results) == min(total, 100)
    finally:
        await adapter.close()


@pytest.mark.parametrize("fault", ["parent", "timestamp", "continuation"])
async def test_metron_window_rejects_filter_or_continuation_disagreement(fault):
    def handle(request):
        rows = [metron_issue(i) for i in range(1, 101)]
        link = str(request.url.copy_set_param("page", "2"))
        if fault == "parent":
            rows[0]["series"]["id"] = 9
        elif fault == "timestamp":
            rows[0]["modified"] = SINCE.isoformat()
        else:
            link = str(request.url.copy_set_param("page", "2").copy_set_param("series_id", "9"))
        return httpx.Response(200, json=envelope(rows, count=101, next_url=link))

    adapter = source(handle)
    try:
        with pytest.raises(MetadataSourceError) as error:
            await adapter.recent_issues("8", since=SINCE)
        assert error.value.status is SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await adapter.close()


@pytest.mark.parametrize("total", [0, 1, 100, 101])
async def test_comicvine_window_retains_one_request_and_explicit_parent(total):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "status_code": 1,
                "number_of_total_results": total,
                "results": [
                    {"id": i, "volume": {"id": 42}, "issue_number": "50-x"}
                    for i in range(1, min(total, 100) + 1)
                ],
            },
        )

    adapter = await api_adapter(handle)
    try:
        result = await adapter.recent_issues("42", since=SINCE)
        assert result.status is SourceStatus.OK
        assert len(calls) == 1 and calls[0].url.params["sort"] == "store_date:desc"
        assert calls[0].url.params["filter"] == "volume:42"
        assert calls[0].url.params["limit"] == "100" and calls[0].url.params["offset"] == "0"
        assert "volume" in calls[0].url.params["field_list"].split(",")
        assert result.data.scope == "recent_publication" and result.data.since is None
        assert result.data.matched_total == total and result.data.truncated is (total > 100)
        assert not result.data.full_catalog
    finally:
        await adapter.close()


@pytest.mark.parametrize("identifier", [10, 999])
async def test_local_window_retains_generation_including_empty_result(tmp_path, identifier):
    reader = installed_reader(tmp_path)
    adapter = ComicVineLocalSource(reader)
    result = await adapter.recent_issues(str(identifier), since=SINCE)
    assert result.status is SourceStatus.OK
    assert result.data.matched_total == (1 if identifier == 10 else 0)
    assert result.data.source_updated_at == datetime(2026, 9, 13, 5, tzinfo=UTC)
    assert result.data.scope == "recent_publication" and not result.data.full_catalog
    assert all(
        row.source_updated_at == result.data.source_updated_at for row in result.data.results
    )


async def test_registry_normalizes_checkpoint_timezone():
    adapter = RecentAdapter()
    await registry(adapter).recent_issues(
        adapter.source, "42", since=SINCE.astimezone(timezone(timedelta(hours=-7)))
    )
    assert adapter.calls == [("42", SINCE)]
    assert adapter.calls[0][1].tzinfo is UTC


def test_executable_sources_advertise_recent_issue_capability():
    for slug in (Source.COMICVINE_API, Source.COMICVINE_LOCAL, Source.METRON_API):
        assert SourceCapability.RECENT_ISSUES in metadata_sources()[slug].capabilities


async def test_recent_read_does_not_reuse_page_cache_or_accept_unsolicited_not_modified():
    adapter = RecentAdapter()
    cache = AsyncMock()
    result = await registry(adapter, read_cache=cache).recent_issues(
        adapter.source, "42", since=SINCE
    )
    assert result.status is SourceStatus.OK
    cache.get.assert_not_called()
    adapter.result = MetadataFetch(status=SourceStatus.NOT_MODIFIED)
    result = await registry(adapter).recent_issues(adapter.source, "42", since=SINCE)
    assert result.status is SourceStatus.INCOMPATIBLE_RESPONSE


async def test_recent_gcd_flag_and_missing_local_catalog_remain_fail_closed(tmp_path):
    instance = MetadataSourceRegistry([runtime(Source.GCD_API_V2)])
    result = await instance.recent_issues(Source.GCD_API_V2, "42", since=SINCE)
    assert result.status is SourceStatus.FEATURE_DISABLED
    reader = installed_reader(tmp_path)
    (reader.root / "active.json").unlink()
    with pytest.raises(MetadataSourceError) as error:
        await ComicVineLocalSource(reader).recent_issues("10", since=SINCE)
    assert error.value.status is SourceStatus.UNCONFIGURED


@pytest.mark.parametrize("fault", ["parent", "missing_parent", "duplicate", "missing_row"])
async def test_comicvine_window_rejects_wrong_membership_without_partial_success(fault):
    rows = [{"id": i, "volume": {"id": 42}, "issue_number": str(i)} for i in (1, 2)]
    if fault == "parent":
        rows[0]["volume"]["id"] = 43
    elif fault == "missing_parent":
        del rows[0]["volume"]
    elif fault == "duplicate":
        rows[1]["id"] = 1
    else:
        rows.pop()
    adapter = await api_adapter(
        lambda request: httpx.Response(
            200,
            json={
                "status_code": 1,
                "number_of_total_results": 2,
                "results": rows,
            },
        )
    )
    try:
        with pytest.raises(MetadataSourceError) as error:
            await adapter.recent_issues("42", since=SINCE)
        assert error.value.status is SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await adapter.close()


async def test_local_window_sorts_and_limits_real_rows_without_including_other_series(tmp_path):
    reader = installed_reader(tmp_path)
    with closing(sqlite3.connect(reader.root / "bases/20260913T050000Z.db")) as db, db:
        db.execute("DELETE FROM issues")
        db.execute("INSERT INTO series VALUES (11,'Other',2024,1,1,NULL)")
        db.execute("INSERT INTO series_fts(rowid,series_id,name,aliases) VALUES (11,11,'Other','')")
        db.executemany(
            "INSERT INTO issues VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (i, 10, str(i), str(i), str(i), None, None, "2026-09-01" if i < 120 else None, None)
                for i in range(1, 131)
            ]
            + [(999, 11, "1", "1", "1", None, None, "2027-01-01", None)],
        )
        # Deliberately give a small ID the newest publication date.
        db.execute("UPDATE issues SET store_date='2026-09-15' WHERE id=1")
        db.execute(
            "UPDATE dataset_manifest SET value=? WHERE key='counts'",
            (
                json.dumps(
                    {
                        table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        for table in TABLES
                    }
                ),
            ),
        )
        db.execute(
            "UPDATE dataset_manifest SET value=? WHERE key='content_sha256'",
            (json.dumps(content_hash(db)),),
        )
    statements = []
    original = reader._query

    def traced(sql, params):
        statements.append((sql, params))
        return original(sql, params)

    reader._query = traced
    result = await ComicVineLocalSource(reader).recent_issues("10", since=SINCE)
    assert result.status is SourceStatus.OK
    assert result.data.matched_total == 130 and result.data.truncated
    assert [row.external_id for row in result.data.results] == ["1", *map(str, range(119, 20, -1))]
    assert all(
        row.source_updated_at == result.data.source_updated_at for row in result.data.results
    )
    assert len(statements) == 1, "Total, rows and generation must come from one bounded read"
