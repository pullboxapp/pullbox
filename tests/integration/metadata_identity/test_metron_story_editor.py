"""An anthology's contents are not its issue title or evidence of a complete issue."""

from zipfile import ZipFile

import pytest

from pullbox.models import Issue
from pullbox.services.archive_metadata_binding import ArchiveMetadataBindingError
from pullbox.services.archive_metadata_preservation import ArchiveMetadataRenderError
from pullbox.services.issue_metadata_editor import IssueMetadataEditError
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_comicinfo_text_editor import documents
from tests.integration.metadata_identity.test_issue_metadata_editor import edit
from tests.integration.metadata_identity.test_issue_tag_editor import preview, source


async def test_anthology_contents_and_issue_title_remain_independent(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        async with factory.begin() as session:
            (await session.get(Issue, issue)).title = "Annual"

        def contents():
            ci, mi = documents(path)
            assert ci.find("Stories") is None and ci.findtext("Title") == "Annual"
            assert mi.findtext("IDS/ID") == "42"
            with ZipFile(path) as archive:
                assert archive.read("page.jpg") == b"page bytes"
            return [(n.text, n.get("id")) for n in mi.findall("Stories/Story")]

        async def replace(stories):
            await source(
                factory, plan, path, "", "", ci_extra="<Title>Annual</Title>", mi_extra=stories
            )

        await replace(
            '<Stories><Story id="a">First, Part I</Story><Story id="b">Second</Story></Stories>'
        )
        before = path.read_bytes()
        result = await preview(factory, issue)
        assert result.ready and path.read_bytes() == before
        row = next(r for r in result.editor_fields if r.key == "issue.story_titles")
        assert row.editable and row.options[-1].value == "First, Part I\nSecond"
        await edit(factory, issue, {"notes": {"source": "custom", "value": "Library note"}})
        assert contents() == [("First, Part I", "a"), ("Second", "b")]
        await edit(
            factory, issue, {"story_titles": {"source": "custom", "value": "Second\nRenamed"}}
        )
        assert contents() == [("Second", "b"), ("Renamed", None)]
        await edit(factory, issue, {"story_titles": {"source": "custom", "value": "Second"}})
        assert contents() == [("Second", "b")]
        assert (await preview(factory, issue)).ready
        await edit(
            factory, issue, {"description": {"source": "custom", "value": "Collection of stories"}}
        )
        assert contents() == [("Second", "b")]
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra="<Title>Annual</Title><Notes>Library note</Notes>",
            mi_extra='<Stories><Story id="b">Changed elsewhere</Story></Stories>'
            "<Notes>Library note</Notes>",
        )
        changed = await preview(factory, issue)
        assert not changed.ready and {c.key for c in changed.conflicts} == {"xml.Stories"}
        await edit(factory, issue, {"story_titles": {"source": "clear"}})
        assert contents() == [] and documents(path)[1].find("Stories") is None
        assert (await preview(factory, issue)).ready
        async with factory() as session:
            assert (await session.get(Issue, issue)).title == "Annual"


async def test_legacy_single_story_still_needs_title_review_and_extracts_stay_blocked(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        async with factory.begin() as session:
            (await session.get(Issue, issue)).title = "Annual"
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra="<Title>Annual</Title>",
            mi_extra="<Stories><Story>Story title</Story></Stories>",
        )
        result = await preview(factory, issue)
        assert not result.ready and any(c.key == "issue.title" for c in result.conflicts)
        await edit(factory, issue, {"story_titles": {"source": "MetronInfo.xml"}})
        ci, mi = documents(path)
        assert ci.findtext("Title") == "Annual" and mi.findtext("Stories/Story") == "Story title"
        # Native story titles can exceed the tracked issue title's 500-character limit.
        long_title = "S" * 501
        await edit(factory, issue, {"story_titles": {"source": "custom", "value": long_title}})
        assert (await preview(factory, issue)).ready
        ci, mi = documents(path)
        assert ci.findtext("Title") == "Annual" and mi.findtext("Stories/Story") == long_title
        untouched = path.read_bytes(), path.stat().st_mtime_ns
        assert (
            await edit(factory, issue, {"story_titles": {"source": "reset"}}) == "override_released"
        )
        assert (path.read_bytes(), path.stat().st_mtime_ns) == untouched
        assert (await preview(factory, issue)).ready
        # Replacing the title list is never approval to relabel a story-only extract.
        await source(
            factory,
            plan,
            path,
            "",
            "",
            mi_extra="<Stories><Story>Story title</Story></Stories>",
            number="158 (B Story)",
        )
        with pytest.raises(ArchiveMetadataBindingError, match="partial_issue"):
            await preview(factory, issue, {"story_titles": {"source": "clear"}})


async def test_annotated_story_cannot_be_destroyed_by_editor(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        xml = (
            '<Stories><Story id="a">First<!--original credit--></Story>'
            "<Story>Second</Story></Stories>"
        )
        await source(factory, plan, path, "", "", mi_extra=xml)
        result = await preview(factory, issue)
        row = next(r for r in result.editor_fields if r.key == "issue.story_titles")
        assert result.ready and not row.editable
        with pytest.raises(IssueMetadataEditError, match="read-only"):
            await preview(factory, issue, {"story_titles": {"source": "clear"}})
        await edit(factory, issue, {"notes": {"source": "custom", "value": "Note"}})
        with ZipFile(path) as archive:
            assert xml.encode() in archive.read("MetronInfo.xml")


async def test_valid_long_story_is_not_an_invalid_issue_title(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        title = "S" * 4096
        await source(
            factory,
            plan,
            path,
            "",
            "",
            mi_extra=f'<Stories><Story id="a">{title}</Story></Stories>',
        )
        assert (await preview(factory, issue)).ready
        await edit(factory, issue, {"notes": {"source": "custom", "value": "Keep the story"}})
        ci, mi = documents(path)
        assert not ci.findtext("Title")
        assert mi.findtext("Stories/Story") == title and mi.find("Stories/Story").get("id") == "a"


async def test_story_review_never_assigns_an_ambiguous_id_or_resolves_a_comic_title(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        async with factory.begin() as session:
            (await session.get(Issue, issue)).title = "Annual"
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra="<Title>Annual</Title>",
            mi_extra='<Stories><Story id="a">Twin</Story><Story id="b">Twin</Story></Stories>',
        )
        before = path.read_bytes()
        with pytest.raises(ArchiveMetadataRenderError, match="ambiguous_list_id"):
            await preview(factory, issue, {"story_titles": {"source": "custom", "value": "Twin"}})
        assert path.read_bytes() == before
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra="<Title>Different issue title</Title>",
            mi_extra="<Stories><Story>First</Story><Story>Second</Story></Stories>",
        )
        before = path.read_bytes()
        result = await preview(factory, issue, {"story_titles": {"source": "MetronInfo.xml"}})
        assert not result.ready and any(c.key == "issue.title" for c in result.conflicts)
        assert path.read_bytes() == before
