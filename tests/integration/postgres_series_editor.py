"""Exercise the two series editor destinations on a migrated Mylar library."""

import asyncio
import hashlib
import json
from pathlib import Path

DIRECTORY = Path("Cutover Shelf 1 (2024)")
SUMMARY = "My series description"
CUSTOM = {"shelf": ["family", "favorites"]}


def seed_sidecar(comics):
    path = comics / DIRECTORY / "series.json"
    with path.open("x") as stream:
        json.dump(
            {"metadata": {"ComicID": 92000, "Summary": "Old summary"}, "custom": CUSTOM}, stream
        )


def verify(sql, db, library):
    snapshot = json.loads(
        sql(db, "SELECT snapshot_json FROM series_metadata_baselines WHERE series_id=1001")
    )
    assert sql(db, "SELECT description FROM series WHERE id=1001") == SUMMARY
    assert any(o["field"] == "description" and o["user_override"] for o in snapshot["origins"])
    sidecar = json.loads((library / DIRECTORY / "series.json").read_bytes())
    assert sidecar["metadata"]["Summary"] == snapshot["values"]["description"] == SUMMARY
    assert sidecar["metadata"]["ComicID"] == 92000 and sidecar["custom"] == CUSTOM
    assert sidecar["pullbox"]["snapshot"] == snapshot


async def exercise(page, library, sql, db):
    from playwright.async_api import expect

    path = library / DIRECTORY / "series.json"

    def signature(file):
        st = file.stat()
        return hashlib.sha256(file.read_bytes()).digest(), st.st_ino, st.st_mtime_ns

    def members():
        return {comic: signature(comic) for comic in library.rglob("*.cbz")}

    originals, original_json = members(), signature(path)
    assert any(comic.parent == path.parent for comic in originals)
    await page.goto("/series/1001")
    trigger = page.get_by_role("button", name="Edit metadata", exact=True)
    dialog = page.get_by_role("dialog", name="Edit series metadata", exact=True)

    async def review(destination):
        await trigger.click()
        await dialog.get_by_label(destination, exact=True).check()
        await dialog.get_by_role("button", name="All fields", exact=True).click()
        await dialog.get_by_label("Update Description", exact=True).check()
        if destination == "Library only":
            await dialog.get_by_role("button", name="Value source for Description").click()
            await page.get_by_role("option", name="Enter correction", exact=True).click()
            await dialog.get_by_label("New Description", exact=True).fill(SUMMARY)
        async with page.expect_response("**/1001/metadata-editor/preview") as pending:
            await dialog.get_by_role("button", name="Update preview", exact=True).click()
        result = await pending.value
        assert result.ok, await result.text()
        preview = await result.json()
        assert preview["ready"] and not preview["warnings"], preview
        assert signature(path) == original_json and members() == originals
        return preview["targets"]

    async def apply(message):
        async with page.expect_response("**/1001/metadata-editor/write") as pending:
            await dialog.get_by_role("button", name="Apply selected changes", exact=True).click()
        result = await pending.value
        assert result.ok, await result.text()
        await expect(dialog.get_by_role("progressbar")).to_have_attribute(
            "aria-valuenow", "100", timeout=20000
        )
        await expect(dialog.get_by_role("status").filter(has_text=message)).to_be_visible()
        await dialog.get_by_role("button", name="Close", exact=True).click()

    assert not await review("Library only")
    description = "SELECT description FROM series WHERE id=1001"
    baseline = "SELECT snapshot_json FROM series_metadata_baselines WHERE series_id=1001"
    jobs = (
        "SELECT count(*) FROM utility_jobs WHERE config::json->>'series_id'='1001' "
        "AND config::json->>'series_editor' IS NOT NULL"
    )
    old_value, old_baseline, old_jobs = (sql(db, query) for query in (description, baseline, jobs))
    assert old_value != SUMMARY
    submit = dialog.get_by_role("button", name="Apply selected changes", exact=True)
    check = dialog.get_by_role("button", name="Check saved values", exact=True)
    # Interrupt only this editor's disposable PostgreSQL session, at commit after
    # the description, provenance, and job have all been written in the transaction.
    sql(
        db,
        """
        CREATE FUNCTION interrupt_editor_commit() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN PERFORM pg_terminate_backend(pg_backend_pid()); RETURN NEW; END $$;
        CREATE CONSTRAINT TRIGGER interrupt_editor_commit AFTER INSERT ON utility_jobs
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        WHEN (NEW.config::json->>'series_id'='1001'
              AND NEW.config::json->>'series_editor' IS NOT NULL)
        EXECUTE FUNCTION interrupt_editor_commit();
    """,
    )
    try:
        async with page.expect_response("**/1001/metadata-editor/write") as interrupted:
            await submit.click()
        failure = await interrupted.value
        assert (
            failure.status == 503
            and (await failure.json())["error"]["code"] == "SERIES_METADATA_SAVE_UNCONFIRMED"
        )
        assert "retry-after" not in failure.headers
        assert (sql(db, description), sql(db, baseline), sql(db, jobs)) == (
            old_value,
            old_baseline,
            old_jobs,
        )
    finally:
        sql(
            db,
            "DROP TRIGGER interrupt_editor_commit ON utility_jobs; "
            "DROP FUNCTION interrupt_editor_commit()",
        )

    async def check_values(expected):
        await expect(check).to_be_visible()
        async with page.expect_response("**/1001/metadata-editor/preview") as checked:
            await check.click()
        response = await checked.value
        assert response.ok and response.request.post_data_json == {
            "mode": "library_only",
            "edits": {},
        }
        saved = dialog.get_by_role("region", name="Saved values")
        await expect(
            saved.get_by_text("Saved: " + (expected or "Not set"), exact=True)
        ).to_be_visible()
        await expect(submit).to_be_hidden()
        assert signature(path) == original_json and members() == originals

    await check_values(old_value)
    assert sql(db, jobs) == old_jobs
    await dialog.get_by_role("button", name="Review changes again").click()
    await expect(submit).to_be_enabled()
    # Forward once, wait for the actual commit, then drop just the response.
    acknowledgements = []

    async def lose_ack(route):
        result = await route.fetch(max_retries=0, max_redirects=0)
        acknowledgements.append((result.status, (await result.json())["job_id"]))
        await route.abort()

    write_url = "**/1001/metadata-editor/write"
    await page.route(write_url, lose_ack)
    try:
        await submit.click()
        await check_values(SUMMARY)
    finally:
        await page.unroute(write_url, lose_ack)
    assert len(acknowledgements) == 1 and acknowledgements[0][0] == 202
    assert int(sql(db, jobs)) == int(old_jobs) + 1
    async with asyncio.timeout(20):
        while True:
            receipt = await page.request.get("/api/v1/series/1001/metadata-editor/job")
            assert receipt.ok
            report = await receipt.json()
            assert report["library_saved"] and report["job"]["id"] == acknowledgements[0][1]
            if report["job"]["state"] == "COMPLETED":
                assert report["job"]["percent"] == 100
                assert (
                    "Series.json and comic files were left unchanged." in report["job"]["message"]
                )
                break
            assert report["job"]["state"] in {"RUNNING", "QUEUED"}
            await asyncio.sleep(0.15)
    await dialog.get_by_role("button", name="Close", exact=True).click()
    await page.reload()
    assert sql(db, "SELECT description FROM series WHERE id=1001") == SUMMARY
    assert signature(path) == original_json and members() == originals
    targets = await review("Library and eligible series.json files")
    assert [(t["directory"], t["action"]) for t in targets] == [
        (str(Path("/comics") / DIRECTORY), "update")
    ]
    await apply("approved sidecar work finished")
    verify(sql, db, library)
    assert members() == originals
