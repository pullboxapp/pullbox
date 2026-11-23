"""Manual import reviews descriptive differences without changing its source."""

from datetime import date
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from sqlalchemy import delete, select

from pullbox.models import Issue, LibraryFile
from pullbox.models.creator import Creator, IssueCreator
from pullbox.models.download import DownloadHistory
from pullbox.schemas.issue_file_metadata import FileMetadataApproval
from pullbox.services.issue_import_service import (
    ManualIssueImportError,
    execute_manual_issue_import,
    prepare_manual_issue_import,
)
from pullbox.services.manual_import_metadata_review import preview_manual_import_metadata
from pullbox.services.manual_paired_metadata import read_manual_metadata_plan
from tests.integration.metadata_identity.test_download_paired_metadata import completed_download
from tests.integration.metadata_identity.test_manual_paired_metadata import (
    paired_manual_writer,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("paired_manual_writer")


async def gargoyle_case(factory, tmp_path, *, embedded_title="Canonical series"):
    download_id, issue_id, source = await completed_download(factory, tmp_path)
    async with factory.begin() as session:
        await session.execute(delete(DownloadHistory).where(DownloadHistory.id == download_id))
        issue = await session.get(Issue, issue_id)
        issue.page_count = 48
        issue.release_date = date(2025, 2, 1)
        issue.store_date = date(2024, 12, 26)
        creator = Creator(name="Catalog Writer")
        session.add(creator)
        await session.flush()
        session.add(IssueCreator(issue_id=issue_id, creator_id=creator.id, role="writer"))
    with ZipFile(source, "w") as archive:
        for page in range(54):
            archive.writestr(f"page-{page:03}.jpg", f"page {page}".encode())
        archive.writestr(
            "ComicInfo.xml",
            f"<ComicInfo><Series>{embedded_title}</Series><Issue>50-X</Issue>"
            "<writer>Local Writer</writer><PageCount>54</PageCount>"
            "<Notes>Original note [CVDB123]</Notes>"
            "<Year>2024</Year><Month>12</Month></ComicInfo>",
        )
    return issue_id, source


async def test_manual_review_requires_choices_then_imports_paired_copy(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    issue_id, source = await gargoyle_case(factory, tmp_path)
    original = source.read_bytes()
    async with factory() as session:
        plan = await read_manual_metadata_plan(session, issue_id)
    review = await preview_manual_import_metadata(plan, source, {})
    assert not review.preview.ready
    conflicts = {item.key: item for item in review.preview.conflicts}
    assert {"issue.page_count", "issue.cover_date", "issue.credits"} <= conflicts.keys()
    assert [(o.value, o.selectable) for o in conflicts["issue.cover_date"].options] == [
        ("2025-02-01", True),
        ("2024-12-?", False),
    ]
    assert "archive" in conflicts["issue.page_count"].note.lower()
    choices = {item.key: "library" for item in review.preview.conflicts}
    choices["issue.page_count"] = "ComicInfo.xml"
    choices["issue.credits"] = "ComicInfo.xml"
    approved = await preview_manual_import_metadata(plan, source, choices)
    assert approved.preview.ready
    assert source.read_bytes() == original
    async with factory() as session:
        prepared = await prepare_manual_issue_import(
            session, issue_id=issue_id, file_path=str(source), move_to_library=True
        )
        result = await execute_manual_issue_import(
            session,
            prepared,
            metadata_approval=FileMetadataApproval(
                review_key=approved.preview.review_key, choices=choices
            ),
        )
        await session.commit()
        with ZipFile(result.library_file.file_path) as archive:
            ci = ET.fromstring(archive.read("ComicInfo.xml"))
            mi = ET.fromstring(archive.read("MetronInfo.xml"))
            assert ci.findtext("PageCount") == mi.findtext("PageCount") == "54"
            assert ci.findtext("Year") == "2025" and ci.findtext("Month") == "2"
            assert mi.findtext("CoverDate") == "2025-02-01"
            assert mi.findtext("StoreDate") == "2024-12-26"
            assert ci.findtext("Writer") == mi.findtext("Credits/Credit/Creator") == "Local Writer"
            assert ci.find("Issue") is None and ci.find("writer") is None
            assert archive.read("page-053.jpg") == b"page 53"
        assert (await session.get(Issue, issue_id)).page_count == 48
    assert source.read_bytes() == original


@pytest.mark.parametrize("change", ["file", "metadata", "choices", "unresolved"])
async def test_manual_review_rejects_stale_or_incomplete_approval(
    identity_probe_db, tmp_path, change
):
    _, factory, _ = identity_probe_db
    issue_id, source = await gargoyle_case(factory, tmp_path)
    async with factory() as session:
        plan = await read_manual_metadata_plan(session, issue_id)
    preview = await preview_manual_import_metadata(plan, source, {})
    choices = {item.key: "library" for item in preview.preview.conflicts}
    review = await preview_manual_import_metadata(plan, source, choices)
    if change == "file":
        with ZipFile(source, "a") as archive:
            archive.writestr("new-page.jpg", b"new")
    elif change == "metadata":
        async with factory.begin() as session:
            (await session.get(Issue, issue_id)).page_count = 49
    elif change == "choices":
        choices["issue.page_count"] = "ComicInfo.xml"
    else:
        review, choices = preview, {}
    before = source.read_bytes()
    async with factory() as session:
        prepared = await prepare_manual_issue_import(
            session, issue_id=issue_id, file_path=str(source), move_to_library=True
        )
        with pytest.raises(ManualIssueImportError, match=r"review|preview"):
            await execute_manual_issue_import(
                session,
                prepared,
                metadata_approval=FileMetadataApproval(
                    review_key=review.preview.review_key, choices=choices
                ),
            )
        await session.rollback()
    assert source.read_bytes() == before

    async with factory() as session:
        assert await session.scalar(select(LibraryFile.id)) is None
    assert not list((tmp_path / "comics").rglob("*.cbz"))


@pytest.mark.parametrize("case", ["confirm", "no-confirm", "changed-target", "source-is-target"])
async def test_editor_replacement_needs_separate_current_file_confirmation(
    identity_probe_db, tmp_path, case
):
    from pathlib import Path

    from pullbox.api.v1.issues import preview_import_file_metadata
    from pullbox.schemas.issue import ManualFileImportPreviewRequest, ManualMetadataApproval

    _, factory, _ = identity_probe_db
    issue_id, source = await gargoyle_case(factory, tmp_path)
    original = source.read_bytes()
    async with factory() as session:
        plan = await read_manual_metadata_plan(session, issue_id)
        choices = {
            row.key: "library"
            for row in (await preview_manual_import_metadata(plan, source, {})).preview.conflicts
        }
        reviewed = (await preview_manual_import_metadata(plan, source, choices)).preview
        args = dict(issue_id=issue_id, file_path=str(source), move_to_library=True)
        initial = await execute_manual_issue_import(
            session,
            await prepare_manual_issue_import(session, **args),
            metadata_approval=FileMetadataApproval(review_key=reviewed.review_key, choices=choices),
        )
        file_id = initial.library_file.id
        target = Path(initial.library_file.file_path)
        await session.commit()
    body = ManualFileImportPreviewRequest.model_validate(
        {
            "file_path": str(source),
            "editor": {
                "edits": {
                    "title": {"source": "custom", "value": "Reviewed replacement"},
                    "credits": {"source": "library"},
                    "cover_date": {"source": "library"},
                    "page_count": {"source": "library"},
                }
            },
        }
    )
    async with factory() as session:
        if case == "source-is-target":
            from fastapi import HTTPException

            before = target.read_bytes()
            with pytest.raises(HTTPException, match="separate source") as rejected:
                await preview_import_file_metadata(
                    issue_id, body.model_copy(update={"file_path": str(target)}), None, session
                )
            assert rejected.value.status_code == 409 and target.read_bytes() == before
            return
        preview = await preview_import_file_metadata(issue_id, body, None, session)
        assert preview.ready and preview.existing_file_id == file_id
    if case == "changed-target":
        import os

        stat = target.stat()
        with target.open("r+b") as file:
            file.write(b"external edit")
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    target_before = target.read_bytes()
    approval = ManualMetadataApproval(
        review_key=preview.review_key,
        editor=body.editor,
        replace_file_id=None if case == "no-confirm" else file_id,
    )
    async with factory() as session:
        prepared = await prepare_manual_issue_import(session, **args)
        if case != "confirm":
            with pytest.raises(ManualIssueImportError, match=r"review|confirm"):
                await execute_manual_issue_import(session, prepared, metadata_approval=approval)
            await session.rollback()
            assert target.read_bytes() == target_before
            assert (await session.get(Issue, issue_id)).title != "Reviewed replacement"
        else:
            result = await execute_manual_issue_import(
                session, prepared, metadata_approval=approval
            )
            await session.commit()
            assert (await session.get(Issue, issue_id)).title == "Reviewed replacement"
            with ZipFile(result.library_file.file_path) as archive:
                assert (
                    ET.fromstring(archive.read("ComicInfo.xml")).findtext("Title")
                    == "Reviewed replacement"
                )
    assert source.read_bytes() == original


@pytest.mark.parametrize("changed", [False, True])
async def test_manual_editor_previews_and_applies_only_approved_edits(
    identity_probe_db, tmp_path, changed
):
    from pullbox.api.v1.issues import preview_import_file_metadata
    from pullbox.schemas.issue import ManualFileImportPreviewRequest, ManualFileImportRequest

    _, factory, _ = identity_probe_db
    issue_id, source = await gargoyle_case(factory, tmp_path, embedded_title="File edition")
    original = source.read_bytes()
    review = {
        "choices": {"series.title": "ComicInfo.xml"},
        "file_path": str(source),
        "editor": {
            "edits": {
                "page_count": {"source": "custom", "value": "52"},
                "credits": {"source": "library"},
                "cover_date": {"source": "library"},
                "tags": {"source": "custom", "value": "Noir"},
                "notes": {"source": "custom", "value": "Reviewed scan"},
                "review": {"source": "custom", "value": "Reader review"},
            }
        },
    }
    async with factory() as session:
        from fastapi import HTTPException

        with pytest.raises(HTTPException, match="series editor"):
            await preview_import_file_metadata(
                issue_id, ManualFileImportPreviewRequest.model_validate(review), None, session
            )
        review["choices"]["series.title"] = "library"
        preview = await preview_import_file_metadata(
            issue_id, ManualFileImportPreviewRequest.model_validate(review), None, session
        )
        conflict = next(c for c in preview.conflicts if c.key == "series.title")
        assert [option.source for option in conflict.options if option.selectable] == ["library"]
        assert preview.ready and preview.editor_available and preview.editor_fields
        assert any(change.after == "52" for change in preview.changes)
        assert (await session.get(Issue, issue_id)).page_count == 48
        assert await session.scalar(select(LibraryFile)) is None
    if changed:
        review["editor"]["edits"]["page_count"]["value"] = "51"
    request = ManualFileImportRequest.model_validate(
        {
            "file_path": str(source),
            "metadata_approval": {
                "review_key": preview.review_key,
                "choices": review["choices"],
                "editor": review["editor"],
            },
        }
    )
    async with factory() as session:
        prepared = await prepare_manual_issue_import(
            session, issue_id=issue_id, file_path=str(source), move_to_library=True
        )
        if changed:
            with pytest.raises(ManualIssueImportError, match="review"):
                await execute_manual_issue_import(
                    session, prepared, metadata_approval=request.metadata_approval
                )
            await session.rollback()
            assert await session.scalar(select(LibraryFile)) is None
            assert (await session.get(Issue, issue_id)).page_count == 48
        else:
            result = await execute_manual_issue_import(
                session, prepared, metadata_approval=request.metadata_approval
            )
            await session.commit()
            from pullbox.models import Series

            assert await session.scalar(select(Series.title)) == "Canonical series"
            assert (await session.get(Issue, issue_id)).page_count == 52
            with ZipFile(result.library_file.file_path) as archive:
                ci = ET.fromstring(archive.read("ComicInfo.xml"))
                mi = ET.fromstring(archive.read("MetronInfo.xml"))
                assert ci.findtext("PageCount") == mi.findtext("PageCount") == "52"
                assert ci.findtext("Series") == "Canonical series"
                assert ci.findtext("Tags") == mi.findtext("Tags/Tag") == "Noir"
                assert ci.findtext("Notes").startswith("Reviewed scan [CVDB123]")
                assert mi.findtext("Notes") == "Reviewed scan"
                assert ci.findtext("Review") == "Reader review" and mi.find("Review") is None
                assert archive.read("page-053.jpg") == b"page 53"
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    "xml",
    [
        "<Issue>99</Issue>",
        "<Issue>50-X</Issue><Number>99</Number>",
        "<Issue>50-X</Issue><Web>https://comicvine.gamespot.com/issue/4000-999999/</Web>",
    ],
)
@pytest.mark.parametrize("edit", [False, True], ids=["classic", "editor"])
async def test_manual_review_cannot_override_issue_identity(identity_probe_db, tmp_path, xml, edit):
    from pullbox.schemas.issue_metadata_editor import IssueMetadataEditorReview

    _, factory, _ = identity_probe_db
    issue_id, source = await gargoyle_case(factory, tmp_path)
    with ZipFile(source, "w") as archive:
        archive.writestr("page.jpg", b"page")
        archive.writestr("ComicInfo.xml", f"<ComicInfo>{xml}</ComicInfo>")
    before = source.read_bytes()
    async with factory() as session:
        plan = await read_manual_metadata_plan(session, issue_id)
    with pytest.raises(ValueError):
        await preview_manual_import_metadata(
            plan, source, {}, editor=IssueMetadataEditorReview() if edit else None
        )
    assert source.read_bytes() == before
