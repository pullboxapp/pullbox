"""Review text and scanner credits belong to ComicInfo, not Summary or MetronInfo."""

from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from pullbox.schemas.issue_metadata_editor import IssueMetadataEditorReview
from pullbox.services.archive_metadata_preservation import ArchiveMetadataRenderError
from pullbox.services.archive_metadata_publication import ArchivePublicationError
from pullbox.services.issue_file_metadata import file_metadata_error, write_file_metadata
from pullbox.services.issue_metadata_editor import IssueMetadataEditError
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_issue_metadata_editor import (
    edit,
    no_control,
    no_progress,
)
from tests.integration.metadata_identity.test_issue_tag_editor import preview, source


def documents(path):
    with ZipFile(path) as archive:
        assert archive.read("page.jpg") == b"page bytes"
        return tuple(ET.fromstring(archive.read(n)) for n in ("ComicInfo.xml", "MetronInfo.xml"))


async def test_review_and_scan_information_are_independent_single_document_edits(
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
            ci_extra=(
                "<Review>A reader's review</Review><ScanInformation>Scanned by A</ScanInformation>"
                "<Notes>Separate note [CVDB0014]</Notes>"
            ),
            mi_extra="<Notes>Metron note</Notes>",
        )
        before = path.read_bytes(), path.stat().st_mtime_ns
        report = await preview(factory, issue)
        rows = {r.key: r for r in report.editor_fields}
        assert {"issue.review", "issue.scan_information"} <= rows.keys()
        for key in ("issue.review", "issue.scan_information"):
            row = rows[key]
            assert row.editable and row.input_type == "textarea" and row.differs
            assert {o.source for o in row.options} == {"library", "ComicInfo.xml"}
            assert "ComicInfo.xml only" in row.reason
        actions = {
            "review": {"source": "ComicInfo.xml"},
            "scan_information": {
                "source": "custom",
                "value": "  From Amazon\r\nScan <B> & Caf\u00e9 ",
            },
        }
        assert (await preview(factory, issue, actions)).ready
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        await edit(factory, issue, actions)
        ci, mi = documents(path)
        assert ci.findtext("Review") == "A reader's review"
        assert ci.findtext("ScanInformation") == "From Amazon\nScan <B> & Caf\u00e9"
        assert (
            ci.findtext("Notes") == "Separate note [CVDB0014]"
            and mi.findtext("Notes") == "Metron note"
        )
        assert mi.find("Review") is mi.find("ScanInformation") is None
        saved = await preview(factory, issue)
        assert saved.ready and all(
            f.overridden and not f.differs
            for f in saved.editor_fields
            if f.key in {"issue.review", "issue.scan_information"}
        )
        await edit(factory, issue, {"review": {"source": "clear"}})
        assert documents(path)[0].find("Review") is None
        signature = path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino
        assert (
            await edit(factory, issue, {"scan_information": {"source": "reset"}})
            == "override_released"
        )
        assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) == signature


@pytest.mark.parametrize(
    "field,tag", [("review", "Review"), ("scan_information", "ScanInformation")]
)
async def test_external_removal_of_single_document_value_requires_review(
    identity_probe_db, tmp_path, field, tag
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await edit(factory, issue, {field: {"source": "custom", "value": "Approved"}})
        await source(factory, plan, path, "", "")
        changed = path.read_bytes()
        report = await preview(factory, issue)
        assert not report.ready
        conflict = next(c for c in report.conflicts if c.key == f"xml.{tag}")
        assert "ComicInfo.xml" in conflict.note and "both documents" not in conflict.note
        with pytest.raises(IssueMetadataEditError, match=r"ComicInfo\.xml only"):
            await preview(factory, issue, {field: {"source": "MetronInfo.xml"}})
        assert path.read_bytes() == changed
        choice = {field: {"source": "library"}}
        approved = await preview(factory, issue, choice)
        await source(factory, plan, path, "", "", ci_extra=f"<{tag}>New external text</{tag}>")
        changed = path.read_bytes()
        with pytest.raises(ArchivePublicationError, match="approval_changed"):
            await write_file_metadata(
                factory,
                issue,
                approved.review_key,
                uuid4(),
                limit=10**6,
                check_control=no_control,
                progress=no_progress,
                editor=IssueMetadataEditorReview.model_validate({"edits": choice}),
            )
        assert path.read_bytes() == changed
        await edit(factory, issue, choice)
        assert (
            documents(path)[0].findtext(tag) == "Approved" and (await preview(factory, issue)).ready
        )


async def test_reference_text_is_protected_without_blocking_unrelated_edits(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        marker = "Catalog: https://comicvine.gamespot.com/volume/4050-123/"
        await source(
            factory, plan, path, "", "", ci_extra=f"<ScanInformation>{marker}</ScanInformation>"
        )
        report = await preview(factory, issue)
        field = next(f for f in report.editor_fields if f.key == "issue.scan_information")
        assert report.ready and not field.editable and "identifier" in field.reason
        with pytest.raises(IssueMetadataEditError, match="identifier"):
            await preview(factory, issue, {"scan_information": {"source": "clear"}})
        with pytest.raises(IssueMetadataEditError, match="identifier"):
            await preview(
                factory, issue, {"review": {"source": "custom", "value": "new cv_issue_id: 67"}}
            )
        await edit(factory, issue, {"review": {"source": "custom", "value": "Not a summary"}})
        ci, _ = documents(path)
        assert ci.findtext("Review") == "Not a summary" and ci.findtext("ScanInformation") == marker


@pytest.mark.parametrize("separator", ["", "<!-- ignored cv_issue_id: 123 -->"])
async def test_review_keeps_identifiers_spanning_neighboring_fields(
    identity_probe_db, tmp_path, separator
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
            ci_extra=(
                f"<ScanInformation>cv_issue_id:</ScanInformation>{separator}"
                "<Review>00123 first impression</Review>"
            ),
        )
        before = path.read_bytes()
        with pytest.raises(
            ArchiveMetadataRenderError, match="comicinfo_text_identity_change"
        ) as caught:
            await preview(
                factory, issue, {"review": {"source": "custom", "value": "123 new review"}}
            )
        assert "neighboring ComicInfo.xml fields" in file_metadata_error(caught.value)
        assert path.read_bytes() == before
        await edit(
            factory, issue, {"review": {"source": "custom", "value": "00123 updated review"}}
        )
        assert documents(path)[0].findtext("Review") == "00123 updated review"


async def test_oversized_legacy_review_is_preserved_when_editing_other_fields(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        oversized = "x" * 20001
        await source(factory, plan, path, "", "", ci_extra=f"<Review>{oversized}</Review>")
        for text, reason in ((oversized, "20,000"), ("bad\x01text", "control characters")):
            with pytest.raises(IssueMetadataEditError, match=reason):
                await preview(
                    factory, issue, {"scan_information": {"source": "custom", "value": text}}
                )
        row = next(
            f for f in (await preview(factory, issue)).editor_fields if f.key == "issue.review"
        )
        assert not row.editable and "20,000" in row.reason
        await edit(
            factory, issue, {"scan_information": {"source": "custom", "value": "Local scan"}}
        )
        assert documents(path)[0].findtext("Review") == oversized
