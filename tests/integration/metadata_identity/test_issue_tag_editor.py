"""Issue tag edits preserve pages and IDs without assigning an ID by position."""

from datetime import UTC, datetime
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from pullbox.models import LibraryFile
from pullbox.schemas.issue_metadata_editor import IssueMetadataEditorReview
from pullbox.services.archive_metadata_preservation import ArchiveMetadataRenderError
from pullbox.services.archive_metadata_publication import ArchivePublicationError
from pullbox.services.issue_file_metadata import prepare_file_metadata, write_file_metadata
from pullbox.services.issue_metadata_editor import IssueMetadataEditError
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_issue_metadata_editor import (
    edit,
    no_control,
    no_progress,
)


async def source(factory, plan, path, ci_tags, mi_tags, *, ci_extra="", mi_extra="", number="50-X"):
    with ZipFile(path, "w") as archive:
        archive.writestr("page.jpg", b"page bytes")
        archive.writestr(
            "ComicInfo.xml",
            f"<ComicInfo><Number>{number}</Number>{ci_extra}<Tags>{ci_tags}</Tags></ComicInfo>",
        )
        archive.writestr(
            "MetronInfo.xml",
            '<MetronInfo><IDS><ID source="Metron" primary="true">42</ID></IDS>'
            '<Series id="12"><Name>Example</Name></Series>'
            f"<Number>{number}</Number>{mi_extra}<Tags>{mi_tags}</Tags></MetronInfo>",
        )
    async with factory.begin() as session:
        row = await session.get(LibraryFile, plan.target.binding.library_file_id)
        row.file_size = path.stat().st_size
        row.file_modified_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)


async def preview(factory, issue_id, edits=None):
    review = IssueMetadataEditorReview.model_validate({"edits": edits or {}})
    async with factory() as session:
        return (await prepare_file_metadata(session, issue_id, editor=review)).preview


async def test_genre_edit_preserves_ids_by_field_and_requires_fresh_review(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id

        async def replace(ci, mi):
            await source(
                factory,
                plan,
                path,
                "Noir",
                '<Tag id="90">Noir</Tag>',
                ci_extra=f"<Genre>{ci}</Genre>",
                mi_extra=f"<Genres>{mi}</Genres>",
            )

        await replace("Crime, Mystery", '<Genre id="9">Crime</Genre><Genre id="10">Mystery</Genre>')
        field = next(
            f
            for f in (await preview(factory, issue)).editor_fields
            if f.label in {"Genre", "Genres"}
        )
        assert field.editable and field.key == "issue.genres"
        before = path.read_bytes()
        edits = {"genres": {"source": "custom", "value": "Noir\nCrime"}}
        report = await preview(factory, issue, edits)
        assert report.ready and path.read_bytes() == before
        assert await edit(factory, issue, edits) == "written"
        with ZipFile(path) as archive:
            ci = ET.fromstring(archive.read("ComicInfo.xml"))
            mi = ET.fromstring(archive.read("MetronInfo.xml"))
            assert (ci.findtext("Genre"), ci.findtext("Tags")) == ("Noir, Crime", "Noir")
            assert [(n.text, n.get("id")) for n in mi.findall("Genres/Genre")] == [
                ("Noir", None),
                ("Crime", "9"),
            ]
            assert [(n.text, n.get("id")) for n in mi.findall("Tags/Tag")] == [("Noir", "90")]
            assert archive.read("page.jpg") == b"page bytes"
        await replace("Drama", '<Genre id="17">Drama</Genre>')
        drift = await preview(factory, issue, {"title": {"source": "custom", "value": "Title"}})
        assert not drift.ready and any(c.key == "xml.Genre" for c in drift.conflicts)
        await edit(factory, issue, {"genres": {"source": "MetronInfo.xml"}})
        await edit(factory, issue, {"genres": {"source": "clear"}})
        with ZipFile(path) as archive:
            assert ET.fromstring(archive.read("ComicInfo.xml")).find("Genre") is None
            assert ET.fromstring(archive.read("MetronInfo.xml")).find("Genres") is None
        signature = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        assert await edit(factory, issue, {"genres": {"source": "reset"}}) == "override_released"
        assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == signature


@pytest.mark.parametrize(
    "field,ci_tag,parent,child",
    [
        ("genres", "Genre", "Genres", "Genre"),
        ("characters", "Characters", "Characters", "Character"),
        ("teams", "Teams", "Teams", "Team"),
        ("locations", "Locations", "Locations", "Location"),
    ],
)
async def test_list_selection_never_splits_or_guesses_an_id(
    identity_probe_db, tmp_path, field, ci_tag, parent, child
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id

        async def replace(mi):
            await source(
                factory,
                plan,
                path,
                "Keep",
                '<Tag id="30">Keep</Tag>',
                ci_extra=f"<{ci_tag}>Mystery</{ci_tag}>",
                mi_extra=f"<{parent}>{mi}</{parent}>",
            )

        await replace(f'<{child} id="3">Crime, Noir</{child}>')
        original = path.read_bytes()
        report = await preview(factory, issue)
        row = next(f for f in report.editor_fields if f.key == f"issue.{field}")
        assert not report.ready and any(
            o.value == "Crime, Noir" and not o.selectable for o in row.options
        )
        choice = {field: {"source": "ComicInfo.xml"}}
        assert (await preview(factory, issue, choice)).ready
        assert path.read_bytes() == original
        await replace(f'<{child} id="3">Mystery</{child}><{child} id="8">Mystery</{child}>')
        original = path.read_bytes()
        with pytest.raises(ArchiveMetadataRenderError, match="ambiguous"):
            await preview(factory, issue, choice)
        async with factory() as session:
            with pytest.raises(ArchiveMetadataRenderError, match="ambiguous"):
                await prepare_file_metadata(
                    session, issue, choices={f"xml.{ci_tag}": "ComicInfo.xml"}
                )
        assert path.read_bytes() == original
        await replace(f'<{child} id="8">Mystery</{child}>')
        assert (
            await edit(factory, issue, {field: {"source": "custom", "value": "Keep\nMystery"}})
            == "written"
        )
        with ZipFile(path) as archive:
            mi = ET.fromstring(archive.read("MetronInfo.xml"))
            assert [(n.text, n.get("id")) for n in mi.findall(f"{parent}/{child}")] == [
                ("Keep", None),
                ("Mystery", "8"),
            ]
            assert mi.find("Tags/Tag").get("id") == "30"
            assert ET.fromstring(archive.read("ComicInfo.xml")).findtext(ci_tag) == "Keep, Mystery"
            assert archive.read("page.jpg") == b"page bytes"


async def test_edit_tags_keeps_existing_ids_and_reopens_the_saved_selection(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        await source(
            factory, plan, path, "Keep, Old", '<Tag id="34">Keep</Tag><Tag id="56">Old</Tag>'
        )
        before = path.read_bytes()
        inventory = (await preview(factory, issue_id)).editor_fields
        assert any(f.key == "issue.tags" and f.editable for f in inventory)
        edits = {"tags": {"source": "custom", "value": "New\nKeep"}}
        report = await preview(factory, issue_id, edits)
        assert report.ready and {c.document for c in report.changes if "/Tags[" in c.field} == {
            "ComicInfo.xml",
            "MetronInfo.xml",
        }
        assert path.read_bytes() == before
        assert await edit(factory, issue_id, edits) == "written"
        with ZipFile(path) as archive:
            assert archive.read("page.jpg") == b"page bytes"
            assert ET.fromstring(archive.read("ComicInfo.xml")).findtext("Tags") == "New, Keep"
            nodes = ET.fromstring(archive.read("MetronInfo.xml")).findall("Tags/Tag")
            assert [(n.text, n.get("id")) for n in nodes] == [("New", None), ("Keep", "34")]
        field = next(
            f for f in (await preview(factory, issue_id)).editor_fields if f.key == "issue.tags"
        )
        assert field.current == "New\nKeep" and field.overridden and not field.differs
        await edit(factory, issue_id, {"tags": {"source": "clear"}})
        with ZipFile(path) as archive:
            for name in ("ComicInfo.xml", "MetronInfo.xml"):
                assert ET.fromstring(archive.read(name)).find("Tags") is None
        signature = path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns
        assert await edit(factory, issue_id, {"tags": {"source": "reset"}}) == "override_released"
        assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == signature


async def test_file_choice_protects_external_changes_and_rechecks_id_only_changes(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id

        async def replace(mi_id, ci):
            await source(
                factory, plan, path, ci, f'<Tag id="{mi_id}">Keep</Tag><Tag>Only in Metron</Tag>'
            )

        await replace("34", "Keep, From ComicInfo")
        assert not (await preview(factory, issue_id)).ready
        selection = {"tags": {"source": "ComicInfo.xml"}}
        initial = await preview(factory, issue_id, selection)
        assert initial.ready
        await replace("35", "Keep, From ComicInfo")
        changed = path.read_bytes()
        with pytest.raises(ArchivePublicationError, match="approval_changed"):
            await write_file_metadata(
                factory,
                issue_id,
                initial.review_key,
                uuid4(),
                limit=1000000,
                check_control=no_control,
                progress=no_progress,
                editor=IssueMetadataEditorReview.model_validate({"edits": selection}),
            )
        assert path.read_bytes() == changed
        await edit(factory, issue_id, selection)
        with ZipFile(path) as archive:
            assert [
                (n.text, n.get("id"))
                for n in ET.fromstring(archive.read("MetronInfo.xml")).findall("Tags/Tag")
            ] == [("Keep", "35"), ("From ComicInfo", None)]
        await source(factory, plan, path, "External", '<Tag id="77">External</Tag>')
        changed = path.read_bytes()
        report = await preview(factory, issue_id, {"title": {"source": "custom", "value": "Other"}})
        assert not report.ready and any(c.key == "xml.Tags" for c in report.conflicts)
        assert not (await preview(factory, issue_id, {"tags": {"source": "reset"}})).ready
        assert path.read_bytes() == changed
        await edit(factory, issue_id, {"tags": {"source": "MetronInfo.xml"}})
        row = next(
            f for f in (await preview(factory, issue_id)).editor_fields if f.key == "issue.tags"
        )
        assert row.current == "External" and row.overridden


async def test_unrepresentable_tag_is_not_split_and_ambiguous_ids_are_not_reassigned(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        await source(factory, plan, path, "Keep", '<Tag id="5">Salt, pepper</Tag>')
        original = path.read_bytes()
        report = await preview(factory, issue_id)
        row = next(f for f in report.editor_fields if f.key == "issue.tags")
        assert not report.ready and any(
            o.value == "Salt, pepper" and not o.selectable for o in row.options
        )
        with pytest.raises(IssueMetadataEditError):
            await preview(factory, issue_id, {"tags": {"source": "MetronInfo.xml"}})
        assert (await preview(factory, issue_id, {"tags": {"source": "ComicInfo.xml"}})).ready
        assert path.read_bytes() == original
        await source(
            factory, plan, path, "Keep, Other", '<Tag id="4">Keep</Tag><Tag id="5">Keep</Tag>'
        )
        original = path.read_bytes()
        with pytest.raises(ArchiveMetadataRenderError, match="ambiguous_list_id"):
            await preview(factory, issue_id, {"tags": {"source": "ComicInfo.xml"}})
        async with factory() as session:
            with pytest.raises(ArchiveMetadataRenderError, match="ambiguous_list_id"):
                await prepare_file_metadata(
                    session, issue_id, choices={"xml.Tags": "ComicInfo.xml"}
                )
        assert path.read_bytes() == original
