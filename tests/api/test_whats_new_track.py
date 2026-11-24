"""Release Track changes the proven series, never a guessed issue or another owner."""
# ruff: noqa: F811 - use the release fixtures from their owning module.

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from pullbox.config import get_settings
from pullbox.core.events import EventBus
from pullbox.models import Issue, IssueStatus, Series, SeriesExternalIdentity
from pullbox.models.whats_new import WhatsNewReleaseCache
from pullbox.services.whats_new_grab import cache_fingerprint
from tests.api.test_metadata_sources_api import csrf
from tests.api.test_whats_new_find_add_api import actions_enabled, release_cache  # noqa: F401
from tests.api.test_whats_new_library_matches import claim, setup_release

pytest_plugins = ["conftest_security"]
URL = "/api/v1/whats-new/track"


async def paused(db, cache_id, namespace):
    row, _ = await setup_release(db, cache_id, metron_series_id=8)
    owner = Series(title="Local edition", sort_title="local", monitored=False)
    db.add(owner)
    await db.flush()
    db.add(claim(owner, namespace, 8 if namespace == "metron" else 180901))
    db.add_all(
        [
            Issue(series_id=owner.id, issue_number=float(i), status=state, manual_skip=manual)
            for i, (state, manual) in enumerate(
                [
                    (IssueStatus.SKIPPED, False),
                    (IssueStatus.SKIPPED, True),
                    (IssueStatus.OWNED, False),
                    (IssueStatus.DOWNLOADING, False),
                ],
                1,
            )
        ]
    )
    return {
        "cache_id": row.id,
        "release_id": 1514020,
        "fingerprint": cache_fingerprint(row.payload),
        "expected_series_id": owner.id,
    }


@pytest.mark.parametrize("namespace", ["locg", "metron"])
async def test_track_preserves_owned_active_and_manual_skips_and_is_idempotent(
    authenticated_client, sec_db, release_cache, monkeypatch, namespace
):
    async def check_committed(event):
        async with sec_db() as reader:
            assert (await reader.get(Series, event.series_id)).monitored
            assert (await reader.get(Issue, event.issue_id)).status is IssueStatus.WANTED

    emit = AsyncMock(side_effect=check_committed)
    monkeypatch.setattr(EventBus, "emit", emit)
    async with sec_db.begin() as db:
        selection = await paused(db, release_cache, namespace)
    page = await authenticated_client.get("/whats-new")
    assert 'data-testid="whats-new-track"' in page.text
    for _ in range(2):
        result = await authenticated_client.post(
            URL, json=selection, headers=csrf(authenticated_client)
        )
        assert result.status_code == 200, result.text
        assert result.json() == {"id": selection["expected_series_id"], "monitored": True}
    emit.assert_awaited_once()
    async with sec_db() as db:
        assert (await db.get(Series, selection["expected_series_id"])).monitored
        assert list(await db.scalars(select(Issue.status).order_by(Issue.issue_number))) == [
            IssueStatus.WANTED,
            IssueStatus.SKIPPED,
            IssueStatus.OWNED,
            IssueStatus.DOWNLOADING,
        ]
        assert await db.scalar(select(func.count()).select_from(SeriesExternalIdentity)) == 1
    page = await authenticated_client.get("/whats-new")
    assert 'data-testid="whats-new-track"' not in page.text and ">Tracked</span>" in page.text


@pytest.mark.parametrize("change", ["row", "owner", "other_series", "crossfeed", "disabled"])
async def test_track_rejects_stale_or_retargeted_selection_without_events(
    authenticated_client, sec_db, release_cache, monkeypatch, change
):
    emit = AsyncMock()
    monkeypatch.setattr(EventBus, "emit", emit)
    async with sec_db.begin() as db:
        selection = await paused(db, release_cache, "metron")
        other = Series(title="Other", sort_title="other", monitored=False)
        db.add(other)
        await db.flush()
        if change == "owner":
            (await db.scalar(select(SeriesExternalIdentity))).series_id = other.id
        elif change == "row":
            row = await db.get(WhatsNewReleaseCache, release_cache)
            row.payload = {**row.payload, "issues": []}
        elif change == "other_series":
            selection["expected_series_id"] = other.id
        elif change == "crossfeed":
            from pullbox.services.whats_new_cache_service import WhatsNewCacheService

            row = await db.get(WhatsNewReleaseCache, release_cache)
            issue = row.payload["issues"][0]
            await WhatsNewCacheService().upsert_upcoming(
                db,
                payload={
                    "weeks": [
                        {
                            "issues": [
                                {**issue, "series": {**issue["series"], "metron_series_id": 9}}
                            ]
                        }
                    ]
                },
            )
    if change == "disabled":
        monkeypatch.setenv("PULLBOX_METADATA_WHATS_NEW_ACTIONS_ENABLED", "false")
        get_settings.cache_clear()
    result = await authenticated_client.post(
        URL, json=selection, headers=csrf(authenticated_client)
    )
    assert result.status_code == (404 if change == "disabled" else 409)
    async with sec_db() as db:
        assert not any(await db.scalars(select(Series.monitored)))
        assert (
            await db.scalar(
                select(func.count()).select_from(Issue).where(Issue.status == IssueStatus.WANTED)
            )
            == 0
        )
    emit.assert_not_awaited()


async def test_track_requires_interactive_operator_and_csrf(
    authenticated_client, unauthenticated_client, sec_db, release_cache, sec_api_key
):
    async with sec_db.begin() as db:
        selection = await paused(db, release_cache, "locg")
    assert (await authenticated_client.post(URL, json=selection)).status_code == 403
    assert (
        await unauthenticated_client.post(URL, json=selection, headers={"X-API-Key": sec_api_key})
    ).status_code in (401, 403)
    assert (
        await authenticated_client.post(
            URL, json={**selection, "monitored": False}, headers=csrf(authenticated_client)
        )
    ).status_code == 422
