"""Editor corrections, clears and override resets survive the real refresh writer."""

import os
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
from pullbox.utilities.executors.file_metadata import FileMetadataExecutor
from pullbox.utilities.models import JobState, JobType, UtilityJob, UtilityJobItem
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
        before_reset = path.stat()
        outcome = await edit(factory, issue_id, {"description": {"source": "reset"}})
        assert outcome == "override_released"
        assert (path.stat().st_ino, path.stat().st_mtime_ns) == (
            before_reset.st_ino,
            before_reset.st_mtime_ns,
        )
        assert path.read_bytes() == after_edit
        async with factory() as session:
            await refresh_issue_from_sources(
                session, issue_id, gcd_api_enabled=False, registry=reader(adapter)
            )
            await session.commit()
        async with factory() as session:
            assert (await session.get(Issue, issue_id)).description == "Provider summary"
        assert path.read_bytes() == after_edit


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


async def test_reset_job_replay_does_not_release_a_later_override(identity_probe_db, tmp_path):
    from pullbox.api.v1.issue_metadata_links import file_job_status

    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        await edit(factory, issue_id, {"description": {"source": "custom", "value": "Original"}})
        review = IssueMetadataEditorReview.model_validate(
            {"edits": {"description": {"source": "reset"}}}
        )
        async with factory() as session:
            preview = (await prepare_file_metadata(session, issue_id, editor=review)).preview
        assert preview.unchanged and preview.editor_changed
        operation, job_id = uuid4(), str(uuid4())
        config = {
            "issue_id": issue_id,
            "review_key": preview.review_key,
            "editor": review.model_dump(),
        }
        async with factory.begin() as session:
            job = UtilityJob(
                id=job_id,
                job_type=JobType.FILE_METADATA,
                display_name="Edit",
                state=JobState.RUNNING,
            )
            session.add(job)
            await session.flush()
            session.add(
                UtilityJobItem(
                    id=operation.hex, job_id=job_id, operation="file_metadata", item_index=0
                )
            )
        executor = FileMetadataExecutor()
        args = ({"id": operation.hex}, config, {"factory": factory, "limit": 1000000})
        first = await executor.process_item_async(*args)
        assert first.after_state == {"outcome": "override_released"}, first.error_message
        async with factory() as session:
            status = await file_job_status(session, await session.get(UtilityJob, job_id))
            assert status["job"]["override_released"]
        await edit(factory, issue_id, {"description": {"source": "custom", "value": "Newer"}})
        before = path.read_bytes(), path.stat().st_ino
        replay = await executor.process_item_async(*args)
        assert replay.after_state == first.after_state
        async with factory() as session:
            fields = (
                await prepare_file_metadata(session, issue_id, editor=IssueMetadataEditorReview())
            ).preview.editor_fields
            summary = next(f for f in fields if f.key == "issue.description")
            assert summary.overridden and summary.current == "Newer"
        assert (path.read_bytes(), path.stat().st_ino) == before


async def test_reset_with_a_pending_xml_change_still_requires_publication(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        await edit(factory, issue_id, {"title": {"source": "custom", "value": "My title"}})
        async with factory.begin() as session:
            (await session.get(Issue, issue_id)).store_date = date(2024, 11, 20)
        before = path.stat()
        assert await edit(factory, issue_id, {"title": {"source": "reset"}}) == "written"
        assert path.stat().st_ino != before.st_ino
        with ZipFile(path) as archive:
            assert b"2024-11-20" in archive.read("MetronInfo.xml")
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


@pytest.mark.parametrize("change", ["file", "cancel"])
async def test_database_only_reset_rechecks_before_commit(identity_probe_db, tmp_path, change):
    from pullbox.core.exceptions import JobCancelledError
    from pullbox.core.metadata_identity import MetadataEntityKind
    from pullbox.services.archive_metadata_binding import ArchiveMetadataBindingError
    from pullbox.services.metadata_baselines import load_metadata_baseline

    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        await edit(factory, issue_id, {"title": {"source": "custom", "value": "Keep"}})
        review = IssueMetadataEditorReview.model_validate({"edits": {"title": {"source": "reset"}}})
        async with factory() as session:
            preview = (await prepare_file_metadata(session, issue_id, editor=review)).preview
        assert preview.unchanged
        before = path.read_bytes()
        checks = 0

        async def control():
            nonlocal checks
            checks += 1
            if checks == 2:
                if change == "cancel":
                    raise JobCancelledError("Cancelled before saving")
                st = path.stat()
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

        with pytest.raises(
            JobCancelledError if change == "cancel" else ArchiveMetadataBindingError
        ):
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
        assert path.read_bytes() == before
        async with factory() as session:
            saved = await load_metadata_baseline(session, MetadataEntityKind.ISSUE, issue_id)
            assert next(o for o in saved.snapshot.origins if o.field == "title").user_override


async def test_reset_is_not_a_choice_between_conflicting_summaries(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue_id = plan.target.binding.metadata.issues[0].local_id
        await edit(factory, issue_id, {"description": {"source": "custom", "value": "In the file"}})
        async with factory.begin() as session:
            (await session.get(Issue, issue_id)).description = "In the library"
        before = path.read_bytes()
        review = IssueMetadataEditorReview.model_validate(
            {"edits": {"description": {"source": "reset"}}}
        )
        async with factory() as session:
            preview = (await prepare_file_metadata(session, issue_id, editor=review)).preview
        assert not preview.ready
        assert any(c.key == "issue.description" and not c.selected for c in preview.conflicts)
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
