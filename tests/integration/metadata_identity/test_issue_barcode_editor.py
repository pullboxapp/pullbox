"""A retail barcode describes the edition; it never identifies a provider issue."""

from zipfile import ZipFile

import pytest

from pullbox.services.issue_metadata_editor import IssueMetadataEditError
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_comicinfo_text_editor import documents
from tests.integration.metadata_identity.test_issue_metadata_editor import edit
from tests.integration.metadata_identity.test_issue_tag_editor import preview, source

ISBN = "0-8044-2957-X"
UPC = "036000291452-00311"
NATIVE = f"<GTIN><ISBN>{ISBN}</ISBN><UPC>{UPC}</UPC></GTIN>"


async def test_choose_barcode_then_keep_that_choice_through_edits(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await source(factory, plan, path, "", "", mi_extra=NATIVE)
        original = path.read_bytes()
        with ZipFile(path) as archive:
            pages = {n: archive.read(n) for n in archive.namelist() if not n.endswith(".xml")}
        result = await preview(factory, issue)
        assert result.ready
        rows = {r.key: r for r in result.editor_fields}
        assert {"issue.isbn", "issue.upc", "issue.gtin"} <= rows.keys()
        assert rows["issue.isbn"].options[-1].value == ISBN
        assert rows["issue.upc"].options[-1].value == UPC
        assert not rows["issue.gtin"].proposed
        changes = {"gtin": {"source": "isbn"}}
        seen = await preview(factory, issue, changes)
        assert seen.ready and path.read_bytes() == original
        assert (
            next(r for r in seen.editor_fields if r.key == "issue.gtin").proposed == "9780804429573"
        )
        await edit(factory, issue, changes)
        assert documents(path)[0].findtext("GTIN") == "9780804429573"
        await edit(factory, issue, {"isbn": {"source": "custom", "value": "9780306406157"}})
        ci, mi = documents(path)
        assert ci.findtext("GTIN") == mi.findtext("GTIN/ISBN") == "9780306406157"
        assert mi.findtext("GTIN/UPC") == UPC
        await edit(factory, issue, {"gtin": {"source": "upc"}})
        await edit(factory, issue, {"isbn": {"source": "clear"}})
        ci, mi = documents(path)
        assert ci.findtext("GTIN") == "036000291452" and mi.findtext("GTIN/UPC") == UPC
        assert mi.find("GTIN/ISBN") is None and mi.findtext("IDS/ID") == "42"
        assert ci.findtext("Number") == mi.findtext("Number") == "50-X"
        await edit(factory, issue, {"upc": {"source": "clear"}})
        ci, mi = documents(path)
        assert ci.find("GTIN") is mi.find("GTIN") is None
        with ZipFile(path) as archive:
            assert pages and pages == {n: archive.read(n) for n in pages}


@pytest.mark.parametrize("generic", ["", "4006381333931"])
async def test_no_barcode_is_inferred_or_replaced(identity_probe_db, tmp_path, generic):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra=f"<GTIN>{generic}</GTIN>" if generic else "",
            mi_extra="<GTIN><ISBN>123</ISBN></GTIN>",
        )
        result = await preview(factory, issue)
        rows = {r.key: r for r in result.editor_fields}
        assert "issue.isbn" in rows and result.ready
        assert not rows["issue.isbn"].options[-1].selectable
        original = path.read_bytes()
        for field, choice in (
            ("isbn", {"source": "MetronInfo.xml"}),
            ("gtin", {"source": "isbn"}),
            ("upc", {"source": "custom", "value": "036000291453"}),
        ):
            with pytest.raises(IssueMetadataEditError):
                await preview(factory, issue, {field: choice})
        assert path.read_bytes() == original
        await edit(factory, issue, {"isbn": {"source": "custom", "value": ISBN}})
        ci, mi = documents(path)
        assert (ci.findtext("GTIN") or "") == generic and mi.findtext("GTIN/ISBN") == ISBN


async def test_conflicting_isbns_need_an_explicit_choice(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await source(
            factory, plan, path, "", "", ci_extra="<GTIN>9780306406157</GTIN>", mi_extra=NATIVE
        )
        original = path.read_bytes()
        assert (
            await preview(factory, issue, {"review": {"source": "custom", "value": "Readable"}})
        ).ready
        with pytest.raises(IssueMetadataEditError, match="ISBN"):
            await preview(factory, issue, {"isbn": {"source": "MetronInfo.xml"}})
        with pytest.raises(IssueMetadataEditError, match="ISBN"):
            await preview(factory, issue, {"gtin": {"source": "ComicInfo.xml"}})
        assert path.read_bytes() == original
        await edit(
            factory,
            issue,
            {"isbn": {"source": "custom", "value": "9781861972712"}, "gtin": {"source": "isbn"}},
        )
        ci, mi = documents(path)
        assert ci.findtext("GTIN") == mi.findtext("GTIN/ISBN") == "9781861972712"
        assert mi.findtext("GTIN/UPC") == UPC
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra="<GTIN>4006381333931</GTIN>",
            mi_extra=NATIVE.replace(ISBN, "9781861972712"),
        )
        drift = await preview(factory, issue, {"isbn": {"source": "custom", "value": ISBN}})
        assert not drift.ready and any(c.key == "xml.GTIN" for c in drift.conflicts)
