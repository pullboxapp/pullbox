"""Reprint descriptions are not issue identities, and IDs belong to names, not positions."""

from uuid import uuid4
from zipfile import ZipFile

import pytest

from pullbox.schemas.issue_metadata_editor import IssueMetadataEditorReview
from pullbox.services.archive_metadata_preservation import ArchiveMetadataRenderError
from pullbox.services.archive_metadata_publication import ArchivePublicationError
from pullbox.services.issue_file_metadata import file_metadata_error, write_file_metadata
from pullbox.services.issue_metadata_editor import IssueMetadataEditError
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_comicinfo_text_editor import documents
from tests.integration.metadata_identity.test_issue_metadata_editor import (
    edit,
    no_control,
    no_progress,
)
from tests.integration.metadata_identity.test_issue_tag_editor import preview, source


def entries(path):
    ci, mi = documents(path)
    assert ci.find("Reprints") is None and mi.findtext("Number") == "50-X"
    assert mi.findtext("IDS/ID") == "42" and mi.find("Series").get("id") == "12"
    return [(node.text, node.get("id")) for node in mi.findall("Reprints/Reprint")]


async def test_clear_cannot_use_old_approval_after_only_reprint_id_changes(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        xml = '<Reprints><Reprint id="11">Twin</Reprint></Reprints>'
        await source(factory, plan, path, "", "", mi_extra=xml)
        changes = {"reprints": {"source": "clear"}}
        approved = await preview(factory, issue, changes)
        assert approved.ready
        await source(factory, plan, path, "", "", mi_extra=xml.replace('"11"', '"22"'))
        sig = path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino
        with pytest.raises(ArchivePublicationError, match="approval_changed"):
            await write_file_metadata(
                factory,
                issue,
                approved.review_key,
                uuid4(),
                limit=1000000,
                check_control=no_control,
                progress=no_progress,
                editor=IssueMetadataEditorReview.model_validate({"edits": changes}),
            )
        assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) == sig
        assert entries(path) == [("Twin", "22")]


async def test_reprints_can_be_adopted_reordered_corrected_and_cleared(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        xml = (
            '<Reprints><Reprint id="a">City, Vol. 1 #3</Reprint>'
            '<Reprint id="b">Night #2</Reprint><Reprint id="c">Typo</Reprint></Reprints>'
        )
        await source(factory, plan, path, "", "", mi_extra=xml)
        before = path.read_bytes()
        result = await preview(factory, issue)
        rows = {r.key: r for r in result.editor_fields}
        assert result.ready and "issue.reprints" in rows
        row = rows["issue.reprints"]
        assert row.editable and row.differs and row.input_type == "textarea"
        assert {o.source: o.value for o in row.options} == {
            "library": "",
            "MetronInfo.xml": "City, Vol. 1 #3\nNight #2\nTypo",
        }
        adopt = {"reprints": {"source": "MetronInfo.xml"}}
        assert (await preview(factory, issue, adopt)).ready and path.read_bytes() == before
        await edit(factory, issue, adopt)
        assert entries(path) == [("City, Vol. 1 #3", "a"), ("Night #2", "b"), ("Typo", "c")]
        await edit(
            factory,
            issue,
            {
                "reprints": {
                    "source": "custom",
                    "value": "Night #2\nCorrected\nCity, Vol. 1 #3\nNew, Part II",
                }
            },
        )
        assert entries(path) == [
            ("Night #2", "b"),
            ("Corrected", None),
            ("City, Vol. 1 #3", "a"),
            ("New, Part II", None),
        ]
        saved = await preview(factory, issue)
        row = next(r for r in saved.editor_fields if r.key == "issue.reprints")
        assert saved.ready and row.overridden and not row.differs
        await source(factory, plan, path, "", "", mi_extra=xml)
        drift = await preview(factory, issue)
        assert not drift.ready and any(c.key == "xml.Reprints" for c in drift.conflicts)
        await edit(factory, issue, {"reprints": {"source": "clear"}})
        assert documents(path)[1].find("Reprints") is None
        sig = path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino
        assert await edit(factory, issue, {"reprints": {"source": "reset"}}) == "override_released"
        assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) == sig


async def test_ambiguous_or_annotated_reprints_do_not_force_unrelated_review(
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
            mi_extra=(
                '<Reprints><Reprint id="1">Twin</Reprint><Reprint id="2">Twin</Reprint></Reprints>'
            ),
        )
        result = await preview(factory, issue)
        rows = {r.key: r for r in result.editor_fields}
        assert result.ready and "issue.reprints" in rows
        assert not next(
            o for o in rows["issue.reprints"].options if o.source == "MetronInfo.xml"
        ).selectable
        before = path.read_bytes()
        for action in (
            {"source": "ComicInfo.xml"},
            {"source": "MetronInfo.xml"},
            {"source": "custom", "value": "A\na"},
            {"source": "custom", "value": "x" * 4097},
        ):
            with pytest.raises(IssueMetadataEditError):
                await preview(factory, issue, {"reprints": action})
        with pytest.raises(ArchiveMetadataRenderError, match="ambiguous_list_id") as exc:
            await preview(factory, issue, {"reprints": {"source": "custom", "value": "Twin"}})
        assert "which identifier to keep" in file_metadata_error(exc.value)
        assert path.read_bytes() == before
        await edit(factory, issue, {"reprints": {"source": "custom", "value": "New reference"}})
        assert entries(path) == [("New reference", None)]
        await edit(factory, issue, {"reprints": {"source": "reset"}})
        xml = '<Reprints><Reprint id="1">Before<!--citation-->After</Reprint></Reprints>'
        await source(factory, plan, path, "", "", mi_extra=xml)
        result = await preview(factory, issue)
        row = next(r for r in result.editor_fields if r.key == "issue.reprints")
        assert result.ready and not row.editable and "annotation" in row.reason
        with pytest.raises(IssueMetadataEditError, match="read-only"):
            await preview(factory, issue, {"reprints": {"source": "clear"}})
        await edit(
            factory, issue, {"collection_title": {"source": "custom", "value": "A collection"}}
        )
        with ZipFile(path) as archive:
            assert xml.encode() in archive.read("MetronInfo.xml")
