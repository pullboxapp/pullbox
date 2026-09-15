"""Individual decisions must not reassign or discard sibling comics."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.core.exceptions import ValidationError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.providers.base import IssueSummary
from pullbox.services.import_review_file_assignment import assign_review_file


async def setup_conflict(session):
    job = ImportJob(
        source_path="/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    session.add(job)
    await session.flush()
    parent = ImportedSeries(
        import_job_id=job.id, raw_series_name="Mixed", cv_id=10, status=ImportSeriesStatus.MATCHED
    )
    session.add(parent)
    await session.flush()
    files = [
        ImportedFile(
            import_job_id=job.id,
            import_series_id=parent.id,
            file_path=f"/comics/{name}.cbz",
            file_name=f"{name}.cbz",
            file_size=100,
            file_format="cbz",
            status=ImportedFileStatus.CONFLICT,
            matched_issue_cv_id=101,
            conflict_group_id=1,
            source_signature={"size": 100},
        )
        for name in ("First", "Second")
    ]
    session.add_all(files)
    await session.flush()
    metadata = AsyncMock()
    metadata.get_series_metadata.return_value = SimpleNamespace(
        provider_id="20",
        title="Second",
        year_start=2020,
        publisher="Test",
        issue_count=1,
        comicvine_url=None,
    )
    metadata.get_issue_summaries_for_series.return_value = [
        IssueSummary("201", 1, "Second", "2020-01-01", None, "issue")
    ]
    return job, parent, files, metadata


async def test_reassignment_moves_only_staged_identity_and_preserves_evidence(
    db_session: AsyncSession,
):
    job, parent, files, metadata = await setup_conflict(db_session)
    chosen, sibling = files
    await assign_review_file(
        db_session, job.id, chosen.id, cv_id=20, issue_cv_id=201, metadata_service=metadata
    )
    assert chosen.import_series_id != parent.id
    assert chosen.matched_issue_cv_id == 201
    assert chosen.file_path == "/comics/First.cbz"
    assert chosen.source_signature == {"size": 100}
    assert chosen.diagnostics["target_issue_summary"]["provider_id"] == "201"
    assert sibling.import_series_id == parent.id
    assert sibling.matched_issue_cv_id == 101
    assert sibling.status is ImportedFileStatus.CONFLICT


async def test_reassignment_rejects_issue_outside_chosen_series(db_session: AsyncSession):
    job, parent, files, metadata = await setup_conflict(db_session)
    with pytest.raises(ValidationError, match="series"):
        await assign_review_file(
            db_session, job.id, files[0].id, cv_id=20, issue_cv_id=999, metadata_service=metadata
        )
    assert files[0].import_series_id == parent.id


async def test_reassignment_does_not_clear_a_safety_block(db_session: AsyncSession):
    job, parent, files, metadata = await setup_conflict(db_session)
    files[0].status = ImportedFileStatus.SAFETY_BLOCKED
    with pytest.raises(ValidationError, match="inspection"):
        await assign_review_file(
            db_session, job.id, files[0].id, cv_id=20, issue_cv_id=201, metadata_service=metadata
        )
    assert files[0].import_series_id == parent.id


async def test_two_files_assigned_to_one_issue_require_a_copy_decision(db_session):
    job, _, files, metadata = await setup_conflict(db_session)
    for file in files:
        await assign_review_file(
            db_session, job.id, file.id, cv_id=20, issue_cv_id=201, metadata_service=metadata
        )
    assert files[0].import_series_id == files[1].import_series_id
    assert files[0].conflict_group_id == files[1].conflict_group_id
    assert files[0].conflict_group_id is not None
    assert all(file.status is ImportedFileStatus.CONFLICT for file in files)
    assert not any(file.include_in_import for file in files)


async def test_assignment_cannot_contradict_exact_source_identity(db_session):
    job, parent, files, metadata = await setup_conflict(db_session)
    files[0].comicvine_issue_id = 101
    files[0].diagnostics = {"metadata_signals": {"comicvine_issue_id": "comicinfo"}}
    with pytest.raises(ValidationError, match="embedded"):
        await assign_review_file(
            db_session, job.id, files[0].id, cv_id=20, issue_cv_id=201, metadata_service=metadata
        )
    metadata.get_series_metadata.assert_not_called()
    assert files[0].import_series_id == parent.id


async def test_assignment_revalidates_review_after_metadata_wait(db_session):
    job, parent, files, metadata = await setup_conflict(db_session)

    async def changed(*args):
        files[0].status = ImportedFileStatus.SKIPPED
        await db_session.flush()
        return [IssueSummary("201", 1, "Second", "2020-01-01", None, "issue")]

    metadata.get_issue_summaries_for_series.side_effect = changed
    with pytest.raises(ValidationError, match="changed"):
        await assign_review_file(
            db_session, job.id, files[0].id, cv_id=20, issue_cv_id=201, metadata_service=metadata
        )
    assert files[0].status is ImportedFileStatus.SKIPPED
    assert files[0].import_series_id == parent.id
