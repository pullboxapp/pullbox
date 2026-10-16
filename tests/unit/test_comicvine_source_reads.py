"""Source detail/list contracts exercised against real HTTP and catalog readers."""

import json
import sqlite3
from contextlib import closing

import httpx
import pytest

from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.providers.metadata.sources import ComicVineLocalSource, comicvine_sources
from pullbox.schemas.metadata_sources import SourceCapability, SourceStatus
from pullbox.services.metadata_discovery import MetadataSourceError
from tests.catalog_fixtures import content_hash
from tests.unit.test_catalog_reader import installed_reader
from tests.unit.test_metadata_source_adapters import api_adapter


def series_payload(**changes):
    return {
        "id": 42,
        "name": "Example",
        "publisher": {"name": "Example Publisher"},
        "count_of_issues": 101,
        **changes,
    }


def issue_payload(identifier=123, **changes):
    return {
        "id": identifier,
        "volume": {"id": 42},
        "issue_number": "50-x",
        "name": "Example issue",
        "cover_date": "2026-09-01",
        **changes,
    }


async def test_api_details_use_real_identity_and_keep_lettered_text():
    calls = []

    def handle(request):
        calls.append(request)
        payload = series_payload() if "/volume/" in request.url.path else issue_payload()
        return httpx.Response(200, json={"status_code": 1, "results": payload})

    adapter = await api_adapter(handle)
    try:
        series = await adapter.series("42")
        issue = await adapter.issue("123")
        assert series.data.external_id == "42" and series.data.title == "Example"
        assert series.data.identity_namespace.value == "comicvine"
        assert series.data.source is Source.COMICVINE_API
        assert issue.data.series_external_id == "42"
        assert issue.data.issue_number_text == "50-x"
        assert issue.data.issue_number_key == "50-X"
        assert issue.data.cover_date.isoformat() == "2026-09-01"
        assert [request.url.path for request in calls] == [
            "/api/volume/4050-42/",
            "/api/issue/4000-123/",
        ]
    finally:
        await adapter.close()


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("series", series_payload(id=43)),
        ("series", {"name": "Missing identity"}),
        ("series", series_payload(name="")),
        ("series", series_payload(id=True)),
        ("issue", issue_payload(id=124)),
        ("issue", issue_payload(volume={"id": 0})),
        ("issue", issue_payload(issue_number=None)),
        ("issue", issue_payload(issue_number="")),
    ],
)
async def test_details_reject_fabricated_fallbacks_or_wrong_identity(operation, payload):
    adapter = await api_adapter(
        lambda request: httpx.Response(200, json={"status_code": 1, "results": payload})
    )
    try:
        with pytest.raises(MetadataSourceError) as raised:
            await getattr(adapter, operation)("42" if operation == "series" else "123")
        assert raised.value.status is SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await adapter.close()


async def test_issue_page_fetches_only_one_bounded_page_with_parent_evidence():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={"status_code": 1, "number_of_total_results": 101, "results": [issue_payload()]},
        )

    adapter = await api_adapter(handle)
    try:
        result = await adapter.issues("42", page=2)
        assert result.status is SourceStatus.OK
        assert result.data.total == 101 and result.data.next_page is None
        assert result.data.results[0].issue_number_text == "50-x"
        assert len(calls) == 1
        assert calls[0].url.params["offset"] == "100"
        assert calls[0].url.params["limit"] == "100"
        assert calls[0].url.params["filter"] == "volume:42"
        assert "volume" in calls[0].url.params["field_list"].split(",")
    finally:
        await adapter.close()


@pytest.mark.parametrize(
    "rows,total",
    [
        ([issue_payload(volume={"id": 43})], 1),
        ([issue_payload(), issue_payload()], 2),
        ([issue_payload()], 101),
        ([], 1),
        ([issue_payload()], True),
        ("not a list", 1),
    ],
)
async def test_issue_page_rejects_partial_or_cross_series_payload(rows, total):
    adapter = await api_adapter(
        lambda request: httpx.Response(
            200, json={"status_code": 1, "number_of_total_results": total, "results": rows}
        )
    )
    try:
        with pytest.raises(MetadataSourceError) as raised:
            await adapter.issues("42")
        assert raised.value.status is SourceStatus.INCOMPATIBLE_RESPONSE
    finally:
        await adapter.close()


@pytest.mark.parametrize("operation", ["series", "issue", "issues"])
async def test_not_found_is_distinct_from_unavailability(operation):
    adapter = await api_adapter(lambda request: httpx.Response(404))
    try:
        assert (await getattr(adapter, operation)("42")).status is SourceStatus.NOT_FOUND
    finally:
        await adapter.close()


async def test_local_reads_are_offline_and_preserve_original_designation(tmp_path):
    adapter = ComicVineLocalSource(installed_reader(tmp_path))
    series = await adapter.series("10")
    issue = await adapter.issue("100")
    page = await adapter.issues("10")
    assert series.status is issue.status is page.status is SourceStatus.OK
    assert series.data.source is Source.COMICVINE_LOCAL
    assert series.data.title == "Batman"
    assert issue.data.issue_number_text == "½"
    assert issue.data.issue_number_key == "0.5"
    assert page.data.results == [issue.data]
    assert page.data.total == 1 and page.data.next_page is None
    assert (await adapter.series("999")).status is SourceStatus.NOT_FOUND
    assert (await adapter.issue("999")).status is SourceStatus.NOT_FOUND
    assert (await adapter.issues("10", page=2)).data.total == 1


async def test_local_issue_page_pins_one_generation_and_bounds_rows(tmp_path, monkeypatch):
    reader = installed_reader(tmp_path)
    await reader.series(10)
    with closing(sqlite3.connect(reader.root / "bases/20260913T050000Z.db")) as db, db:
        db.executemany(
            "INSERT INTO issues VALUES (?,10,?,?,?,NULL,NULL,NULL,NULL)",
            [(i, str(i), str(i), str(i)) for i in range(101, 301)],
        )
        counts = json.loads(
            db.execute("SELECT value FROM dataset_manifest WHERE key='counts'").fetchone()[0]
        )
        counts["issues"] = 201
        db.execute("UPDATE dataset_manifest SET value=? WHERE key='counts'", (json.dumps(counts),))
        db.execute(
            "UPDATE dataset_manifest SET value=? WHERE key='content_sha256'",
            (json.dumps(content_hash(db)),),
        )
    # This synthetic catalog mutation is complete before the read; no live catalog is edited.
    original_generation = reader._generation
    generations = []

    def generation():
        generations.append(1)
        return original_generation()

    monkeypatch.setattr(reader, "_generation", generation)
    result = await ComicVineLocalSource(reader).issues("10", page=2)
    assert result.data.total == 201 and result.data.next_page == 3
    assert len(result.data.results) == 100
    assert [int(item.external_id) for item in result.data.results] == list(range(200, 300))
    assert generations == [1]


def test_comicvine_registers_only_implemented_read_capabilities():
    for source, registration in comicvine_sources().items():
        assert {
            SourceCapability.SERIES_DETAILS,
            SourceCapability.ISSUE_DETAILS,
            SourceCapability.ISSUE_LIST,
        } <= registration.capabilities
        assert (SourceCapability.STORY_ARC_SEARCH in registration.capabilities) == (
            source is Source.COMICVINE_API
        )
