"""Local matching uses aliases, exact IDs, and normalized issue designations."""

import json

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
