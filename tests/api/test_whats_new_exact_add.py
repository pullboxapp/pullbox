"""What's New direct Add trusts the cached release and full catalog, not a title or client ID."""
# ruff: noqa: F811 - fixtures shared with the existing operator API tests.

import json
import re
from datetime import UTC, datetime, timedelta
from html import unescape
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from pullbox.config import get_settings
from pullbox.core.events import EventBus
from pullbox.models import Issue, LibraryRoot, Series, SeriesExternalIdentity
from pullbox.models.config import SystemConfig
from pullbox.models.whats_new import WhatsNewReleaseCache
from pullbox.services.whats_new_cache_service import WhatsNewCacheService
from tests.api.test_metadata_sources_api import csrf
from tests.api.test_source_series_add_api import source_add_setup  # noqa: F401
from tests.api.test_whats_new_find_add_api import actions_enabled, release_cache  # noqa: F401

pytest_plugins = ["conftest_security"]


@pytest.fixture
async def published(sec_db, release_cache, source_add_setup):
    async with sec_db.begin() as db:
        row = await db.get(WhatsNewReleaseCache, release_cache)
        issue = row.payload["issues"][0]
        issue = {
            **issue,
            "series": {
                **issue["series"],
                "publication_state": "published",
                "publication_as_of": datetime.now(UTC).date().isoformat(),
                "metron_series_id": 8,
            },
        }
        row.payload = {**row.payload, "issues": [issue]}
        await WhatsNewCacheService().upsert_upcoming(db, payload={"weeks": []})
        (
            await db.get(LibraryRoot, source_add_setup["body"]["library_root_id"])
        ).is_default_managed_destination = True
    return release_cache


async def offer(client):
    page = await client.get("/whats-new")
    assert page.status_code == 200
    assert "data-discovery-find-add" in page.text
    button = re.search(r"""data-release-offer='([^']+)'""", page.text)
    return json.loads(unescape(button[1])) if button else None


async def submit(client, body):
    return await client.post("/api/v1/whats-new/add-series", json=body, headers=csrf(client))


async def test_exact_id_on_another_week_for_the_same_published_series(
    authenticated_client, sec_db, published, source_add_setup
):
    async with sec_db.begin() as db:
        current = await db.get(WhatsNewReleaseCache, published)
        issue = current.payload["issues"][0]
        current.payload = {
            **current.payload,
            "issues": [{**issue, "series": {**issue["series"], "metron_series_id": None}}],
        }
        sibling = {**issue, "locg_issue_id": 50005001, "store_date": "2099-01-01"}
        await WhatsNewCacheService().upsert_upcoming(
            db,
            payload={
                "weeks": [
                    {"store_date": "2098-01-01", "issues": []},
                    {"store_date": "2099-01-01", "issues": [sibling]},
                ]
            },
        )
    page = await authenticated_client.get("/whats-new?release_week=2098-01-01")
    assert page.is_success
    offers = [
        json.loads(unescape(value))
        for value in re.findall(r"""data-release-offer='([^']+)'""", page.text)
    ]
    assert any(item["cache_id"] == published and item["release_id"] == 1514020 for item in offers)
    assert not source_add_setup["calls"]


@pytest.mark.parametrize("monitor", [False, True])
async def test_exact_add_from_release_uses_normal_policy_and_is_repeat_safe(
    authenticated_client, sec_db, published, source_add_setup, monitor, monkeypatch
):
    emit = AsyncMock()
    monkeypatch.setattr(EventBus, "emit", emit)
    async with sec_db.begin() as db:
        (await db.get(SystemConfig, "search_on_add_default")).value = str(monitor).lower()
    selected = await offer(authenticated_client)
    assert selected and not source_add_setup["calls"]
    result = await submit(authenticated_client, selected)
    assert result.is_success, result.text
    assert result.json()["monitored"] is monitor and result.json()["locg_series_id"] == "180901"
    calls = len(source_add_setup["calls"])
    assert calls > 0
    again = await submit(authenticated_client, selected)
    assert again.is_success and again.json() == result.json()
    assert len(source_add_setup["calls"]) == calls
    emit.assert_awaited_once()
    async with sec_db() as db:
        assert await db.scalar(select(func.count()).select_from(Series)) == 1
        assert await db.scalar(select(func.count()).select_from(Issue)) == 3
        assert {
            (r.identity_namespace.value, r.external_id)
            for r in await db.scalars(select(SeriesExternalIdentity))
        } == {("metron", "8"), ("locg", "180901")}
        link = await db.scalar(
            select(SeriesExternalIdentity).where(SeriesExternalIdentity.external_id == "180901")
        )
        origin = json.loads(link.evidence_locator)["origin"]
        assert (
            origin["record_kind"] == "locg_release"
            and origin["record_id"] == selected["release_id"]
        )
    assert (
        'data-testid="whats-new-local-series"'
        in (await authenticated_client.get("/whats-new")).text
    )


@pytest.mark.parametrize(
    "reason",
    [
        "conflicting_feed",
        "unpublished",
        "no_id",
        "no_default",
        "future_cache",
        "stale_cache",
        "missing_catalog",
    ],
)
async def test_changed_release_never_uses_stale_direct_add(
    authenticated_client, sec_db, published, source_add_setup, reason, monkeypatch
):
    selected = await offer(authenticated_client)
    assert selected
    async with sec_db.begin() as db:
        row = await db.get(WhatsNewReleaseCache, published)
        issue = row.payload["issues"][0]
        if reason == "missing_catalog":
            from pullbox.services.catalog import reader

            monkeypatch.setattr(
                reader, "get_catalog_reader", lambda: reader.CatalogReader(source_add_setup["root"])
            )
            row.payload = {
                **row.payload,
                "issues": [
                    {
                        **issue,
                        "series": {
                            **issue["series"],
                            "metron_series_id": None,
                            "comicvine_series_id": 10,
                        },
                    }
                ],
            }
        elif reason in {"future_cache", "stale_cache"}:
            row.fetched_at = datetime.now(UTC) + timedelta(
                days=1 if reason == "future_cache" else -1
            )
        elif reason == "no_default":
            (
                await db.get(LibraryRoot, source_add_setup["body"]["library_root_id"])
            ).is_default_managed_destination = False
        elif reason == "conflicting_feed":
            other = {**issue, "series": {**issue["series"], "metron_series_id": 9}}
            await WhatsNewCacheService().upsert_upcoming(
                db, payload={"weeks": [{"issues": [other]}]}
            )
        else:
            fields = (
                {"metron_series_id": None}
                if reason == "no_id"
                else {"publication_state": "unknown"}
            )
            row.payload = {
                **row.payload,
                "issues": [{**issue, "series": {**issue["series"], **fields}}],
            }
    assert await offer(authenticated_client) is None
    response = await submit(authenticated_client, selected)
    assert response.status_code == 409 and "Find & Add" in response.text
    assert not source_add_setup["calls"] and not list(source_add_setup["root"].iterdir())


async def test_exact_add_is_operator_only_and_browser_cannot_substitute_ids(
    authenticated_client,
    unauthenticated_client,
    sec_api_key,
    published,
    source_add_setup,
    monkeypatch,
):
    body = await offer(authenticated_client)
    url = "/api/v1/whats-new/add-series"
    assert (await authenticated_client.post(url, json=body)).status_code == 403
    assert (
        await unauthenticated_client.post(url, json=body, headers={"X-API-Key": sec_api_key})
    ).status_code in (401, 403)
    assert (await submit(authenticated_client, {**body, "metron_series_id": 9})).status_code == 422
    assert (
        await submit(authenticated_client, {**body, "fingerprint": "0" * 64})
    ).status_code == 409
    monkeypatch.setenv("PULLBOX_METADATA_WHATS_NEW_ACTIONS_ENABLED", "false")
    get_settings.cache_clear()
    assert (await submit(authenticated_client, body)).status_code == 404
    assert not source_add_setup["calls"]


async def test_submitted_offer_cannot_override_conflicting_local_owner(
    authenticated_client, published, source_add_setup, sec_db
):
    selected = await offer(authenticated_client)
    async with sec_db.begin() as db:
        series = Series(title="Other edition", sort_title="other edition")
        db.add(series)
        await db.flush()
        for ns, external in [("metron", "8"), ("locg", "222")]:
            db.add(
                SeriesExternalIdentity(
                    series_id=series.id,
                    identity_namespace=ns,
                    external_id=external,
                    verification_state="verified",
                    evidence_kind="user_selection",
                )
            )
    assert await offer(authenticated_client) is None
    response = await submit(authenticated_client, selected)
    assert response.status_code == 409
    assert not source_add_setup["calls"]


@pytest.mark.parametrize("change", ["other_feed", "setting", "catalog_disagrees"])
async def test_exact_add_rechecks_evidence_after_provider_read(
    authenticated_client, sec_db, published, source_add_setup, monkeypatch, change
):
    from pullbox.services import whats_new_exact_add as command

    emit = AsyncMock()
    monkeypatch.setattr(EventBus, "emit", emit)
    if change == "catalog_disagrees":
        async with sec_db.begin() as db:
            row = await db.get(WhatsNewReleaseCache, published)
            issue = row.payload["issues"][0]
            row.payload = {
                **row.payload,
                "issues": [{**issue, "series": {**issue["series"], "gcd_series_id": 345}}],
            }
        source_add_setup["profile"]["gcd_id"] = 346
    fetch = command.fetch_published_catalog

    async def fetch_then_change(*args):
        bundle = await fetch(*args)
        if change == "other_feed":
            async with sec_db.begin() as db:
                row = await db.get(WhatsNewReleaseCache, published)
                issue = row.payload["issues"][0]
                other = {**issue, "series": {**issue["series"], "metron_series_id": 99}}
                await WhatsNewCacheService().upsert_upcoming(
                    db, payload={"weeks": [{"issues": [other]}]}
                )
        elif change == "setting":
            monkeypatch.setenv("PULLBOX_METADATA_WHATS_NEW_ACTIONS_ENABLED", "false")
            get_settings.cache_clear()
        return bundle

    monkeypatch.setattr(command, "fetch_published_catalog", fetch_then_change)
    response = await submit(authenticated_client, await offer(authenticated_client))
    assert response.status_code in (404, 409), response.text
    assert source_add_setup["calls"] and not list(source_add_setup["root"].iterdir())
    emit.assert_not_awaited()
    async with sec_db() as db:
        assert await db.scalar(select(func.count()).select_from(Series)) == 0
