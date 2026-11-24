"""Selected writes and kept-in-place protection across a PostgreSQL cutover."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

from defusedxml.ElementTree import fromstring

TITLE = "Cutover Shelf 2"
OLD_TITLE = "Old shelf name"
FILES = tuple(Path(TITLE) / f"Shelf {number}.cbz" for number in (1, 2, 3))
PUBLICATION_TITLES = [("Rivage", "fr"), ("Rivage", "en"), ("New edition", "en")]


def seed(db, library):
    from tests.integration.postgres_import_workload import _comic

    db.execute("UPDATE series SET comicvine_id=93000 WHERE id=1002")
    db.execute(
        "INSERT INTO series_external_identities(series_id,identity_namespace,external_id,"
        "verification_state,evidence_kind) "
        "VALUES(1002,'comicvine','93000','verified','provider_result')"
    )
    for number, relative in enumerate(FILES, 1):
        issue_id, cv = 10031 + number, 93000 + number
        path = library / relative
        _comic(path, str(number), series=OLD_TITLE)
        alternatives = (
            '<AlternativeNames><AlternativeName id="61">Rivage</AlternativeName>'
            '<AlternativeName lang="fr" id="62">Rivage</AlternativeName></AlternativeNames>'
            if number == 2
            else ""
        )
        with ZipFile(path, "a") as archive:
            archive.writestr(
                "MetronInfo.xml",
                f'<MetronInfo><Series id="93000"><Name>{OLD_TITLE}</Name>{alternatives}</Series>'
                f'<Number>{number}</Number><IDS><ID source="Comic Vine" primary="true">{cv}'
                "</ID></IDS></MetronInfo>",
            )
        timestamp = 1700000000 + number
        os.utime(path, (timestamp, timestamp))
        db.execute("UPDATE issues SET comicvine_id=?,status='OWNED' WHERE id=?", (cv, issue_id))
        db.execute(
            "INSERT INTO issue_external_identities(issue_id,identity_namespace,external_id,"
            "verification_state,evidence_kind) "
            "VALUES(?,'comicvine',?,'verified','provider_result')",
            (issue_id, str(cv)),
        )
        db.execute(
            "INSERT INTO library_files(library_root_id,issue_id,file_path,file_name,file_size,"
            "file_format,file_modified_at,match_confidence,has_comicinfo,storage_mode) "
            "VALUES(77,?,?,?,?,'CBZ',?,'HIGH',true,?)",
            (
                issue_id,
                str(Path("/comics") / relative),
                path.name,
                path.stat().st_size,
                datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M:%S.%f"),
                "referenced" if number == 3 else "managed",
            ),
        )


def verify(sql, db, library):
    rows = sql(
        db,
        "SELECT i.id,i.status,l.file_path,l.storage_mode FROM issues i "
        "JOIN library_files l ON i.id=l.issue_id WHERE i.series_id=1002 ORDER BY i.id",
    )
    assert rows.splitlines() == [
        f"{10031 + n}|OWNED|/comics/{relative}|{'referenced' if n == 3 else 'managed'}"
        for n, relative in enumerate(FILES, 1)
    ]
    for n, relative in enumerate(FILES, 1):
        with ZipFile(library / relative) as archive:
            ci = fromstring(archive.read("ComicInfo.xml"))
            mi = fromstring(archive.read("MetronInfo.xml"))
        assert (
            ci.findtext("Series") == mi.findtext("Series/Name") == (TITLE if n == 2 else OLD_TITLE)
        )
        assert ci.findtext("Number") == mi.findtext("Number") == str(n)
        assert mi.findtext("IDS/ID") == str(93000 + n)


async def exercise(page, library, sql, db):
    from playwright.async_api import expect

    def signatures():
        return {
            p.relative_to(library): (
                hashlib.sha256(p.read_bytes()).digest(),
                p.stat().st_ino,
                p.stat().st_mtime_ns,
            )
            for p in library.rglob("*")
            if p.is_file()
        }

    before = signatures()
    with ZipFile(library / FILES[1]) as archive:
        pages = {n: archive.read(n) for n in archive.namelist() if n.endswith(".png")}
    assert len(pages) == 2
    await page.goto("/series/1002")
    await page.get_by_role("button", name="Update comic metadata", exact=True).click()
    dialog = page.get_by_role("dialog", name="Update series comic metadata")
    await dialog.get_by_role("checkbox", name="Series name", exact=True).check()
    preview = dialog.get_by_role("button", name="Preview comic files")
    await preview.click()
    status = dialog.get_by_role("status")
    await expect(
        status.filter(has_text="2 ready; 0 already current; 1 need attention")
    ).to_be_visible(timeout=20000)
    blocked = dialog.locator("article").filter(has_text="Issue 3")
    await expect(blocked).to_contain_text("Files kept in place are not modified")
    await expect(blocked.get_by_role("link", name="Review issue")).to_have_attribute(
        "href", "/issues/10034"
    )
    await expect(blocked.get_by_role("checkbox")).to_have_count(0)
    await dialog.get_by_role("checkbox", name="Update Issue 1", exact=True).uncheck()
    assert signatures() == before
    async with page.expect_response("**/1002/metadata-propagation/write") as pending:
        await dialog.get_by_role("button", name="Update 1 selected file", exact=True).click()
    result = await pending.value
    assert result.ok and result.request.post_data_json["issue_ids"] == [10033]
    await expect(
        status.filter(has_text="1 updated; 0 already current; 0 need attention")
    ).to_be_visible(timeout=20000)
    verify(sql, db, library)
    after = signatures()
    assert before.keys() == after.keys()
    assert {p for p in before if before[p] != after[p]} == {FILES[1]}
    with ZipFile(library / FILES[1]) as archive:
        assert {n: archive.read(n) for n in archive.namelist() if n.endswith(".png")} == pages
    await preview.click()
    await expect(
        status.filter(has_text="1 ready; 1 already current; 1 need attention")
    ).to_be_visible(timeout=20000)
    assert signatures() == after
    await _correct_titles(page, library, signatures)
    verify_publication_titles(sql, db, library)


def verify_publication_titles(sql, db, library):
    snapshot = json.loads(
        sql(db, "SELECT snapshot_json FROM series_metadata_baselines WHERE series_id=1002")
    )
    assert snapshot["values"]["publication_titles"] == [
        {"name": name, "language": language} for name, language in PUBLICATION_TITLES
    ]
    assert any(
        o["field"] == "publication_titles" and o["user_override"] for o in snapshot["origins"]
    )
    for n, relative in enumerate(FILES, 1):
        with ZipFile(library / relative) as archive:
            titles = fromstring(archive.read("MetronInfo.xml")).findall(
                "Series/AlternativeNames/AlternativeName"
            )
        assert [(t.text, t.get("lang", "en"), t.get("id")) for t in titles] == (
            [("Rivage", "fr", "62"), ("Rivage", "en", "61"), ("New edition", "en", None)]
            if n == 2
            else []
        )


async def _correct_titles(page, library, signatures):
    from playwright.async_api import expect

    before = signatures()
    with ZipFile(library / FILES[1]) as archive:
        unchanged = {
            name: archive.read(name) for name in archive.namelist() if name != "MetronInfo.xml"
        }
    dialog = page.get_by_role("dialog", name="Update series comic metadata")
    preview = dialog.get_by_role("button", name="Preview comic files")
    await dialog.get_by_label("Series name", exact=True).uncheck()
    await dialog.get_by_label("Alternate publication titles", exact=True).check()
    await preview.click()
    row = dialog.get_by_role("article").filter(has_text="Issue 2")
    titles = row.get_by_role("list", name="Titles to keep")
    await expect(titles.get_by_role("listitem")).to_have_text(
        ["Rivage (en)", "Rivage (fr)"], timeout=20000
    )
    await expect(row.get_by_role("checkbox")).to_have_count(0)
    await row.get_by_role("button", name="Edit series metadata").click()
    editor = page.get_by_role("dialog", name="Edit series metadata", exact=True)
    await expect(dialog).to_be_hidden()
    await editor.get_by_label("Library only", exact=True).check()
    await editor.get_by_role("button", name="All fields").click()
    await editor.get_by_label("Update Alternate publication titles", exact=True).check()
    await editor.get_by_role("button", name="Value source for Alternate publication titles").click()
    await page.get_by_role("option", name="Enter correction", exact=True).click()
    for index, (name, language) in enumerate(PUBLICATION_TITLES, 1):
        if index != 1:
            await editor.get_by_role("button", name="Add publication title").click()
        await editor.get_by_label(f"Publication title {index}", exact=True).fill(name)
        await editor.get_by_label(f"Title language {index}", exact=True).fill(language)
    await editor.get_by_role("button", name="Update preview").click()
    await editor.get_by_role("button", name="Apply selected changes").click()
    await expect(editor.get_by_role("progressbar")).to_have_attribute(
        "aria-valuenow", "100", timeout=20000
    )
    await editor.get_by_role("button", name="Close", exact=True).click()
    await page.get_by_role("button", name="Update comic metadata", exact=True).click()
    await expect(
        dialog.get_by_text("Preview again before updating files.", exact=True)
    ).to_be_visible()
    assert signatures() == before
    await preview.click()
    status = dialog.get_by_role("status")
    await expect(
        status.filter(has_text="2 ready; 0 already current; 1 need attention")
    ).to_be_visible(timeout=20000)
    await dialog.get_by_role("checkbox", name="Update Issue 1", exact=True).uncheck()
    assert signatures() == before
    await dialog.get_by_role("button", name="Update 1 selected file", exact=True).click()
    await expect(
        status.filter(has_text="1 updated; 0 already current; 0 need attention")
    ).to_be_visible(timeout=20000)
    after = signatures()
    assert after.keys() == before.keys() and {p for p in before if before[p] != after[p]} == {
        FILES[1]
    }
    with ZipFile(library / FILES[1]) as archive:
        assert {
            name: archive.read(name) for name in archive.namelist() if name != "MetronInfo.xml"
        } == unchanged
    await preview.click()
    await expect(
        status.filter(has_text="1 ready; 1 already current; 1 need attention")
    ).to_be_visible(timeout=20000)
    assert signatures() == after
