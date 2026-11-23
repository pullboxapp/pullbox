"""Explicit note corrections do not transfer or reinterpret legacy identity evidence."""

from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from pullbox.schemas.issue_metadata_editor import IssueMetadataEditorReview
from pullbox.services.archive_metadata_publication import ArchivePublicationError
from pullbox.services.issue_file_metadata import write_file_metadata
from pullbox.services.issue_metadata_editor import IssueMetadataEditError
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_issue_metadata_editor import (
    edit,
    no_control,
    no_progress,
)
from tests.integration.metadata_identity.test_issue_tag_editor import preview, source


def notes(path):
    with ZipFile(path) as archive:
        assert archive.read("page.jpg") == b"page bytes"
        return tuple(
            ET.fromstring(archive.read(doc)).findtext("Notes")
            for doc in ("ComicInfo.xml", "MetronInfo.xml")
        )


async def test_notes_review_uses_prose_and_keeps_each_documents_original_markers(
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
            ci_extra="<Notes>Scan note [CVDB0014] CVID: 15</Notes>",
            mi_extra="<Notes>Digital note [cv_issue_id:0016]</Notes>",
        )
        before = path.read_bytes(), path.stat().st_mtime_ns
        report = await preview(factory, issue)
        row = next((f for f in report.editor_fields if f.key == "issue.notes"), None)
        assert row and row.editable and row.input_type == "textarea"
        assert {o.source: o.value for o in row.options if o.source != "library"} == {
            "ComicInfo.xml": "Scan note",
            "MetronInfo.xml": "Digital note",
        }
        assert row.differs
        selection = {"notes": {"source": "MetronInfo.xml"}}
        assert (await preview(factory, issue, selection)).ready
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        await edit(factory, issue, selection)
        assert notes(path) == (
            "Digital note [CVDB0014] CVID: 15",
            "Digital note [cv_issue_id:0016]",
        )
        saved = next(
            f for f in (await preview(factory, issue)).editor_fields if f.key == "issue.notes"
        )
        assert saved.current == "Digital note" and saved.overridden and not saved.differs
        await edit(factory, issue, {"notes": {"source": "custom", "value": "Corrected\nby reader"}})
        assert notes(path) == (
            "Corrected\nby reader [CVDB0014] CVID: 15",
            "Corrected\nby reader [cv_issue_id:0016]",
        )
        await edit(factory, issue, {"notes": {"source": "clear"}})
        assert notes(path) == ("[CVDB0014] CVID: 15", "[cv_issue_id:0016]")
        signature = path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino
        assert await edit(factory, issue, {"notes": {"source": "reset"}}) == "override_released"
        assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) == signature


@pytest.mark.parametrize(
    "unsafe",
    ["new [cv_issue_id:123]", "issueid 88", "https://comicvine.gamespot.com/volume/4050-19/"],
)
async def test_custom_notes_cannot_introduce_identity_evidence(identity_probe_db, tmp_path, unsafe):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        original = path.read_bytes()
        with pytest.raises(IssueMetadataEditError, match="Notes"):
            await preview(factory, issue, {"notes": {"source": "custom", "value": unsafe}})
        assert path.read_bytes() == original


async def test_ambiguous_note_is_read_only_and_does_not_block_other_fields(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        value = "Unfinished legacy token [CVDB123"
        await source(factory, plan, path, "", "", ci_extra=f"<Notes>{value}</Notes>")
        field = next(
            f for f in (await preview(factory, issue)).editor_fields if f.key == "issue.notes"
        )
        assert not field.editable and "identity" in field.reason.lower()
        with pytest.raises(IssueMetadataEditError, match="Notes"):
            await preview(factory, issue, {"notes": {"source": "clear"}})
        await edit(factory, issue, {"title": {"source": "custom", "value": "New title"}})
        assert notes(path) == (value, None)


async def test_external_notes_change_requires_a_fresh_decision(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        choice = {"notes": {"source": "custom", "value": "Reviewed"}}
        await edit(factory, issue, choice)
        await source(factory, plan, path, "", "", ci_extra="<Notes>Outside edit [CVDB12]</Notes>")
        drift = await preview(factory, issue, {"title": {"source": "custom", "value": "Other"}})
        assert not drift.ready and any(c.key == "xml.Notes" for c in drift.conflicts)
        approved = await preview(factory, issue, choice)
        assert approved.ready
        await source(factory, plan, path, "", "", ci_extra="<Notes>Outside edit [CVDB13]</Notes>")
        changed = path.read_bytes()
        with pytest.raises(ArchivePublicationError, match="approval_changed"):
            await write_file_metadata(
                factory,
                issue,
                approved.review_key,
                uuid4(),
                limit=1000000,
                check_control=no_control,
                progress=no_progress,
                editor=IssueMetadataEditorReview.model_validate({"edits": choice}),
            )
        assert path.read_bytes() == changed
        await edit(factory, issue, choice)
        assert notes(path) == ("Reviewed [CVDB13]", "Reviewed")
        await source(factory, plan, path, "", "", ci_extra="<Notes>Mixed [CVDB13] text</Notes>")
        locked = await preview(factory, issue)
        assert not locked.ready
        assert any(
            c.key == "xml.Notes" and "correct the source" in c.note for c in locked.conflicts
        )
