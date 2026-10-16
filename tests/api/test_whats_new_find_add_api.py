"""Explicit release selection uses normal Add Series and durable exact links."""
# ruff: noqa: F811 - pytest fixture imported from the owning Add API tests.

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from pullbox.config import get_settings
from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models import Series
from pullbox.models.metadata_identity import SeriesExternalIdentity
from pullbox.models.whats_new import WhatsNewCacheKind, WhatsNewReleaseCache
from tests.api.test_metadata_sources_api import csrf
from tests.api.test_source_series_add_api import source_add_setup  # noqa: F401
from tests.ui.test_whats_new_ui_routes import _issue_summary

pytest_plugins = ["conftest_security"]


@pytest.fixture
def actions_enabled(monkeypatch):
    monkeypatch.setenv("PULLBOX_METADATA_WHATS_NEW_ACTIONS_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def release_cache(sec_db, actions_enabled):
    now = datetime.now(UTC)
    async with sec_db.begin() as session:
        row = WhatsNewReleaseCache(
            cache_key="current-week:2026-03-11",
            cache_kind=WhatsNewCacheKind.CURRENT_WEEK,
            store_date=date(2026, 3, 11),
            payload={"store_date": "2026-03-11", "issues": [_issue_summary()], "count": 1},
            fetched_at=now,
            last_successful_refresh_at=now,
        )
        session.add(row)
        await session.flush()
        return row.id


async def context(client, cache_id):
    response = await client.get(f"/api/v1/whats-new/resolve/{cache_id}/1514020")
    assert response.status_code == 200, response.text
    return response.json()["selection"]


async def test_confirmed_find_add_is_atomic_repeat_safe_and_not_title_matching(
    authenticated_client, source_add_setup, release_cache, sec_db
):
    fixture = source_add_setup
    selected = await context(authenticated_client, release_cache)
    before = await authenticated_client.get("/whats-new")
    assert "Find &amp; Add" in before.text and "whats-new-local-series" not in before.text
    body = {**fixture["body"], "whats_new_selection": selected}
    response = await authenticated_client.post(
        "/api/v1/series", json=body, headers=csrf(authenticated_client)
    )
    assert response.status_code == 201, response.text
    series_id = response.json()["id"]
    again = await authenticated_client.post(
        "/api/v1/series", json=body, headers=csrf(authenticated_client)
    )
    assert again.status_code == 201 and again.json()["id"] == series_id
    async with sec_db() as session:
        claim = await session.scalar(
            select(SeriesExternalIdentity).where(
                SeriesExternalIdentity.identity_namespace == IdentityNamespace.LOCG
            )
        )
        assert claim.series_id == series_id and claim.external_id == "180901"
        assert claim.verification_state is IdentityVerificationState.VERIFIED
        assert await session.scalar(select(func.count()).select_from(Series)) == 1
    assert len(fixture["events"]) == 1
    after = await authenticated_client.get("/whats-new")
    assert 'data-testid="whats-new-local-series"' in after.text and "Tracked" in after.text
    assert f'href="/series/{series_id}"' in after.text


async def test_stale_release_selection_creates_no_series_or_folder(
    authenticated_client, source_add_setup, release_cache, sec_db
):
    selected = await context(authenticated_client, release_cache)
    async with sec_db.begin() as session:
        row = await session.get(WhatsNewReleaseCache, release_cache)
        changed = _issue_summary()
        changed["series"] = {**changed["series"], "locg_series_id": 999}
        changed["locg_series_id"] = 999
        row.payload = {**row.payload, "issues": [changed]}
    result = await authenticated_client.post(
        "/api/v1/series",
        json={**source_add_setup["body"], "whats_new_selection": selected},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 409
    assert not source_add_setup["calls"] and not source_add_setup["events"]
    assert not list(source_add_setup["root"].iterdir())


async def test_existing_locg_owner_is_not_reassigned_by_find_add(
    authenticated_client, source_add_setup, release_cache, sec_db
):
    selected = await context(authenticated_client, release_cache)
    async with sec_db.begin() as session:
        original = Series(title="Original", sort_title="original", monitored=False)
        session.add(original)
        await session.flush()
        session.add(
            SeriesExternalIdentity(
                series_id=original.id,
                identity_namespace=IdentityNamespace.LOCG,
                external_id="180901",
                verification_state=IdentityVerificationState.VERIFIED,
                evidence_kind="user_selection",
            )
        )
        original_id = original.id
    result = await authenticated_client.post(
        "/api/v1/series",
        json={**source_add_setup["body"], "whats_new_selection": selected},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 409
    assert not source_add_setup["events"] and not list(source_add_setup["root"].iterdir())
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(Series)) == 1
        assert (await session.scalar(select(SeriesExternalIdentity))).series_id == original_id
    page = await authenticated_client.get("/whats-new")
    assert "Added" in page.text and "Paused" in page.text


async def test_resolver_requires_auth_and_does_not_make_provider_calls(
    unauthenticated_client, authenticated_client, source_add_setup, release_cache
):
    path = f"/api/v1/whats-new/resolve/{release_cache}/1514020"
    assert (await unauthenticated_client.get(path)).status_code in {401, 403}
    response = await authenticated_client.get(path)
    assert response.status_code == 200
    assert response.json()["title"] == "Absolute Flash"
    assert not source_add_setup["calls"]


async def test_feature_off_rejects_release_link_before_provider_work(
    authenticated_client, source_add_setup, release_cache, monkeypatch
):
    selected = await context(authenticated_client, release_cache)
    monkeypatch.setenv("PULLBOX_METADATA_WHATS_NEW_ACTIONS_ENABLED", "false")
    get_settings.cache_clear()
    response = await authenticated_client.post(
        "/api/v1/series",
        json={**source_add_setup["body"], "whats_new_selection": selected},
        headers=csrf(authenticated_client),
    )
    assert response.status_code == 404
    assert not source_add_setup["calls"]


async def test_release_changed_during_provider_io_rolls_back_add_and_event(
    authenticated_client, source_add_setup, release_cache, sec_db, monkeypatch
):
    from pullbox.api.v1 import series as routes

    selected = await context(authenticated_client, release_cache)
    fetch = routes.fetch_source_series_bundle

    async def changed_after_fetch(*args, **kwargs):
        result = await fetch(*args, **kwargs)
        async with sec_db.begin() as session:
            row = await session.get(WhatsNewReleaseCache, release_cache)
            changed = _issue_summary()
            changed["series"] = {**changed["series"], "title": "Changed"}
            row.payload = {**row.payload, "issues": [changed]}
        return result

    monkeypatch.setattr(routes, "fetch_source_series_bundle", changed_after_fetch)
    result = await authenticated_client.post(
        "/api/v1/series",
        json={**source_add_setup["body"], "whats_new_selection": selected},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 409
    assert source_add_setup["calls"] and not source_add_setup["events"]
    assert not list(source_add_setup["root"].iterdir())
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(Series)) == 0


async def test_release_title_never_claims_an_existing_series(
    authenticated_client, release_cache, sec_db
):
    async with sec_db.begin() as session:
        session.add(Series(title="Absolute Flash", sort_title="absolute flash", year_start=2026))
    page = await authenticated_client.get("/whats-new")
    assert "Find &amp; Add" in page.text
    assert 'data-testid="whats-new-local-series"' not in page.text
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(SeriesExternalIdentity)) == 0


async def test_sparse_release_adds_series_without_inventing_locg_identity(
    authenticated_client, source_add_setup, release_cache, sec_db
):
    async with sec_db.begin() as session:
        row = await session.get(WhatsNewReleaseCache, release_cache)
        sparse = _issue_summary()
        sparse["series"] = {**sparse["series"], "locg_series_id": None}
        sparse["locg_series_id"] = None
        row.payload = {**row.payload, "issues": [sparse]}
    selected = await context(authenticated_client, release_cache)
    result = await authenticated_client.post(
        "/api/v1/series",
        json={**source_add_setup["body"], "whats_new_selection": selected},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 201
    async with sec_db() as session:
        assert (
            await session.scalar(
                select(SeriesExternalIdentity).where(
                    SeriesExternalIdentity.identity_namespace == IdentityNamespace.LOCG
                )
            )
            is None
        )
    page = await authenticated_client.get("/whats-new")
    assert 'data-testid="whats-new-local-series"' not in page.text


async def test_discovery_requires_interactive_user_and_csrf(
    authenticated_client, unauthenticated_client, sec_api_key, source_add_setup, release_cache
):
    selected = await context(authenticated_client, release_cache)
    body = {**source_add_setup["body"], "whats_new_selection": selected}
    result = await authenticated_client.post("/api/v1/series", json=body)
    assert result.status_code == 403
    key_headers = {"X-API-Key": sec_api_key}
    for url in [
        f"/api/v1/whats-new/resolve/{release_cache}/1514020",
        f"/whats-new/find-series/{release_cache}/1514020?q=Batman",
    ]:
        assert (await unauthenticated_client.get(url, headers=key_headers)).status_code in {
            401,
            403,
        }
    result = await unauthenticated_client.post("/api/v1/series", json=body, headers=key_headers)
    assert result.status_code in {401, 403}
    assert not source_add_setup["calls"]


@pytest.mark.parametrize("cache_id,release_id", [(2**31, 1514020), (1, 2**63)])
async def test_release_locator_is_bounded_before_database_binding(
    authenticated_client, actions_enabled, cache_id, release_id
):
    result = await authenticated_client.get(f"/api/v1/whats-new/resolve/{cache_id}/{release_id}")
    assert result.status_code == 422
