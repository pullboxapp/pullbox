"""Propagation journals and sibling previews are stable on both supported databases."""

from uuid import uuid4

from pullbox.models import Issue, Series
from pullbox.schemas.series_metadata_propagation import PropagationReview
from pullbox.services.issue_file_metadata import prepare_file_metadata, write_file_metadata
from pullbox.services.series_metadata_propagation import KEY, job_details, latest_job, preview_plan
from pullbox.utilities.executors.file_metadata import FileMetadataExecutor
from pullbox.utilities.job_queue import JobQueueManager
from pullbox.utilities.models import JobType
from tests.api.test_series_metadata_propagation import seed_pair
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared


async def test_projection_finalizes_without_advancing_canonical_state(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, staged, plan):
        await seed_pair(factory, path, staged, plan)
        series_id = plan.target.binding.metadata.series.local_id
        issue_id = plan.target.binding.metadata.issues[0].local_id
        fields = frozenset({"title"})
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
