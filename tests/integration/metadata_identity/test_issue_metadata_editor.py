"""Editor corrections, clears and override resets survive the real refresh writer."""

from datetime import date
from uuid import uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import select

from pullbox.core.metadata_identity import IdentityNamespace, MetadataSource
from pullbox.models import Issue, Series
from pullbox.schemas.issue_metadata_editor import IssueMetadataEditorReview
from pullbox.schemas.metadata_credits import MetadataCredit
from pullbox.schemas.metadata_sources import ProviderIssueRead
from pullbox.services.issue_file_metadata import prepare_file_metadata, write_file_metadata
from pullbox.services.metadata_issue_refresh import refresh_issue_from_sources
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_issue_refresh import IssueAdapter, reader
from tests.integration.metadata_identity.test_series_adoption import (
    configured_sources,  # noqa: F401
)


async def no_control():
    return None


async def no_progress(*_):
    return None


async def edit(factory, issue_id, edits):
    review = IssueMetadataEditorReview.model_validate({"edits": edits})
    async with factory() as session:
        preview = (await prepare_file_metadata(session, issue_id, editor=review)).preview
    assert preview.ready
    return await write_file_metadata(
        factory,
        issue_id,
        preview.review_key,
        uuid4(),
        limit=1000000,
        check_control=no_control,
        progress=no_progress,
        editor=review,
    )


@pytest.mark.parametrize("action", ["custom", "clear"])
async def test_editor_override_survives_refresh_and_reset_allows_provider(
    identity_probe_db, tmp_path, action
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        body = {"source": action}
        if action == "custom":
            body["value"] = "My summary"
        await edit(factory, issue_id, {"description": body})
        after_edit = path.read_bytes()
        adapter = IssueAdapter(
            ProviderIssueRead(
                source=MetadataSource.METRON_API,
                identity_namespace=IdentityNamespace.METRON,
                external_id="42",
                series_external_id="12",
                issue_number_text="50-X",
                description="Provider summary",
            )
        )
        async with factory() as session:
            await refresh_issue_from_sources(
                session, issue_id, gcd_api_enabled=False, registry=reader(adapter)
            )
            await session.commit()
        async with factory() as session:
            issue = await session.get(Issue, issue_id)
            assert issue.description == ("My summary" if action == "custom" else None)
        assert path.read_bytes() == after_edit
        await edit(factory, issue_id, {"description": {"source": "reset"}})
        async with factory() as session:
            await refresh_issue_from_sources(
                session, issue_id, gcd_api_enabled=False, registry=reader(adapter)
            )
            await session.commit()
        async with factory() as session:
            assert (await session.get(Issue, issue_id)).description == "Provider summary"


async def test_selected_dates_page_count_and_credits_preserve_series_and_pages(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        await edit(
            factory,
            issue_id,
            {
                "cover_date": {"source": "custom", "value": "2026-10-14"},
                "store_date": {"source": "custom", "value": "2026-10-07"},
                "page_count": {"source": "custom", "value": "32"},
                "credits": {
                    "source": "custom",
                    "value": "Jane Doe | writer\nJohn Smith | colorist",
                },
            },
        )
        from pullbox.services.metadata_credits import read_issue_credits

        async with factory() as session:
            issue = await session.get(Issue, issue_id)
            assert issue.release_date == date(2026, 10, 14)
            assert issue.store_date == date(2026, 10, 7)
            assert issue.page_count == 32
            assert (await session.scalar(select(Series))).title == "Example"
            assert (await read_issue_credits(session, [issue_id]))[issue_id] == (
                MetadataCredit(name="Jane Doe", role="writer"),
                MetadataCredit(name="John Smith", role="colorist"),
            )
        with ZipFile(path) as archive:
            assert archive.read("page.jpg") == b"page bytes"


async def test_unselected_disagreement_does_not_authorize_overwrite(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(
        factory,
        tmp_path,
        comicinfo="<ComicInfo><Number>50-X</Number><Summary>File summary</Summary></ComicInfo>",
    ) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        async with factory.begin() as session:
            (await session.get(Issue, issue_id)).description = "Library summary"
        before = path.read_bytes()
        review = IssueMetadataEditorReview.model_validate(
            {"edits": {"title": {"source": "custom", "value": "New title"}}}
        )
        async with factory() as session:
            preview = (await prepare_file_metadata(session, issue_id, editor=review)).preview
        assert not preview.ready
        assert any(c.key == "issue.description" for c in preview.conflicts)
        assert path.read_bytes() == before


async def test_explicit_embedded_choice_updates_only_selected_value(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(
        factory,
        tmp_path,
        comicinfo="<ComicInfo><Number>50-X</Number><Summary>File summary</Summary></ComicInfo>",
    ) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        async with factory.begin() as session:
            (await session.get(Issue, issue_id)).description = "Library summary"
        await edit(factory, issue_id, {"description": {"source": "ComicInfo.xml"}})
        async with factory() as session:
            assert (await session.get(Issue, issue_id)).description == "File summary"
        with ZipFile(path) as archive:
            assert archive.read("page.jpg") == b"page bytes"
            assert b"File summary" in archive.read("MetronInfo.xml")


@pytest.mark.parametrize("change", ["source", "metadata", "cancel"])
async def test_editor_rechecks_approval_and_cancellation(identity_probe_db, tmp_path, change):
    from pullbox.core.exceptions import JobCancelledError

    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        review = IssueMetadataEditorReview.model_validate(
            {"edits": {"title": {"source": "custom", "value": "Correction"}}}
        )
        async with factory() as session:
            preview = (await prepare_file_metadata(session, issue_id, editor=review)).preview
        if change == "metadata":
            async with factory.begin() as session:
                (await session.get(Issue, issue_id)).title = "Concurrent edit"
        elif change == "source":
            path.touch()
        before = path.read_bytes(), path.stat()

        async def control():
            if change == "cancel":
                raise JobCancelledError("Stop")

        with pytest.raises((ValueError, JobCancelledError)):
            await write_file_metadata(
                factory,
                issue_id,
                preview.review_key,
                uuid4(),
                limit=1000000,
                check_control=control,
                progress=no_progress,
                editor=review,
            )
        assert path.read_bytes() == before[0]
        assert path.stat().st_mtime_ns == before[1].st_mtime_ns
        async with factory() as session:
            assert (await session.get(Issue, issue_id)).title != "Correction"
