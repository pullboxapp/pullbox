"""Editing arc tags on an issue must not edit the tracked reading list."""

from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from sqlalchemy import select

from pullbox.models.story_arc import IssueStoryArc, StoryArc
from pullbox.schemas.issue_metadata_editor import IssueMetadataEditorReview
from pullbox.services.archive_metadata_preservation import ArchiveMetadataRenderError
from pullbox.services.archive_metadata_publication import ArchivePublicationError
from pullbox.services.issue_file_metadata import prepare_file_metadata, write_file_metadata
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_issue_metadata_editor import (
    edit,
    no_control,
    no_progress,
)
from tests.integration.metadata_identity.test_issue_tag_editor import preview, source


async def test_edit_arc_pairs_preserves_ids_pages_and_tracked_order(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        async with factory.begin() as session:
            arc = StoryArc(name="Keep", normalized_name="keep")
            session.add(arc)
            await session.flush()
            session.add(
                IssueStoryArc(
                    story_arc_id=arc.id, issue_id=issue, sequence_number=6, source_ordinal=6
                )
            )
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra="<StoryArc>Old, Keep</StoryArc><StoryArcNumber>1, 6</StoryArcNumber>",
            mi_extra='<Arcs><Arc id="12"><Name>Old</Name><Number>1</Number></Arc>'
            '<Arc id="15"><Name>Keep</Name><Number>6</Number></Arc></Arcs>',
        )
        initial = await preview(factory, issue)
        assert any(f.key == "issue.story_arcs" and f.editable for f in initial.editor_fields)
        original = path.read_bytes()
        pairs = [{"name": "Keep", "position": 3}, {"name": "New | Side story", "position": None}]
        custom = {"story_arcs": {"source": "custom", "value": pairs}}
        assert (await preview(factory, issue, custom)).ready
        assert path.read_bytes() == original
        await edit(factory, issue, custom)
        with ZipFile(path) as z:
            ci = ET.fromstring(z.read("ComicInfo.xml"))
            mi = ET.fromstring(z.read("MetronInfo.xml"))
            assert ci.findtext("StoryArc") == "Keep, New | Side story"
            assert ci.find("StoryArcNumber") is None
            assert [
                (a.findtext("Name"), a.findtext("Number"), a.get("id"))
                for a in mi.findall("Arcs/Arc")
            ] == [("Keep", "3", "15"), ("New | Side story", None, None)]
            assert z.read("page.jpg") == b"page bytes"
        saved = next(
            f for f in (await preview(factory, issue)).editor_fields if f.key == "issue.story_arcs"
        )
        assert saved.overridden and not saved.differs
        pairs[1]["position"] = 1
        await edit(factory, issue, custom)
        with ZipFile(path) as z:
            assert ET.fromstring(z.read("ComicInfo.xml")).findtext("StoryArcNumber") == "3, 1"
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra="<StoryArc>External</StoryArc>",
            mi_extra="<Arcs><Arc><Name>External</Name></Arc></Arcs>",
        )
        external = path.read_bytes()
        unrelated = await preview(
            factory, issue, {"title": {"source": "custom", "value": "New title"}}
        )
        assert not unrelated.ready and any(c.key == "xml.StoryArc" for c in unrelated.conflicts)
        assert path.read_bytes() == external
        await edit(factory, issue, {"story_arcs": {"source": "clear"}})
        with ZipFile(path) as z:
            assert ET.fromstring(z.read("MetronInfo.xml")).find("Arcs") is None
            assert ET.fromstring(z.read("ComicInfo.xml")).find("StoryArc") is None
        signature = path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns
        assert (
            await edit(factory, issue, {"story_arcs": {"source": "reset"}}) == "override_released"
        )
        assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == signature
        async with factory() as session:
            assert (
                await session.execute(
                    select(StoryArc.name, IssueStoryArc.sequence_number).join(
                        IssueStoryArc, StoryArc.id == IssueStoryArc.story_arc_id
                    )
                )
            ).all() == [("Keep", 6)]


@pytest.mark.parametrize("numbers", ["3", "3,,5"])
async def test_malformed_arc_list_is_reviewable_and_conflicting_ids_are_never_guessed(
    identity_probe_db, tmp_path, numbers
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
            ci_extra=f"<StoryArc>One, Two</StoryArc><StoryArcNumber>{numbers}</StoryArcNumber>",
            mi_extra='<Arcs><Arc id="4"><Name>One</Name><Number>2</Number></Arc></Arcs>',
        )
        before = path.read_bytes()
        async with factory() as session:
            with pytest.raises(ArchiveMetadataRenderError, match="unsupported_content"):
                await prepare_file_metadata(session, issue)
        report = await preview(factory, issue)
        field = next(f for f in report.editor_fields if f.key == "issue.story_arcs")
        assert not report.ready and any(
            o.source == "ComicInfo.xml" and not o.selectable for o in field.options
        )
        assert path.read_bytes() == before
        await edit(factory, issue, {"story_arcs": {"source": "MetronInfo.xml"}})
        with ZipFile(path) as z:
            assert ET.fromstring(z.read("ComicInfo.xml")).findtext("StoryArcNumber") == "2"
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra="<StoryArc>One</StoryArc>",
            mi_extra='<Arcs><Arc id="4"><Name>One</Name></Arc>'
            '<Arc id="6"><Name>One</Name></Arc></Arcs>',
        )
        before = path.read_bytes()
        with pytest.raises(ArchiveMetadataRenderError, match="ambiguous"):
            await preview(factory, issue, {"story_arcs": {"source": "ComicInfo.xml"}})
        assert path.read_bytes() == before


async def test_id_only_change_invalidates_arc_approval(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id

        async def replace(resource_id):
            await source(
                factory,
                plan,
                path,
                "",
                "",
                ci_extra="<StoryArc>One</StoryArc>",
                mi_extra=f'<Arcs><Arc id="{resource_id}"><Name>One</Name></Arc></Arcs>',
            )

        edits = {"story_arcs": {"source": "custom", "value": [{"name": "One", "position": 7}]}}
        await replace(10)
        previewed = await preview(factory, issue, edits)
        await replace(11)
        before = path.read_bytes()
        with pytest.raises(ArchivePublicationError, match="approval_changed"):
            await write_file_metadata(
                factory,
                issue,
                previewed.review_key,
                uuid4(),
                limit=1000000,
                check_control=no_control,
                progress=no_progress,
                editor=IssueMetadataEditorReview.model_validate({"edits": edits}),
            )
        assert path.read_bytes() == before
        await edit(factory, issue, edits)
        with ZipFile(path) as z:
            assert ET.fromstring(z.read("MetronInfo.xml")).find("Arcs/Arc").get("id") == "11"
