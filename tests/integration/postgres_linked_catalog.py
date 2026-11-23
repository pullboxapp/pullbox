"""Real GCD link, explicit overlap review, and repeat refresh after first start."""

import hashlib
import sqlite3
from contextlib import closing


def seed(source, imports, comics):
    from tests.api.test_gcd_local import gcd_dump
    from tests.integration.postgres_import_workload import _comic

    digest = _comic(comics / "one.cbz", "0.5", series="first confidential title")
    size = (comics / "one.cbz").stat().st_size
    source.execute("UPDATE series SET comicvine_id=700 WHERE id=71")
    source.execute("UPDATE issues SET comicvine_id=701 WHERE id=83")
    source.execute(
        "INSERT INTO series_external_identities(series_id,identity_namespace,external_id,"
        "verification_state,evidence_kind) "
        "VALUES(71,'comicvine','700','verified','provider_result')"
    )
    source.execute(
        "INSERT INTO issue_external_identities(issue_id,identity_namespace,external_id,"
        "verification_state,evidence_kind) "
        "VALUES(83,'comicvine','701','verified','provider_result')"
    )
    source.execute(
        "UPDATE library_files SET issue_id=83,file_size=?,storage_mode='referenced' WHERE id=101",
        (size,),
    )
    with closing(sqlite3.connect(gcd_dump(imports / "catalog.db"))) as catalog, catalog:
        catalog.execute(
            "UPDATE gcd_series SET name='first confidential title', "
            "sort_name='first confidential title' WHERE id=2999"
        )
        catalog.execute("UPDATE gcd_issue SET number='0.5' WHERE id IN (10,13)")
    return digest


def verify(sql, db):
    rows = sql(
        db,
        "SELECT i.id,i.status,g.external_id,i.issue_number_text FROM issues i "
        "JOIN issue_external_identities g ON g.issue_id=i.id "
        "WHERE i.series_id=71 AND g.identity_namespace='gcd' AND g.verification_state='verified' "
        "ORDER BY i.id",
    ).splitlines()
    assert len(rows) == 3 and rows[0].startswith("83|OWNED|10|"), rows
    assert {tuple(row.split("|")[1:]) for row in rows[1:]} == {
        ("WANTED", "11", "13B"),
        ("WANTED", "12", "50-X"),
    }, rows
    assert sql(db, "SELECT issue_count,comicvine_id FROM series WHERE id=71") == "3|700"
    assert sql(db, "SELECT count(*) FROM issues WHERE series_id=71") == "3"
    assert sql(db, "SELECT comicvine_id FROM issues WHERE id=83") == "701"
    assert sql(
        db,
        "SELECT identity_namespace,external_id,verification_state FROM series_external_identities "
        "WHERE series_id=71 ORDER BY identity_namespace",
    ).splitlines() == ["comicvine|700|verified", "gcd|2999|verified"]
    assert sql(db, "SELECT issue_id,file_path,storage_mode FROM library_files WHERE id=101") == (
        "83|/comics/one.cbz|referenced"
    )
    return rows


async def exercise(page, library, sql, db, digest):
    from playwright.async_api import expect

    file = library / "one.cbz"
    original = file.stat()
    await page.goto("/settings?tab=metadata")
    settings = page.get_by_test_id("gcd-local-access")
    await settings.get_by_label("GCD SQLite database").fill("/imports/catalog.db")
    await settings.get_by_role("button", name="Validate and enable GCD", exact=True).click()
    await expect(settings.get_by_role("status")).to_contain_text("GCD is enabled")
    await page.goto("/series/71")
    card = page.get_by_test_id("series-metadata-links")
    await expect(card).to_contain_text("ComicVine 700")
    await card.get_by_role("button", name="Link provider").click()
    dialog = page.get_by_role("dialog", name="Link metadata provider")
    await dialog.locator("#series-link-source-trigger").click()
    await page.get_by_role("option", name="GCD Local", exact=True).click()
    async with page.expect_response("**/api/v1/metadata/search") as search:
        await dialog.get_by_role("button", name="Search provider").click()
    found = await (await search.value).json()
    assert [item["external_id"] for item in found["results"]] == ["2999"], found
    await dialog.get_by_role("button", name="Review first confidential title").click()
    await dialog.get_by_role("button", name="Link this series").click()
    await expect(card).to_contain_text("GCD 2999")
    assert sql(db, "SELECT COUNT(*) FROM issues WHERE series_id=71") == "1"

    async def refresh():
        async with page.expect_response(lambda r: r.url.endswith("/series/71/refresh")) as result:
            await page.get_by_test_id("series-action-refresh").click()
        return await result.value

    rejected = await refresh()
    assert rejected.status == 409, await rejected.text()
    message = (await rejected.json())["detail"]
    assert "GCD Local" in message and "Link provider" in message, message
    await expect(page.get_by_text(message, exact=True)).to_be_visible()
    assert sql(db, "SELECT COUNT(*) FROM issues WHERE series_id=71") == "1"
    await page.goto("/issues/83")
    card = page.get_by_test_id("issue-metadata-links")
    await card.get_by_role("button", name="Link provider").click()
    dialog = page.get_by_role("dialog", name="Link issue metadata provider")
    await dialog.get_by_role("button", name="Browse provider issues").click()
    await dialog.get_by_role("button", name="Review issue 0.5", exact=True).click()
    await dialog.get_by_role("button", name="Link this issue").click()
    await expect(card).to_contain_text("GCD 10")
    await page.goto("/series/71")
    previous = None
    for _ in range(2):
        async with page.expect_navigation(wait_until="domcontentloaded"):
            response = await refresh()
            assert response.ok, await response.text()
        current = verify(sql, db)
        table = page.get_by_test_id("series-detail-issues-table")
        for designation in ("13B", "50-X"):
            await expect(table).to_contain_text(designation)
        assert previous is None or previous == current
        previous = current
    assert hashlib.sha256(file.read_bytes()).digest() == digest
    assert (file.stat().st_ino, file.stat().st_mtime_ns) == (original.st_ino, original.st_mtime_ns)
    return previous
