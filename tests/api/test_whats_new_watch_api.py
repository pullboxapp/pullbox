"""Watch is cache-independent intent, not a placeholder library or download."""
# ruff: noqa: F811 - fixtures imported from the owning discovery tests.

from datetime import date

import pytest
from sqlalchemy import func, select

from pullbox.config import get_settings
from pullbox.core.metadata_identity import IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models import Series
from pullbox.models.config import SystemConfig
from pullbox.models.library import LibraryRoot
from pullbox.models.metadata_identity import SeriesExternalIdentity
from pullbox.models.whats_new import WhatsNewReleaseCache
from tests.api.test_metadata_sources_api import csrf
from tests.api.test_source_series_add_api import source_add_setup  # noqa: F401
from tests.api.test_whats_new_find_add_api import (
    actions_enabled,  # noqa: F401
    context,
    release_cache,  # noqa: F401
)
from tests.ui.test_whats_new_ui_routes import _issue_summary

pytest_plugins = ["conftest_security"]


@pytest.fixture
async def future_release(release_cache, sec_db, source_add_setup):
    async with sec_db.begin() as session:
        row = await session.get(WhatsNewReleaseCache, release_cache)
        release = _issue_summary()
        release["store_date"] = "2099-01-01"
        row.store_date = date(2099, 1, 1)
        row.payload = {**row.payload, "store_date": "2099-01-01", "issues": [release]}
        root = await session.get(LibraryRoot, source_add_setup["body"]["library_root_id"])
        root.is_default_managed_destination = True
    return release_cache


async def watch(client, cache_id, root_id=None):
    body = {"selection": await context(client, cache_id)}
    if root_id is not None:
        body["library_root_id"] = root_id
    return await client.post("/api/v1/whats-new/watch", json=body, headers=csrf(client))


async def test_watch_repeat_cache_eviction_cancel_and_pull_list_are_durable(
    authenticated_client, source_add_setup, future_release, sec_db
):
    result = await watch(authenticated_client, future_release)
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved["state"] == "watching" and saved["locg_series_id"] == "180901"
    again = await watch(authenticated_client, future_release)
    assert again.status_code == 200 and again.json()["id"] == saved["id"]
    async with sec_db.begin() as session:
        assert await session.scalar(select(func.count()).select_from(Series)) == 0
        await session.delete(await session.get(WhatsNewReleaseCache, future_release))
    pull_list = await authenticated_client.get("/pull-list")
    assert 'data-testid="pull-list-watching"' in pull_list.text
    assert "Absolute Flash" in pull_list.text and "Watching" in pull_list.text
    assert not source_add_setup["calls"] and not source_add_setup["events"]
    assert not list(source_add_setup["root"].iterdir())
    for _ in range(2):
        cancelled = await authenticated_client.post(
            f"/api/v1/whats-new/watch/{saved['id']}/cancel", headers=csrf(authenticated_client)
        )
        assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    assert "Absolute Flash" not in (await authenticated_client.get("/pull-list")).text


async def test_watch_explicit_find_add_promotes_and_always_monitors(
    authenticated_client, source_add_setup, future_release, sec_db
):
    async with sec_db.begin() as session:
        setting = await session.get(SystemConfig, "search_on_add_default")
        setting.value = "false"
    saved = await watch(authenticated_client, future_release)
    assert saved.status_code == 200, saved.text
    selection = await context(authenticated_client, future_release)
    result = await authenticated_client.post(
        "/api/v1/series",
        json={**source_add_setup["body"], "whats_new_selection": selection},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 201, result.text
    assert result.json()["monitored"] is True and result.json()["wanted_count"] == 3
    assert (
        'data-testid="pull-list-watch-row"'
        not in (await authenticated_client.get("/pull-list")).text
    )
    cancelled = await authenticated_client.post(
        f"/api/v1/whats-new/watch/{saved.json()['id']}/cancel", headers=csrf(authenticated_client)
    )
    assert cancelled.status_code == 409
    async with sec_db() as session:
        series = await session.get(Series, result.json()["id"])
        assert series.monitored is True


@pytest.mark.parametrize("reason", ["sparse", "past", "root", "owner", "stale"])
async def test_watch_refuses_unsafe_or_unavailable_context(
    authenticated_client, source_add_setup, future_release, sec_db, reason
):
    selected = await context(authenticated_client, future_release)
    async with sec_db.begin() as session:
        row = await session.get(WhatsNewReleaseCache, future_release)
        release = _issue_summary()
        release["store_date"] = "2099-01-01"
        if reason == "sparse":
            release["series"] = {**release["series"], "locg_series_id": None}
            release["locg_series_id"] = None
        elif reason == "past":
            release["store_date"] = "2000-01-01"
        elif reason == "stale":
            release["series"] = {**release["series"], "title": "Changed"}
        elif reason == "owner":
            owner = Series(title="Exact owner", sort_title="owner")
            session.add(owner)
            await session.flush()
            session.add(
                SeriesExternalIdentity(
                    series_id=owner.id,
                    identity_namespace=IdentityNamespace.LOCG,
                    external_id="180901",
                    verification_state=IdentityVerificationState.VERIFIED,
                    evidence_kind="user_selection",
                )
            )
        row.payload = {**row.payload, "issues": [release]}
    if reason in {"sparse", "past"}:
        selected = await context(authenticated_client, future_release)
    result = await authenticated_client.post(
        "/api/v1/whats-new/watch",
        json={"selection": selected, **({"library_root_id": 99999} if reason == "root" else {})},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 409, result.text
    assert not source_add_setup["calls"] and not source_add_setup["events"]
    assert not list(source_add_setup["root"].iterdir())


async def test_watch_requires_interactive_auth_csrf_and_feature_flag(
    authenticated_client,
    unauthenticated_client,
    sec_api_key,
    source_add_setup,
    future_release,
    monkeypatch,
):
    body = {"selection": await context(authenticated_client, future_release)}
    assert (
        await authenticated_client.post("/api/v1/whats-new/watch", json=body)
    ).status_code == 403
    denied = await unauthenticated_client.post(
        "/api/v1/whats-new/watch", json=body, headers={"X-API-Key": sec_api_key}
    )
    assert denied.status_code in {401, 403}
    monkeypatch.setenv("PULLBOX_METADATA_WHATS_NEW_ACTIONS_ENABLED", "false")
    get_settings.cache_clear()
    result = await authenticated_client.post(
        "/api/v1/whats-new/watch", json=body, headers=csrf(authenticated_client)
    )
    assert result.status_code == 404


async def test_cancel_during_provider_fetch_does_not_force_monitoring(
    authenticated_client, source_add_setup, future_release, sec_db, monkeypatch
):
    from pullbox.api.v1 import series as routes

    async with sec_db.begin() as session:
        (await session.get(SystemConfig, "search_on_add_default")).value = "false"
    saved = (await watch(authenticated_client, future_release)).json()
    selected = await context(authenticated_client, future_release)
    fetch = routes.fetch_source_series_bundle

    async def cancel_after_fetch(*args, **kwargs):
        result = await fetch(*args, **kwargs)
        response = await authenticated_client.post(
            f"/api/v1/whats-new/watch/{saved['id']}/cancel", headers=csrf(authenticated_client)
        )
        assert response.status_code == 200
        return result

    monkeypatch.setattr(routes, "fetch_source_series_bundle", cancel_after_fetch)
    result = await authenticated_client.post(
        "/api/v1/series",
        json={**source_add_setup["body"], "whats_new_selection": selected},
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 201 and result.json()["monitored"] is False


async def test_watch_without_default_requires_explicit_safe_destination(
    authenticated_client, source_add_setup, future_release, sec_db
):
    root_id = source_add_setup["body"]["library_root_id"]
    async with sec_db.begin() as session:
        (await session.get(LibraryRoot, root_id)).is_default_managed_destination = False
    assert (await watch(authenticated_client, future_release)).status_code == 409
    result = await watch(authenticated_client, future_release, root_id)
    assert result.status_code == 200 and result.json()["library_root_id"] == root_id
    resolver = await authenticated_client.get(f"/api/v1/whats-new/resolve/{future_release}/1514020")
    assert resolver.json()["watch_default_id"] is None
    assert resolver.json()["roots"][0]["id"] == root_id


async def test_changed_watch_destination_rolls_back_entire_add(
    authenticated_client, source_add_setup, future_release, sec_db, tmp_path
):
    saved = await watch(authenticated_client, future_release)
    assert saved.status_code == 200
    other = tmp_path / "other"
    other.mkdir()
    async with sec_db.begin() as session:
        root = LibraryRoot(name="Other", path=str(other), allow_managed_writes=True)
        session.add(root)
        await session.flush()
        other_id = root.id
    selected = await context(authenticated_client, future_release)
    result = await authenticated_client.post(
        "/api/v1/series",
        json={
            **source_add_setup["body"],
            "library_root_id": other_id,
            "whats_new_selection": selected,
        },
        headers=csrf(authenticated_client),
    )
    assert result.status_code == 409 and "watched destination" in result.text
    assert not source_add_setup["events"] and not list(other.iterdir())
    async with sec_db() as session:
        assert await session.scalar(select(func.count()).select_from(Series)) == 0
        assert await session.scalar(select(func.count()).select_from(SeriesExternalIdentity)) == 0


async def test_watch_cancel_and_rewatch_reuses_intent_and_requires_csrf(
    authenticated_client, source_add_setup, future_release
):
    saved = (await watch(authenticated_client, future_release)).json()
    url = f"/api/v1/whats-new/watch/{saved['id']}/cancel"
    assert (await authenticated_client.post(url)).status_code == 403
    assert (
        await authenticated_client.post(url, headers=csrf(authenticated_client))
    ).status_code == 200
    repeated = await watch(authenticated_client, future_release)
    assert repeated.status_code == 200 and repeated.json()["id"] == saved["id"]
    assert repeated.json()["state"] == "watching"


async def test_repeated_watch_rejects_a_different_destination_without_changing_intent(
    authenticated_client, source_add_setup, future_release, sec_db, tmp_path
):
    saved = (await watch(authenticated_client, future_release)).json()
    other = tmp_path / "other-watch-root"
    other.mkdir()
    async with sec_db.begin() as session:
        root = LibraryRoot(name="Other", path=str(other), allow_managed_writes=True)
        session.add(root)
        await session.flush()
        other_id = root.id
    changed = await watch(authenticated_client, future_release, other_id)
    assert changed.status_code == 409 and "already watches" in changed.text
    unchanged = (await watch(authenticated_client, future_release)).json()
    assert (
        unchanged["id"] == saved["id"] and unchanged["library_root_id"] == saved["library_root_id"]
    )
    assert not list(other.iterdir())
