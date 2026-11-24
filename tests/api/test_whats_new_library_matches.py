"""A verified native series match is read-only and grants no issue-level actions."""
# ruff: noqa: F811 - fixtures shared with existing release action tests.

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from pullbox.models import Issue, Series, SeriesExternalIdentity
from pullbox.models.whats_new import WhatsNewReleaseCache
from pullbox.services.whats_new_cache_service import WhatsNewCacheService
from tests.api.test_whats_new_find_add_api import actions_enabled, release_cache  # noqa: F401

pytest_plugins = ["conftest_security"]


def claim(series, namespace, value, state="verified"):
    return SeriesExternalIdentity(
        series_id=series.id,
        identity_namespace=namespace,
        external_id=str(value),
        verification_state=state,
        evidence_kind="user_selection",
    )


async def setup_release(db, cache_id, **identities):
    row = await db.get(WhatsNewReleaseCache, cache_id)
    issue = row.payload["issues"][0]
    issue = {**issue, "series": {**issue["series"], **identities}}
    row.payload = {**row.payload, "issues": [issue]}
    await WhatsNewCacheService().upsert_upcoming(db, payload={"weeks": []})
    return row, issue


@pytest.mark.parametrize(
    ("namespace", "monitored"),
    [
        ("metron", True),
        ("comicvine", False),
        ("gcd", True),
    ],
)
async def test_release_links_to_native_owner_without_adding_tracking_or_matching_an_issue(
    authenticated_client, sec_db, release_cache, namespace, monitored
):
    async with sec_db.begin() as db:
        await setup_release(db, release_cache, **{f"{namespace}_series_id": 8})
        owner = Series(title="Different catalog title", sort_title="different", monitored=monitored)
        db.add(owner)
        await db.flush()
        owner_id = owner.id
        db.add_all([claim(owner, namespace, 8), Issue(series_id=owner_id, issue_number="1")])
    page = await authenticated_client.get("/whats-new")
    assert page.is_success
    assert f'href="/series/{owner_id}">Added</a>' in page.text
    assert f">{'Tracked' if monitored else 'Paused'}</span>" in page.text
    assert 'data-testid="whats-new-local-issue"' not in page.text
    assert 'data-testid="whats-new-grab"' not in page.text
    assert "data-release-offer=" not in page.text
    async with sec_db() as db:
        assert await db.scalar(select(func.count()).select_from(SeriesExternalIdentity)) == 1
        assert (await db.get(Series, owner_id)).monitored is monitored


@pytest.mark.parametrize(
    "conflict",
    [
        "stale_link",
        "different_locg",
        "disputed_locg",
        "split_owners",
        "different_native",
        "other_week",
        "old_snapshot",
        "legacy_same_row",
        "legacy_other_owner",
        "other_release_on_hidden_week",
    ],
)
async def test_disagreement_never_projects_native_series_ownership(
    authenticated_client, sec_db, release_cache, conflict
):
    async with sec_db.begin() as db:
        row, issue = await setup_release(db, release_cache, metron_series_id=8, gcd_series_id=9)
        a = Series(title="Absolute Flash", sort_title="absolute flash")
        b = Series(title="Other", sort_title="other")
        db.add_all([a, b])
        await db.flush()
        db.add(claim(a, "metron", 8, "stale" if conflict == "stale_link" else "verified"))
        if conflict == "different_locg":
            db.add(claim(a, "locg", 180902))
        elif conflict == "disputed_locg":
            db.add(claim(b, "locg", 180901, "conflicted"))
        elif conflict == "split_owners":
            db.add(claim(b, "gcd", 9))
        elif conflict == "different_native":
            db.add(claim(a, "gcd", 10))
        elif conflict == "other_week":
            sibling = {**issue, "series": {**issue["series"], "metron_series_id": 11}}
            await WhatsNewCacheService().upsert_upcoming(
                db, payload={"weeks": [{"issues": [sibling]}]}
            )
        elif conflict == "old_snapshot":
            row.fetched_at = datetime.now(UTC) - timedelta(days=1)
        elif conflict.startswith("legacy"):
            row.payload = {
                **row.payload,
                "issues": [{**issue, "series": {**issue["series"], "comicvine_series_id": 12}}],
            }
            if conflict == "legacy_same_row":
                a.comicvine_id = 13
            else:
                b.comicvine_id = 12
        elif conflict == "other_release_on_hidden_week":
            db.add(claim(a, "comicvine", 12))
            sibling = {
                **issue,
                "locg_issue_id": 201,
                "locg_series_id": 202,
                "series": {"locg_series_id": 202, "comicvine_series_id": 12},
            }
            await WhatsNewCacheService().upsert_upcoming(
                db, payload={"weeks": [{"store_date": "2099-02-02", "issues": [sibling]}]}
            )
    page = await authenticated_client.get("/whats-new")
    assert page.is_success
    assert 'data-testid="whats-new-local-series"' not in page.text
    assert 'data-testid="whats-new-find-add"' in page.text
