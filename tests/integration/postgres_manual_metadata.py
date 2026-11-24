"""An explicit issue edit is durable; refreshing provider facts must not edit the CBZ."""

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

from defusedxml.ElementTree import fromstring

TITLE = "My Harbor Lights title"
PUBLISHER = "Cutover Press"
IMPRINT = "Harbor Noir"
ARCS = [{"name": "The Storm", "position": 3}, {"name": "Harbor After Dark", "position": 1}]
ISSUE_LISTS = {
    "Tags": ("Tags", "Tags/Tag", ["Harbor", "Coastal"]),
    "Genres": ("Genre", "Genres/Genre", ["Mystery", "Adventure"]),
    "Characters": ("Characters", "Characters/Character", ["Skipper"]),
    "Teams": ("Teams", "Teams/Team", ["Harbor Watch"]),
    "Locations": ("Locations", "Locations/Location", ["Lighthouse"]),
}


def seed(source, imports):
    publisher = source.execute(
        "INSERT INTO publishers(name) VALUES(?) RETURNING id", (PUBLISHER,)
    ).fetchone()[0]
    source.execute("UPDATE series SET publisher_id=? WHERE id=1000", (publisher,))
    source.execute(
        "INSERT INTO series_external_identities(series_id,identity_namespace,external_id,"
        "verification_state,evidence_kind) "
        "VALUES(1000,'comicvine','91000','verified','provider_result')"
    )
    source.execute(
        "INSERT INTO issue_external_identities(issue_id,identity_namespace,external_id,"
        "verification_state,evidence_kind) VALUES(10001,'gcd','102','verified','provider_result')"
    )
    original = imports / "catalog.db"
    with closing(sqlite3.connect(original)) as catalog, catalog:
        catalog.execute(
            "INSERT INTO gcd_series VALUES"
            "(21,'Harbor Lights','Harbor Lights',2024,NULL,1,1,2,0,0,1,'')"
        )
        catalog.executemany(
            "INSERT INTO gcd_issue VALUES(?,21,?,?,?,0,NULL,'','','',NULL)",
            [(102, "2", "Title from GCD", 2), (101, "13a", "Another story", 13)],
        )
    updated = imports / "updated-catalog.db"
    shutil.copyfile(original, updated)
    with closing(sqlite3.connect(updated)) as catalog, catalog:
        catalog.execute(
            "UPDATE gcd_issue SET title='Revised GCD title',on_sale_date='2024-11-20' WHERE id=102"
        )


def verify(sql, db, *, refreshed=False):
    assert sql(db, "SELECT title,status FROM issues WHERE id=10001") == f"{TITLE}|OWNED"
    snapshot = json.loads(
        sql(db, "SELECT snapshot_json FROM issue_metadata_baselines WHERE issue_id=10001")
    )
    assert snapshot["values"]["title"] == TITLE
    assert snapshot["values"]["age_rating"] == "Teen"
    assert snapshot["values"]["imprint"] == IMPRINT
    assert snapshot["values"]["story_arcs"] == ARCS
    assert any(o["field"] == "story_arcs" and o["user_override"] for o in snapshot["origins"])
    assert any(o["field"] == "imprint" and o["user_override"] for o in snapshot["origins"])
    for label, (_, _, names) in ISSUE_LISTS.items():
        assert snapshot["values"][label.lower()] == names
        assert any(o["field"] == label.lower() and o["user_override"] for o in snapshot["origins"])
    assert any(o["field"] == "age_rating" and o["user_override"] for o in snapshot["origins"])
    assert any(o["field"] == "title" and o["user_override"] for o in snapshot["origins"])
    if refreshed:
        assert sql(db, "SELECT store_date FROM issues WHERE id=10001") == "2024-11-20"


async def exercise(page, library, sql, db):
    from playwright.async_api import expect

    path = Path(sql(db, "SELECT file_path FROM library_files WHERE issue_id=10001"))
    comic = library / path.relative_to("/comics")

    def signature():
        stat = comic.stat()
        return hashlib.sha256(comic.read_bytes()).digest(), stat.st_ino, stat.st_mtime_ns

    async def refresh_issue():
        async with page.expect_response("**/issues/10001/refresh-metadata") as response:
            await (
                page.get_by_test_id("issue-metadata-links")
                .get_by_role("button", name="Refresh metadata", exact=True)
                .click()
            )
        result = await response.value
        assert result.ok, await result.text()

    before = signature()
    with ZipFile(comic) as archive:
        pages = {name: archive.read(name) for name in archive.namelist() if name.endswith(".png")}
    assert len(pages) == 2
    await page.goto("/issues/10001")
    await refresh_issue()
    assert sql(db, "SELECT title FROM issues WHERE id=10001") == "Title from GCD"
    async with page.expect_response("**/issues/10001/metadata-editor/preview") as response:
        await page.get_by_role("button", name="Edit metadata", exact=True).click()
    result = await response.value
    assert result.ok, await result.text()
    dialog = page.get_by_role("dialog", name="Edit issue metadata")
    await dialog.get_by_role("button", name="All fields", exact=True).click()
    await dialog.get_by_label("Update Title", exact=True).check()
    await dialog.get_by_role("button", name="Value source for Title").click()
    await page.get_by_role("option", name="Enter correction", exact=True).click()
    await dialog.get_by_label("New Title", exact=True).fill(TITLE)
    await dialog.get_by_label("Update Age rating", exact=True).check()
    await dialog.get_by_role("button", name="Value source for Age rating").click()
    await page.get_by_role("option", name="Enter correction", exact=True).click()
    await dialog.get_by_role("button", name="New Age rating", exact=True).click()
    await page.get_by_role("option", name="Teen", exact=True).click()
    await dialog.get_by_label("Update Imprint", exact=True).check()
    await dialog.get_by_role("button", name="Value source for Imprint").click()
    await page.get_by_role("option", name="Enter correction", exact=True).click()
    await dialog.get_by_label("New Imprint", exact=True).fill(IMPRINT)
    await dialog.get_by_label("Update Embedded story arcs", exact=True).check()
    await dialog.get_by_role("button", name="Value source for Embedded story arcs").click()
    await page.get_by_role("option", name="Enter correction", exact=True).click()
    for index, entry in enumerate(ARCS, 1):
        if index > 1:
            await dialog.get_by_role("button", name="Add arc entry").click()
        await dialog.get_by_label(f"Arc name {index}", exact=True).fill(entry["name"])
        await dialog.get_by_label(f"Arc position {index}", exact=True).fill(str(entry["position"]))
    for label, (_, _, names) in ISSUE_LISTS.items():
        await dialog.get_by_label(f"Update {label}", exact=True).check()
        await dialog.get_by_role("button", name=f"Value source for {label}").click()
        await page.get_by_role("option", name="Enter correction", exact=True).click()
        await dialog.get_by_label(f"New {label}", exact=True).fill("\n".join(names))
    async with page.expect_response("**/issues/10001/metadata-editor/preview") as response:
        await dialog.get_by_role("button", name="Update preview").click()
    result = await response.value
    preview = await result.json()
    assert result.ok and preview["ready"], preview
    assert {
        c["document"]
        for c in preview["changes"]
        if "AgeRating" in c["field"] and c["after"] == "Teen"
    } == {"ComicInfo.xml", "MetronInfo.xml"}
    assert signature() == before
    await dialog.get_by_role("button", name="Apply selected changes").click()
    await expect(dialog.get_by_text("Both metadata files are up to date.")).to_be_visible(
        timeout=20000
    )
    verify(sql, db)
    with ZipFile(comic) as archive:
        assert fromstring(archive.read("ComicInfo.xml")).findtext("Title") == TITLE
        ci = fromstring(archive.read("ComicInfo.xml"))
        mi = fromstring(archive.read("MetronInfo.xml"))
        assert ci.findtext("Publisher") == mi.findtext("Publisher/Name") == PUBLISHER
        assert ci.findtext("Imprint") == mi.findtext("Publisher/Imprint") == IMPRINT
        assert ci.findtext("StoryArc") == "The Storm, Harbor After Dark"
        assert ci.findtext("StoryArcNumber") == "3, 1"
        assert [(a.findtext("Name"), a.findtext("Number")) for a in mi.findall("Arcs/Arc")] == [
            ("The Storm", "3"),
            ("Harbor After Dark", "1"),
        ]
        for ci_path, mi_path, names in ISSUE_LISTS.values():
            assert ci.findtext(ci_path) == ", ".join(names)
            assert [n.text for n in mi.findall(mi_path)] == names
        for name in ("ComicInfo.xml", "MetronInfo.xml"):
            assert fromstring(archive.read(name)).findtext("AgeRating") == "Teen"
        assert fromstring(archive.read("MetronInfo.xml")).findtext("Stories/Story") == TITLE
        assert {
            name: archive.read(name) for name in archive.namelist() if name.endswith(".png")
        } == pages
    edited = signature()
    assert edited[0] != before[0]
    await dialog.get_by_role("button", name="Close", exact=True).click()
    await expect(page.get_by_test_id("issue-detail-subtitle")).to_have_text(TITLE)
    await page.goto("/settings?tab=metadata")
    settings = page.get_by_test_id("gcd-local-access")
    await settings.get_by_label("GCD SQLite database").fill("/imports/updated-catalog.db")
    async with page.expect_response("**/metadata/sources/gcd_local") as response:
        await settings.get_by_role("button", name="Validate and enable GCD", exact=True).click()
    assert (await response.value).ok
    await page.goto("/issues/10001")
    await refresh_issue()
    verify(sql, db, refreshed=True)
    await page.goto("/series/1000")
    async with page.expect_navigation(wait_until="domcontentloaded"):
        async with page.expect_response("**/series/1000/refresh") as response:
            await page.get_by_test_id("series-action-refresh").click()
        result = await response.value
        assert result.ok, await result.text()
    verify(sql, db, refreshed=True)
    await expect(page.locator("#series-issue-title-link-10001")).to_have_text(TITLE)
    await page.goto("/issues/10001")
    await page.get_by_role("button", name="Write file metadata", exact=True).click()
    dialog = page.get_by_role("dialog", name="Write file metadata")
    await expect(dialog.get_by_test_id("file-metadata-changes")).to_contain_text("2024-11-20")
    assert signature() == edited
    await dialog.get_by_role("button", name="Write both metadata files").click()
    await expect(dialog.get_by_text("Both metadata files are up to date.")).to_be_visible(
        timeout=20000
    )
    await dialog.get_by_role("button", name="Close", exact=True).click()
    with ZipFile(comic) as archive:
        assert fromstring(archive.read("MetronInfo.xml")).findtext("StoreDate") == "2024-11-20"

    async def review_date(choice):
        await page.get_by_role("button", name="Edit metadata", exact=True).click()
        editor = page.get_by_role("dialog", name="Edit issue metadata")
        await editor.get_by_role("button", name="All fields", exact=True).click()
        await editor.get_by_label("Update Store date", exact=True).check()
        await editor.get_by_role("button", name="Value source for Store date").click()
        await page.get_by_role("option", name=choice, exact=True).click()
        async with page.expect_response("**/issues/10001/metadata-editor/preview") as pending:
            await editor.get_by_role("button", name="Update preview").click()
        result = await pending.value
        assert result.ok, await result.text()
        return editor, await result.json()

    async def check_interrupted_approval(editor, message):
        write_url = "**/issues/10001/metadata-editor/write"
        jobs = (
            "SELECT count(*) FROM utility_jobs WHERE job_type='file_metadata' "
            "AND config::json->>'issue_id'='10001'"
        )
        before_jobs = int(sql(db, jobs))
        receipts = []

        async def lose_ack(route):
            response = await route.fetch(max_retries=0, max_redirects=0)
            assert response.status == 202, await response.text()
            receipts.append((await response.json())["job_id"])
            await route.abort()

        await page.route(write_url, lose_ack)
        try:
            await editor.get_by_role("button", name="Apply selected changes").click()
            async with page.expect_response("**/file-metadata/job?editor_request_id=*") as pending:
                await editor.get_by_role("button", name="Check this edit").click()
            report = await (await pending.value).json()
            assert [report["job"]["id"]] == receipts
            await expect(editor.get_by_text(message, exact=False)).to_be_visible(timeout=20000)
            assert int(sql(db, jobs)) == before_jobs + 1
        finally:
            await page.unroute(write_url, lose_ack)

    editor, preview = await review_date("Clear value")
    assert preview["ready"] and preview["editor_changed"]
    await check_interrupted_approval(editor, "Both metadata files are up to date.")
    await editor.get_by_role("button", name="Close", exact=True).click()
    cleared = signature()
    with ZipFile(comic) as archive:
        assert fromstring(archive.read("MetronInfo.xml")).find("StoreDate") is None
        assert {name: archive.read(name) for name in pages} == pages
    await refresh_issue()
    assert sql(db, "SELECT store_date IS NULL FROM issues WHERE id=10001") == "t"
    assert signature() == cleared
    editor, preview = await review_date("Remove manual override")
    assert preview["ready"] and preview["unchanged"] and not preview["changes"]
    await expect(editor.get_by_text("Only the manual override", exact=False)).to_be_visible()
    await check_interrupted_approval(editor, "Selected fields are back under provider control.")
    await editor.get_by_role("button", name="Close", exact=True).click()
    assert sql(db, "SELECT store_date IS NULL FROM issues WHERE id=10001") == "t"
    await refresh_issue()
    verify(sql, db, refreshed=True)
    assert signature() == cleared
