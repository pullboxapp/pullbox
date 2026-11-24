"""Watched series and their chosen destinations across cutover and native restore."""

import json
from datetime import UTC, datetime

from playwright.async_api import expect


def seed(db):
    db.execute("UPDATE library_roots SET is_default_managed_destination=true WHERE id=77")
    db.execute(
        "INSERT INTO library_roots(id,name,path,enabled) VALUES(78,'Watch shelf','/watch-shelf',1)"
    )
    db.executemany(
        "INSERT INTO series_interests(id,source_namespace,source_series_id,title_snapshot,"
        "publisher_snapshot,target_library_root_id,last_actor_user_id,state) "
        "VALUES(?,'locg',?,?,'Cutover Press',78,(SELECT id FROM users WHERE username='reader'),?)",
        [
            (301, "9201", "Tide Tables", "watching"),
            (302, "9202", "Tide Tables", "cancelled"),
            (303, "9203", "Dock Lights", "needs_confirmation"),
        ],
    )
    # The first release has the same title but belongs to the cancelled Watch.
    releases = [
        {"locg_issue_id": issue, "series": {"locg_series_id": series, "title": title}}
        for issue, series, title in [
            (4502, 9202, "Tide Tables"),
            (4501, 9201, "Tide Tables: New Edition"),
        ]
    ]
    today = datetime.now(UTC).date().isoformat()
    # Startup refreshes both feeds if either is missing. Keep this check local.
    db.executemany(
        "INSERT INTO whats_new_release_cache(cache_key,cache_kind,store_date,payload,fetched_at,"
        "last_successful_refresh_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        [
            (
                f"current-week:{today}",
                "CURRENT_WEEK",
                today,
                json.dumps({"store_date": today, "issues": []}),
            ),
            (
                "upcoming:all",
                "UPCOMING",
                None,
                json.dumps({"weeks": [{"store_date": "2099-01-07", "issues": releases}]}),
            ),
        ],
    )


async def exercise(page):
    await page.goto("/pull-list")
    rows = page.get_by_test_id("pull-list-watch-row")
    await expect(rows).to_have_count(2)
    await expect(rows.filter(has_text="Tide Tables")).to_have_attribute("data-watch-id", "301")
    # Resolve through the authenticated route, without searching any external provider.
    response = await page.request.get("/api/v1/whats-new/watch/301/resolve")
    assert response.ok, (response.status, await response.text())
    data = await response.json()
    assert (data["locg_series_id"], data["selection"]["release_id"], data["title"]) == (
        "9201",
        4501,
        "Tide Tables: New Edition",
    )
    assert data["watch_default_id"] == 77
    assert [(root["id"], root["path"]) for root in data["roots"]] == [(78, "/watch-shelf")]
    assert (await page.request.get("/api/v1/whats-new/watch/302/resolve")).status == 409
    pending = rows.filter(has_text="Dock Lights")
    await expect(pending).to_contain_text("Needs confirmation")
    await pending.get_by_role("button", name="Cancel Watch").click()
    await expect(pending).to_have_count(0)
    await page.reload()
    await expect(rows).to_have_count(1)
    await expect(rows).to_have_attribute("data-watch-id", "301")


def verify(sql, db):
    assert sql(
        db,
        "SELECT i.id,i.source_series_id,i.state,r.path,u.username,i.resolved_series_id IS NULL "
        "FROM series_interests i JOIN library_roots r ON r.id=i.target_library_root_id "
        "JOIN users u ON u.id=i.last_actor_user_id WHERE i.source_namespace='locg' ORDER BY i.id",
    ).splitlines() == [
        "301|9201|watching|/watch-shelf|reader|t",
        "302|9202|cancelled|/watch-shelf|reader|t",
        "303|9203|cancelled|/watch-shelf|reader|t",
    ]
