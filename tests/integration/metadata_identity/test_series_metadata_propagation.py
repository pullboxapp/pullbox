"""Propagation journals and sibling previews are stable on both supported databases."""

from uuid import uuid4

import pytest

from pullbox.models import Issue, Series
from pullbox.schemas.series_metadata_editor import SeriesMetadataEditorReview
from pullbox.schemas.series_metadata_propagation import PropagationReview
from pullbox.services.issue_file_metadata import prepare_file_metadata, write_file_metadata
from pullbox.services.series_metadata_editor import prepare_series_editor, save_series_editor
from pullbox.services.series_metadata_propagation import KEY, job_details, latest_job, preview_plan
from pullbox.utilities.executors.file_metadata import FileMetadataExecutor
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.models import JobType
from tests.api.test_series_metadata_propagation import seed_pair, seed_protected_titles
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared


async def test_blocked_reason_survives_reopening_preview_on_both_databases(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, staged, plan):
        await seed_protected_titles(factory, path, staged, plan)
        series_id = plan.target.binding.metadata.series.local_id
        manager = JobQueueManager(factory)
        manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
        async with factory.begin() as session:
            batch = await preview_plan(
                session, series_id, PropagationReview(fields={"publication_titles"})
            )
            await manager.create_job(
                session, JobType.FILE_METADATA, "Preview", {KEY: batch.model_dump(mode="json")}
            )
        await manager.dispatch_next()
        async with factory() as session:
            job = await latest_job(session, series_id)
            report = await job_details(session, job, 1)
            assert report["ready_ids"] == [] and report["counts"] == {"attention": 1}
            assert report["targets"][0]["protected_titles"] == {
                "entries": [
                    {"name": "Le Port", "language": "fr"},
                    {"name": "Port & Sea", "language": "en"},
                ],
                "not_shown": 0,
            }
            assert (await job_details(session, job, 2))["targets"] == []


@pytest.mark.parametrize("field", ["title", "publication_titles"])
async def test_projection_finalizes_without_advancing_canonical_state(
    identity_probe_db, tmp_path, field
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, staged, plan):
        await seed_pair(factory, path, staged, plan)
        series_id = plan.target.binding.metadata.series.local_id
        issue_id = plan.target.binding.metadata.issues[0].local_id
        if field == "publication_titles":
            selection = SeriesMetadataEditorReview.model_validate(
                {
                    "mode": "library_only",
                    "edits": {
                        field: {"action": "set", "value": [{"name": "Le Port", "language": "fr"}]}
                    },
                }
            )
            async with factory() as session:
                key = (
                    await prepare_series_editor(session, series_id, selection)
                ).preview.review_key
            async with factory() as session:
                await save_series_editor(session, series_id, selection, key)
                await session.commit()
        fields = frozenset({field})
        async with factory() as session:
            review = await prepare_file_metadata(session, issue_id, propagation_fields=fields)
            batch = await preview_plan(session, series_id, PropagationReview(fields=fields))
        async with factory() as session:
            series_before = (await session.get(Series, series_id)).updated_at
            issue_before = (await session.get(Issue, issue_id)).updated_at

        async def check():
            pass

        async def progress(*args):
            pass

        operation = uuid4()
        assert (
            await write_file_metadata(
                factory,
                issue_id,
                review.preview.review_key,
                operation,
                limit=1000000,
                check_control=check,
                progress=progress,
                propagation_fields=fields,
            )
            == "written"
        )
        signature = path.read_bytes(), path.stat()
        assert (
            await write_file_metadata(
                factory,
                issue_id,
                review.preview.review_key,
                operation,
                limit=1000000,
                check_control=check,
                progress=progress,
                propagation_fields=fields,
            )
            == "recovered"
        )
        assert (path.read_bytes(), path.stat()) == signature
        async with factory() as session:
            assert (await session.get(Series, series_id)).updated_at == series_before
            assert (await session.get(Issue, issue_id)).updated_at == issue_before
        manager = JobQueueManager(factory)
        manager.register_executor(JobType.FILE_METADATA, FileMetadataExecutor)
        async with factory.begin() as session:
            job = await manager.create_job(
                session, JobType.FILE_METADATA, "Preview", {KEY: batch.model_dump(mode="json")}
            )
        async with factory() as session:
            assert (await latest_job(session, series_id)).id == job.id
        await manager.dispatch_next()
        async with factory() as session:
            job = await latest_job(session, series_id)
            result = await job_details(session, job, 1)
            assert result["counts"] == {"current": 1}, result
            assert len(result["targets"]) == 1
