"""Exercise the two series editor destinations on a migrated Mylar library."""

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
    await apply("Series.json and comic files were left unchanged.")
    assert sql(db, "SELECT description FROM series WHERE id=1001") == SUMMARY
    assert signature(path) == original_json and members() == originals
    targets = await review("Library and eligible series.json files")
    assert [(t["directory"], t["action"]) for t in targets] == [
        (str(Path("/comics") / DIRECTORY), "update")
    ]
    await apply("approved sidecar work finished")
    verify(sql, db, library)
    assert members() == originals
