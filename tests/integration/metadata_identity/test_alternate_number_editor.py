"""Alternate numbering is descriptive, not an issue identity or a catalog count."""

import pytest
from sqlalchemy import select

from pullbox.models import Issue
from pullbox.services.issue_metadata_editor import IssueMetadataEditError
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_comicinfo_text_editor import documents
from tests.integration.metadata_identity.test_issue_metadata_editor import edit
from tests.integration.metadata_identity.test_issue_tag_editor import preview, source


async def test_alternate_numbers_belong_to_their_own_document(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra="<AlternateSeries>Crossover</AlternateSeries>"
            "<AlternateNumber>013-A</AlternateNumber><AlternateCount>+0018</AlternateCount>",
            mi_extra="<AlternativeNumber>150</AlternativeNumber>",
        )
        original = path.read_bytes()
        report = await preview(factory, issue)
        assert report.ready
        rows = {r.key: r for r in report.editor_fields}
        for field, doc, value in (
            ("alternate_series", "ComicInfo.xml", "Crossover"),
            ("alternate_number", "ComicInfo.xml", "013-A"),
            ("alternate_count", "ComicInfo.xml", "18"),
            ("alternative_number", "MetronInfo.xml", "150"),
        ):
            assert "issue." + field in rows
            assert {o.source: o.value for o in rows["issue." + field].options} == {
                "library": "",
                doc: value,
            }
        edits = {
            "alternate_series": {"source": "ComicInfo.xml"},
            "alternate_number": {"source": "custom", "value": "003-b"},
            "alternate_count": {"source": "ComicInfo.xml"},
            "alternative_number": {"source": "custom", "value": "0150-X"},
        }
        assert (await preview(factory, issue, edits)).ready and path.read_bytes() == original
        await edit(factory, issue, edits)
        ci, mi = documents(path)
        assert (
            ci.findtext("AlternateNumber") == "003-b"
            and mi.findtext("AlternativeNumber") == "0150-X"
        )
        assert ci.findtext("AlternateCount") == "+0018"
        assert ci.findtext("Number") == mi.findtext("Number") == "50-X"
        assert mi.find("AlternateSeries") is ci.find("AlternativeNumber") is None
        assert mi.findtext("IDS/ID") == "42" and mi.find("Series").get("id") == "12"
        async with factory() as session:
            assert (
                await session.scalar(select(Issue.issue_number_text).where(Issue.id == issue))
                == "50-X"
            )
        assert all(
            not r.differs and r.overridden
            for r in (await preview(factory, issue)).editor_fields
            if r.key.removeprefix("issue.") in edits
        )
        await edit(factory, issue, {"alternate_count": {"source": "clear"}})
        ci, mi = documents(path)
        assert ci.find("AlternateCount") is None and ci.findtext("AlternateSeries") == "Crossover"
        assert (
            ci.findtext("AlternateNumber") == "003-b"
            and mi.findtext("AlternativeNumber") == "0150-X"
        )


async def test_bad_count_does_not_block_other_fields_or_become_a_catalog_count(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await source(factory, plan, path, "", "", ci_extra="<AlternateCount>many</AlternateCount>")
        result = await preview(factory, issue)
        assert result.ready
        row = next(r for r in result.editor_fields if r.key == "issue.alternate_count")
        assert any(o.source == "ComicInfo.xml" and not o.selectable for o in row.options)
        for field, choice in (
            ("alternate_count", {"source": "ComicInfo.xml"}),
            ("alternate_count", {"source": "custom", "value": "2147483648"}),
            ("alternate_count", {"source": "custom", "value": "-2"}),
            ("alternate_number", {"source": "MetronInfo.xml"}),
            ("alternative_number", {"source": "ComicInfo.xml"}),
            ("alternate_series", {"source": "custom", "value": "Team [cvdb123]"}),
        ):
            with pytest.raises(IssueMetadataEditError):
                await preview(factory, issue, {field: choice})
        await edit(factory, issue, {"alternative_number": {"source": "custom", "value": "-1"}})
        assert documents(path)[0].findtext("AlternateCount") == "many"
        for count in ("-1", "0"):
            await edit(factory, issue, {"alternate_count": {"source": "custom", "value": count}})
            assert documents(path)[0].findtext("AlternateCount") == count
        await source(factory, plan, path, "", "", ci_extra="<AlternateCount>0</AlternateCount>")
        stale = await preview(factory, issue)
        assert not stale.ready
        conflict = next(c for c in stale.conflicts if c.key == "xml.AlternativeNumber")
        assert "MetronInfo.xml" in conflict.note
        await edit(factory, issue, {"alternative_number": {"source": "clear"}})
        assert (await preview(factory, issue)).ready


async def test_annotated_metron_number_is_not_partially_replaced(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await source(
            factory,
            plan,
            path,
            "",
            "",
            mi_extra="<AlternativeNumber>15<!--old-->0</AlternativeNumber>",
        )
        report = await preview(factory, issue)
        row = next(r for r in report.editor_fields if r.key == "issue.alternative_number")
        assert report.ready and not row.editable
        with pytest.raises(IssueMetadataEditError, match="read-only"):
            await preview(
                factory, issue, {"alternative_number": {"source": "custom", "value": "999"}}
            )
        await edit(factory, issue, {"alternate_number": {"source": "custom", "value": "000.5"}})
        assert documents(path)[1].findtext("AlternativeNumber") == "150"
