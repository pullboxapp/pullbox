"""GCD's real dump schema, exercised through settings, search, preview and Add."""

import hashlib
import os
import sqlite3
import sys

import pytest
from sqlalchemy import func, select

from pullbox.core.events import EventBus
from pullbox.models import Issue, Series
from pullbox.models.library import LibraryRoot
from tests.api.test_metadata_sources_api import csrf, policy

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest_plugins = ["conftest_security"]
URL = "/api/v1/metadata/sources/gcd_local"


def gcd_dump(path):
    # Required columns use the 2026-09-29 official dump's names and semantics.
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE gcd_publisher (id INTEGER PRIMARY KEY, name TEXT, deleted INTEGER);
            CREATE TABLE stddata_language (id INTEGER PRIMARY KEY, code TEXT);
            CREATE TABLE gcd_series (
                id INTEGER PRIMARY KEY, name TEXT, sort_name TEXT, year_began INTEGER,
                year_ended INTEGER, publisher_id INTEGER, language_id INTEGER,
                issue_count INTEGER, is_current INTEGER, deleted INTEGER,
                is_comics_publication INTEGER, notes TEXT);
            CREATE TABLE gcd_issue (
                id INTEGER PRIMARY KEY, series_id INTEGER, number TEXT, title TEXT,
                sort_code INTEGER, deleted INTEGER, variant_of_id INTEGER, notes TEXT,
                key_date TEXT, on_sale_date TEXT, page_count NUMERIC);
            CREATE INDEX issue_series ON gcd_issue(series_id, sort_code);
            CREATE INDEX series_name ON gcd_series(name);
            INSERT INTO gcd_publisher VALUES (1, 'Example Press', 0);
            INSERT INTO stddata_language VALUES (1, 'en');
            INSERT INTO gcd_series VALUES
                (2999, 'Swamp Thing', 'Swamp Thing', 1985, 1996, 1, 1, 3, 0, 0, 1, ''),
                (3000, 'Deleted series', 'Deleted series', 2000, NULL, 1, 1, 0, 0, 1, 1, ''),
                (3001, 'Other series', 'Other series', 2001, NULL, 1, 1, 1, 0, 0, 1, '');
            INSERT INTO gcd_issue VALUES
                (10, 2999, '13a', 'First', 1, 0, NULL, '', '1985-08-00', '1985-04-30', 36),
                (11, 2999, '13b', 'Second', 3, 0, NULL, '', '1985-09-01', '', 36.5),
                (12, 2999, '50-x', 'Third', 4, 0, NULL, '', '', '', 32),
                (13, 2999, '13a', 'Variant', 2, 0, 10, '', '', '', 36),
                (14, 2999, '99', 'Deleted', 5, 1, NULL, '', '', '', 32),
                (15, 3001, '1', 'Cross-series representative', 1, 0, 10, '', '', '', 32);
        """)
    return path


async def activate(client, path, revision=0):
    return await client.put(
        URL,
        json=policy(revision=revision, settings={"database_path": str(path)}),
        headers=csrf(client),
    )


async def search(client, query="Swamp Thing"):
    return await client.post(
        "/api/v1/metadata/search",
        json={"query": query, "sources": ["gcd_local"]},
        headers=csrf(client),
    )


async def test_bad_candidate_never_replaces_working_gcd(authenticated_client, tmp_path):
    path = gcd_dump(tmp_path / "gcd.db")
    assert (await activate(authenticated_client, path)).status_code == 200
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a SQLite database")
    failed = await activate(authenticated_client, bad, revision=1)
    assert failed.status_code == 400
    result = await search(authenticated_client)
    assert result.json()["results"][0]["external_id"] == "2999"


async def test_gcd_search_preview_and_add_are_readonly_and_repeat_safe(
    authenticated_client, sec_db, monkeypatch, tmp_path
):
    path = gcd_dump(tmp_path / "gcd.db")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    saved = await activate(authenticated_client, path)
    assert saved.status_code == 200, saved.text
    result = await search(authenticated_client)
    assert result.status_code == 200
    assert result.json()["sources"][0]["status"] == "ok"
    row = result.json()["results"][0]
    assert (row["external_id"], row["identity_namespace"], row["issue_count"]) == ("2999", "gcd", 3)
    assert row["image_url"] is None and row["cross_identities"] == []
    assert (await search(authenticated_client, "Deleted")).json()["results"] == []
    preview = await authenticated_client.post(
        "/api/v1/metadata/series/preview",
        json={"source": "gcd_local", "external_id": "2999"},
        headers=csrf(authenticated_client),
    )
    assert preview.status_code == 200, preview.text
    rows = preview.json()["issues"]["data"]["results"]
    assert [row["issue_number_text"] for row in rows] == ["13a", "13b", "50-x"]
    assert rows[0]["cover_date"] is None and rows[0]["store_date"] == "1985-04-30"
    assert rows[1]["page_count"] is None  # Fractional pages are not rounded into facts.

    from pullbox.api.v1 import series as routes

    monkeypatch.setattr(routes, "get_event_bus", EventBus)
    root_path = tmp_path / "library"
    root_path.mkdir()
    async with sec_db.begin() as session:
        root = LibraryRoot(name="Test", path=str(root_path), allow_managed_writes=True)
        session.add(root)
        await session.flush()
        root_id = root.id
    body = {
        "source": "gcd_local",
        "external_id": "2999",
        "source_revision": 1,
        "library_root_id": root_id,
    }
    first = await authenticated_client.post(
        "/api/v1/series", json=body, headers=csrf(authenticated_client)
    )
    assert first.status_code == 201, first.text
    assert first.json()["comicvine_id"] is None
    assert first.json()["issue_count"] == 3
    assert first.json()["issue_catalog_state"] == "complete"
    again = await authenticated_client.post(
        "/api/v1/series", json=body, headers=csrf(authenticated_client)
    )
    assert again.status_code == 201 and again.json()["id"] == first.json()["id"]
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(Series)) == 1
        assert await session.scalar(select(func.count()).select_from(Issue)) == 3
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in tmp_path.glob("gcd*")) == ["gcd.db"]


async def test_changed_dump_requires_validation_before_queries(authenticated_client, tmp_path):
    path = gcd_dump(tmp_path / "gcd.db")
    assert (await activate(authenticated_client, path)).status_code == 200
    with sqlite3.connect(path) as db:
        db.execute("UPDATE gcd_series SET name='Replaced' WHERE id=2999")
    result = await search(authenticated_client)
    assert result.json()["sources"][0]["status"] == "invalid_configuration"
    assert result.json()["results"] == []
    assert (await activate(authenticated_client, path, revision=1)).status_code == 200
    assert (await search(authenticated_client, "Replaced")).json()["results"][0][
        "external_id"
    ] == "2999"


async def test_cross_series_variant_stays_represented(authenticated_client, tmp_path):
    assert (await activate(authenticated_client, gcd_dump(tmp_path / "gcd.db"))).status_code == 200
    result = await authenticated_client.post(
        "/api/v1/metadata/series/preview",
        json={"source": "gcd_local", "external_id": "3001"},
        headers=csrf(authenticated_client),
    )
    assert result.json()["series"]["data"]["issue_count"] == 1
    assert result.json()["issues"]["data"]["results"][0]["external_id"] == "15"


async def test_gcd_preview_and_add_expose_structured_credits(
    authenticated_client, sec_db, monkeypatch, tmp_path
):
    from pullbox.models.creator import Creator
    from pullbox.utilities.comicinfo_creators import load_comicinfo_creator_fields
    from tests.unit.test_gcd_local_credits import EXPECTED, credited_dump

    path = credited_dump(tmp_path / "gcd.db")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert (await activate(authenticated_client, path)).status_code == 200
    preview = await authenticated_client.post(
        "/api/v1/metadata/series/preview",
        json={"source": "gcd_local", "external_id": "2999"},
        headers=csrf(authenticated_client),
    )
    assert preview.status_code == 200
    issues = preview.json()["issues"]["data"]["results"]
    assert issues[0]["credits"] == EXPECTED
    assert issues[2]["credits"] is None
    root_path = tmp_path / "library"
    root_path.mkdir()
    async with sec_db.begin() as session:
        root = LibraryRoot(name="Test", path=str(root_path), allow_managed_writes=True)
        session.add(root)
        await session.flush()
        root_id = root.id
    monkeypatch.setattr("pullbox.api.v1.series.get_event_bus", EventBus)
    result = await authenticated_client.post(
        "/api/v1/series",
        json={
            "source": "gcd_local",
            "external_id": "2999",
            "source_revision": 1,
            "library_root_id": root_id,
        },
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 201, result.text
    async with sec_db() as session:
        issue_id = await session.scalar(select(Issue.id).order_by(Issue.id))
        assert (await load_comicinfo_creator_fields(session, issue_id))["Writer"] == "Alan Moore"
        assert all(item.comicvine_id is None for item in await session.scalars(select(Creator)))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert not [item for item in root_path.rglob("*") if item.is_file()], (
        "Metadata Add may create its series directory, but must not create comic files"
    )


@pytest.mark.parametrize("kind", ["missing", "symlink", "incompatible"])
async def test_invalid_gcd_candidates_fail_without_leaking_paths(
    authenticated_client, tmp_path, kind
):
    path = tmp_path / "candidate.db"
    if kind == "symlink":
        path.symlink_to(gcd_dump(tmp_path / "source.db"))
    elif kind == "incompatible":
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE unrelated (id INTEGER)")
    response = await activate(authenticated_client, path)
    assert response.status_code == 400
    assert str(tmp_path) not in response.text


@pytest.fixture
async def watchmen_catalog(authenticated_client, sec_db, monkeypatch, tmp_path):
    path = gcd_dump(tmp_path / "gcd.db")
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO gcd_series VALUES "
            "(3172, 'Watchmen', 'Watchmen', 1986, 1987, 1, 1, 13, 0, 0, 1, '')"
        )
        db.executemany(
            "INSERT INTO gcd_issue VALUES (?,3172,?,'',?,0,NULL,'','','',32)",
            [(41814 + n, str(n), n) for n in range(1, 13)] + [(647784, "1 [2nd Printing]", 13)],
        )
    assert (await activate(authenticated_client, path)).status_code == 200
    root_path = tmp_path / "library"
    root_path.mkdir()
    async with sec_db.begin() as session:
        root = LibraryRoot(name="Test", path=str(root_path), allow_managed_writes=True)
        session.add(root)
        await session.flush()
        root_id = root.id
    monkeypatch.setattr("pullbox.api.v1.series.get_event_bus", EventBus)
    return (
        path,
        root_path,
        {
            "source": "gcd_local",
            "external_id": "3172",
            "source_revision": 1,
            "library_root_id": root_id,
        },
    )


async def catalog_preview(client, body):
    return await client.post(
        "/api/v1/metadata/series/preview",
        json={key: value for key, value in body.items() if key != "source_revision"},
        headers=csrf(client),
    )


async def test_gcd_exception_review_add_and_refresh_preserve_exact_catalog_decision(
    authenticated_client, sec_db, watchmen_catalog
):
    from pullbox.models.metadata_identity import IssueExternalIdentity

    path, root, body = watchmen_catalog
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    preview = await catalog_preview(authenticated_client, body)
    assert preview.status_code == 200, preview.text
    review = preview.json().get("catalog_review")
    assert review is not None, "GCD Add must expose a review, not fail only after Add"
    assert review["total"] == 13 and review["supported_count"] == 12
    assert [(row["external_id"], row["issue_number_text"]) for row in review["excluded"]] == [
        ("647784", "1 [2nd Printing]")
    ]
    assert len(review["token"]) == 64
    assert not list(root.iterdir())
    rejected = await authenticated_client.post(
        "/api/v1/series", json=body, headers=csrf(authenticated_client)
    )
    assert rejected.status_code == 409 and not list(root.iterdir())
    approved = {**body, "catalog_review_token": review["token"]}
    added = await authenticated_client.post(
        "/api/v1/series", json=approved, headers=csrf(authenticated_client)
    )
    assert added.status_code == 201, added.text
    data = added.json()
    assert data["issue_count"] == 12
    assert data["catalog_exclusions"][0]["external_id"] == "647784"
    async with sec_db() as session:
        ids = set(await session.scalars(select(IssueExternalIdentity.external_id)))
        assert ids == {str(41814 + n) for n in range(1, 13)}
        assert await session.scalar(select(func.count()).select_from(Issue)) == 12
    detail = await authenticated_client.get(f"/series/{data['id']}")
    assert "1 [2nd Printing]" in detail.text and "647784" in detail.text
    refreshed = await authenticated_client.post(
        f"/api/v1/series/{data['id']}/refresh", headers=csrf(authenticated_client)
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["issue_count"] == 12
    assert refreshed.json()["catalog_exclusions"] == data["catalog_exclusions"]
    again = await authenticated_client.post(
        "/api/v1/series", json=approved, headers=csrf(authenticated_client)
    )
    assert again.status_code == 201 and again.json()["id"] == data["id"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


async def test_stale_or_forged_catalog_review_cannot_create_library(
    authenticated_client, sec_db, watchmen_catalog
):
    path, root, body = watchmen_catalog
    preview = await catalog_preview(authenticated_client, body)
    review = preview.json().get("catalog_review")
    assert review is not None
    for token in ("f" * 64,):
        result = await authenticated_client.post(
            "/api/v1/series",
            json={**body, "catalog_review_token": token},
            headers=csrf(authenticated_client),
        )
        assert result.status_code == 409
    with sqlite3.connect(path) as db:
        db.execute("UPDATE gcd_issue SET number='1 [3rd Printing]' WHERE id=647784")
    assert (await activate(authenticated_client, path, revision=1)).status_code == 200
    result = await authenticated_client.post(
        "/api/v1/series",
        json={**body, "source_revision": 2, "catalog_review_token": review["token"]},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 409
    assert not list(root.iterdir())
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(Series)) == 0


async def test_gcd_review_includes_exceptions_after_the_first_page(
    authenticated_client, watchmen_catalog
):
    path, _, body = watchmen_catalog
    with sqlite3.connect(path) as db:
        db.execute("UPDATE gcd_issue SET sort_code=500 WHERE id=647784")
        db.executemany(
            "INSERT INTO gcd_issue VALUES (?,3172,?,'',?,0,NULL,'','','',32)",
            [(50000 + n, str(n), n) for n in range(13, 205)],
        )
    assert (await activate(authenticated_client, path, revision=1)).status_code == 200
    preview = await catalog_preview(authenticated_client, body)
    review = preview.json().get("catalog_review")
    assert review is not None
    assert review["supported_count"] == 204 and review["total"] == 205
    assert review["excluded"][0]["external_id"] == "647784"


@pytest.mark.parametrize("change", ["new_exception", "changed_exception", "duplicate_number"])
async def test_refresh_rejects_unreviewed_changes_without_changing_owned_files(
    authenticated_client, sec_db, watchmen_catalog, change
):
    from pullbox.models.issue import IssueStatus

    path, root, body = watchmen_catalog
    review = (await catalog_preview(authenticated_client, body)).json()["catalog_review"]
    added = await authenticated_client.post(
        "/api/v1/series",
        json={**body, "catalog_review_token": review["token"]},
        headers=csrf(authenticated_client),
    )
    assert added.status_code == 201
    series_id = added.json()["id"]
    marker = root / "owned.cbz"
    marker.write_bytes(b"existing comic pages")
    async with sec_db.begin() as session:
        series = await session.get(Series, series_id)
        series.title = "My Watchmen title"
        issue = await session.scalar(select(Issue).order_by(Issue.id))
        issue.status = IssueStatus.OWNED
        issue_id = issue.id
    with sqlite3.connect(path) as db:
        if change == "changed_exception":
            db.execute("UPDATE gcd_issue SET number='1 [3rd Printing]' WHERE id=647784")
        else:
            db.execute(
                "INSERT INTO gcd_issue VALUES (888888,3172,?,'',500,0,NULL,'','','',32)",
                ("nn" if change == "new_exception" else "1",),
            )
    assert (await activate(authenticated_client, path, revision=1)).status_code == 200
    refreshed = await authenticated_client.post(
        f"/api/v1/series/{series_id}/refresh", headers=csrf(authenticated_client)
    )
    assert refreshed.status_code == 409
    async with sec_db() as session:
        series = await session.get(Series, series_id)
        assert series.title == "My Watchmen title" and series.issue_count == 12
        assert series.catalog_exclusions == added.json()["catalog_exclusions"]
        assert (await session.get(Issue, issue_id)).status == IssueStatus.OWNED
        assert await session.scalar(select(func.count()).select_from(Issue)) == 12
    assert marker.read_bytes() == b"existing comic pages"


async def test_all_unsupported_catalog_cannot_be_added(authenticated_client, watchmen_catalog):
    path, root, body = watchmen_catalog
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM gcd_issue WHERE series_id=3172 AND id != 647784")
    assert (await activate(authenticated_client, path, revision=1)).status_code == 200
    body["source_revision"] = 2
    review = (await catalog_preview(authenticated_client, body)).json()["catalog_review"]
    assert review["supported_count"] == 0
    result = await authenticated_client.post(
        "/api/v1/series",
        json={**body, "catalog_review_token": review["token"]},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 409 and not list(root.iterdir())
