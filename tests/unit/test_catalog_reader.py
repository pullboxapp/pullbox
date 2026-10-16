"""Local matching uses aliases, exact IDs, and normalized issue designations."""

import json
import os

import pytest

from pullbox.services.catalog.contract import CatalogError
from pullbox.services.catalog.reader import CatalogReader
from tests.catalog_fixtures import build_snapshot


def installed_reader(tmp_path):
    root = tmp_path / "catalog"
    (root / "bases").mkdir(parents=True)
    build_snapshot(root / "bases/20260913T050000Z.db")
    (root / "active.json").write_text(
        json.dumps(
            {
                "path": "bases/20260913T050000Z.db",
                "version": "20260913T050000Z",
                "base_version": "20260913T050000Z",
                "source_cutoff_at": "2026-09-13T05:00:00+00:00",
            }
        )
    )
    return CatalogReader(root)


async def test_alias_search_returns_existing_comicvine_identity(tmp_path):
    reader = installed_reader(tmp_path)
    results = await reader.search("Dark Knight")
    assert len(results) == 1
    assert results[0].provider_id == "10"
    assert results[0].title == "Batman"
    assert results[0].publisher == "DC"
    assert await reader.search("Batman", year=2024) == []


async def test_search_input_is_not_fts_syntax_or_sql(tmp_path):
    reader = installed_reader(tmp_path)
    assert await reader.search('" OR * )') == []
    assert await reader.search("'; DROP TABLE series; --") == []
    assert (await reader.series(10)).title == "Batman"


async def test_issue_catalog_preserves_fractional_identity_and_cutoff(tmp_path):
    reader = installed_reader(tmp_path)
    issues = await reader.issues(10)
    assert len(issues) == 1
    assert issues[0].provider_id == "100"
    assert issues[0].issue_number == 0.5
    assert issues[0].issue_number_text == "0.5"
    assert issues[0].source_cutoff_at.isoformat() == "2026-09-13T05:00:00+00:00"
    assert await reader.series(999) is None


async def test_rejects_active_pointer_outside_catalog(tmp_path):
    reader = installed_reader(tmp_path)
    (reader.root / "active.json").write_text(
        json.dumps({"path": "../../user.db", "version": "20260913T050000Z"})
    )
    with pytest.raises(CatalogError):
        await reader.search("Batman")


async def test_cache_token_tracks_file_replacement_and_pointer_cutoff(tmp_path):
    reader = installed_reader(tmp_path)
    first = await reader.cache_token()
    assert isinstance(first, str) and len(first) == 64
    assert await reader.cache_token() == first
    path = reader.root / "bases/20260913T050000Z.db"
    original = path.stat()
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns + 1))
    second = await reader.cache_token()
    assert second != first
    pointer = reader.root / "active.json"
    value = json.loads(pointer.read_text())
    value["source_cutoff_at"] = "2026-09-14T05:00:00+00:00"
    pointer.write_text(json.dumps(value))
    assert await reader.cache_token() != second
    assert str(tmp_path) not in first


async def test_cache_token_rejects_corrupted_generation(tmp_path):
    reader = installed_reader(tmp_path)
    await reader.cache_token()
    (reader.root / "bases/20260913T050000Z.db").write_bytes(b"not a database")
    with pytest.raises(CatalogError):
        await reader.cache_token()


async def test_cached_issue_parent_batch_is_bounded_and_omits_missing_ids(tmp_path, monkeypatch):
    from pullbox.services.catalog.lookup import CatalogLookupService

    reader = installed_reader(tmp_path)
    queries = []
    original = reader._query

    def capture(sql, params):
        queries.append(params)
        return original(sql, params)

    monkeypatch.setattr(reader, "_query", capture)
    lookup = CatalogLookupService(reader)
    result = await lookup.get_issue_batch_cached([str(n) for n in range(1, 252)] + ["100"])

    assert list(result) == ["100"]
    assert result["100"].series_provider_id == "10"
    assert result["100"].issue_number_text == "0.5"
    assert len(queries) == 2
    assert max(map(len, queries)) <= 200
    assert await lookup.get_issue_batch_cached([]) == {}
    assert len(queries) == 2
