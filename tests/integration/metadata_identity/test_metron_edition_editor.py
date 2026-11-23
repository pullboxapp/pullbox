"""Collection titles and manga volume labels describe one edition, not its parent."""

from zipfile import ZipFile

import pytest
from sqlalchemy import select

from pullbox.models import Issue, Series
from pullbox.services.issue_metadata_editor import IssueMetadataEditError
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_comicinfo_text_editor import documents
from tests.integration.metadata_identity.test_issue_metadata_editor import edit
from tests.integration.metadata_identity.test_issue_tag_editor import preview, source


async def test_edition_labels_are_independent_and_metron_only(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        ci_fields = "<SeriesGroup>City</SeriesGroup><Manga>Unknown</Manga>"
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra=ci_fields,
            mi_extra="<CollectionTitle>The City &amp; Beyond</CollectionTitle>"
            "<MangaVolume>006-A</MangaVolume>",
        )
        original = path.read_bytes()
        result = await preview(factory, issue)
        assert result.ready
        rows = {r.key: r for r in result.editor_fields}
        for field, value in (("collection_title", "The City & Beyond"), ("manga_volume", "006-A")):
            assert f"issue.{field}" in rows
            row = rows[f"issue.{field}"]
            assert row.editable and row.differs
            assert {o.source: o.value for o in row.options} == {
                "library": "",
                "MetronInfo.xml": value,
            }
            with pytest.raises(IssueMetadataEditError, match=r"MetronInfo.xml only"):
                await preview(factory, issue, {field: {"source": "ComicInfo.xml"}})
        changes = {
            "collection_title": {"source": "MetronInfo.xml"},
            "manga_volume": {"source": "custom", "value": "007-B"},
        }
        assert (await preview(factory, issue, changes)).ready and path.read_bytes() == original
        await edit(factory, issue, changes)
        ci, mi = documents(path)
        assert ci.find("CollectionTitle") is ci.find("MangaVolume") is None
        assert ci.findtext("Manga") == "Unknown" and ci.findtext("SeriesGroup") == "City"
        assert (
            mi.findtext("MangaVolume") == "007-B"
            and mi.findtext("CollectionTitle") == "The City & Beyond"
        )
        assert mi.findtext("Number") == ci.findtext("Number") == "50-X"
        assert mi.findtext("Series/Name") == "Example" and mi.find("Series").get("id") == "12"
        assert mi.findtext("IDS/ID") == "42"
        async with factory() as session:
            assert (
                await session.execute(select(Series.title, Issue.issue_number_text).join(Issue))
            ).one() == ("Example", "50-X")
        saved = await preview(factory, issue)
        assert saved.ready and all(
            r.overridden and not r.differs
            for r in saved.editor_fields
            if r.key in {"issue.collection_title", "issue.manga_volume"}
        )
        await edit(factory, issue, {"collection_title": {"source": "clear"}})
        mi = documents(path)[1]
        assert mi.find("CollectionTitle") is None and mi.findtext("MangaVolume") == "007-B"
        await source(factory, plan, path, "", "", ci_extra=ci_fields)
        drift = await preview(factory, issue)
        assert not drift.ready and any(
            c.key == "xml.MangaVolume" and "MetronInfo.xml" in c.note for c in drift.conflicts
        )
        await edit(factory, issue, {"manga_volume": {"source": "library"}})
        assert documents(path)[1].findtext("MangaVolume") == "007-B"
        sig = path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino
        assert (
            await edit(factory, issue, {"manga_volume": {"source": "reset"}}) == "override_released"
        )
        assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) == sig


async def test_annotated_edition_text_is_preserved_during_other_corrections(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await source(
            factory,
            plan,
            path,
            "",
            "",
            mi_extra="<CollectionTitle>Part <!--ordinal-->III</CollectionTitle>",
        )
        before = path.read_bytes()
        report = await preview(factory, issue)
        rows = {r.key: r for r in report.editor_fields}
        assert report.ready and "issue.collection_title" in rows
        assert (
            not rows["issue.collection_title"].editable
            and "annotation" in rows["issue.collection_title"].reason
        )
        with pytest.raises(IssueMetadataEditError, match="read-only"):
            await preview(factory, issue, {"collection_title": {"source": "clear"}})
        for value in ("1\n2", " " * 2, "x" * 4097, "cv_issue_id:123"):
            with pytest.raises(IssueMetadataEditError):
                await preview(
                    factory, issue, {"manga_volume": {"source": "custom", "value": value}}
                )
        assert path.read_bytes() == before
        await edit(factory, issue, {"manga_volume": {"source": "custom", "value": "001.5"}})
        with ZipFile(path) as archive:
            assert b"<CollectionTitle>Part <!--ordinal-->III</CollectionTitle>" in archive.read(
                "MetronInfo.xml"
            )
        assert documents(path)[1].findtext("MangaVolume") == "001.5"
