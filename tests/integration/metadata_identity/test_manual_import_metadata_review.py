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


async def gargoyle_case(factory, tmp_path):
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
            "<ComicInfo><Series>Canonical series</Series><Issue>50-X</Issue>"
            "<writer>Local Writer</writer><PageCount>54</PageCount>"
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


@pytest.mark.parametrize(
    "xml",
    [
        "<Issue>99</Issue>",
        "<Issue>50-X</Issue><Number>99</Number>",
        "<Issue>50-X</Issue><Web>https://comicvine.gamespot.com/issue/4000-999999/</Web>",
    ],
)
async def test_manual_review_cannot_override_issue_identity(identity_probe_db, tmp_path, xml):
    _, factory, _ = identity_probe_db
    issue_id, source = await gargoyle_case(factory, tmp_path)
    with ZipFile(source, "w") as archive:
        archive.writestr("page.jpg", b"page")
        archive.writestr("ComicInfo.xml", f"<ComicInfo>{xml}</ComicInfo>")
    before = source.read_bytes()
    async with factory() as session:
        plan = await read_manual_metadata_plan(session, issue_id)
    with pytest.raises(ValueError):
        await preview_manual_import_metadata(plan, source, {})
    assert source.read_bytes() == before
