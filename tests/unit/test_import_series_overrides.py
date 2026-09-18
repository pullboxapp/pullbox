"""Tests for manual import-series override helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pullbox.core.exceptions import NotFoundError
from pullbox.models.import_job import ImportedSeries, ImportJob, ImportJobStatus, ImportSourceType
from pullbox.providers.base import SeriesMetadata
from pullbox.services.import_series_overrides import override_cv_id

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _create_job_row(session: AsyncSession) -> ImportJob:
    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=ImportJobStatus.REVIEW,
    )
    session.add(job)
    await session.flush()
    return job


async def _create_series_row(session: AsyncSession, job: ImportJob) -> ImportedSeries:
    series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Almost Batman",
        raw_year=2016,
    )
    session.add(series)
    await session.flush()
    return series


def _series_metadata(cv_id: int) -> SeriesMetadata:
    return SeriesMetadata(
        provider_id=str(cv_id),
        title="Batman",
        sort_title="batman",
        year_start=2016,
        year_end=None,
        status="Ended",
        publisher="DC Comics",
        description=None,
        cover_url=None,
        issue_count=85,
        comicvine_url=f"https://comicvine.gamespot.com/batman/4050-{cv_id}/",
    )


@pytest.mark.parametrize(
    "review_reason",
    [None, "volume_leaf_identity_unconfirmed", "volume_leaf_release_unconfirmed"],
)
async def test_override_cv_id_sets_metadata_and_reruns_matching(
    db_session: AsyncSession,
    review_reason: str | None,
) -> None:
    job = await _create_job_row(db_session)
    series = await _create_series_row(db_session, job)
    if review_reason:
        series.diagnostics = {"kind": "source_layout_review", "reason": review_reason}
    calls: list[tuple[str, list[int] | None]] = []

    async def fetch_series_metadata(cv_id: int) -> SeriesMetadata:
        return _series_metadata(cv_id)

    async def reclassify_matched_series_duplicates(
        _session: AsyncSession,
        _job: ImportJob,
        *,
        series_ids: list[int] | None = None,
    ) -> int:
        calls.append(("reclassify", series_ids))
        return 0

    def logical_series_group_key(
        _series: ImportedSeries,
        *,
        prefer_resolved_cv_only: bool = False,
    ) -> tuple[object, ...] | None:
        assert prefer_resolved_cv_only is True
        return None

    async def logical_group_series_ids(
        _session: AsyncSession,
        _job_id: int,
        _group_key: tuple[object, ...],
        *,
        prefer_resolved_cv_only: bool = False,
    ) -> list[int]:
        raise AssertionError("logical group lookup should not run without a group key")

    async def reset_series_group_files(
        _session: AsyncSession,
        *,
        job_id: int,
        series_ids: list[int],
    ) -> None:
        assert job_id == job.id
        calls.append(("reset", series_ids))

    async def consolidate_logical_series_groups(
        _session: AsyncSession,
        _job: ImportJob,
        *,
        series_ids: list[int] | None = None,
        prefer_resolved_cv_only: bool = False,
    ) -> dict[int, int]:
        assert prefer_resolved_cv_only is True
        calls.append(("consolidate", series_ids))
        return {series.id: series.id}

    async def run_file_matching(
        _session: AsyncSession,
        _job: ImportJob,
        *,
        series_ids: list[int] | None = None,
    ) -> None:
        calls.append(("match", series_ids))

    updated = await override_cv_id(
        db_session,
        series.id,
        12345,
        fetch_series_metadata=fetch_series_metadata,
        reclassify_matched_series_duplicates=reclassify_matched_series_duplicates,
        logical_series_group_key=logical_series_group_key,
        logical_group_series_ids=logical_group_series_ids,
        reset_series_group_files=reset_series_group_files,
        consolidate_logical_series_groups=consolidate_logical_series_groups,
        run_file_matching=run_file_matching,
    )

    assert updated.user_selected_cv_id == 12345
    assert updated.cv_title == "Batman"
    assert updated.cv_publisher == "DC Comics"
    assert updated.cv_match_method == "user_override"
    assert updated.diagnostics == {}
    assert calls == [
        ("reclassify", [series.id]),
        ("reset", [series.id]),
        ("consolidate", [series.id]),
        ("match", [series.id]),
    ]


async def test_override_cv_id_raises_for_missing_series(db_session: AsyncSession) -> None:
    async def fetch_series_metadata(cv_id: int) -> SeriesMetadata:
        return _series_metadata(cv_id)

    async def reclassify_matched_series_duplicates(
        _session: AsyncSession,
        _job: ImportJob,
        *,
        series_ids: list[int] | None = None,
    ) -> int:
        return 0

    async def noop_reset(
        _session: AsyncSession,
        *,
        job_id: int,
        series_ids: list[int],
    ) -> None:
        return None

    async def noop_consolidate(
        _session: AsyncSession,
        _job: ImportJob,
        *,
        series_ids: list[int] | None = None,
        prefer_resolved_cv_only: bool = False,
    ) -> dict[int, int]:
        return {}

    async def noop_match(
        _session: AsyncSession,
        _job: ImportJob,
        *,
        series_ids: list[int] | None = None,
    ) -> None:
        return None

    with pytest.raises(NotFoundError):
        await override_cv_id(
            db_session,
            9999,
            12345,
            fetch_series_metadata=fetch_series_metadata,
            reclassify_matched_series_duplicates=reclassify_matched_series_duplicates,
            logical_series_group_key=lambda _series, **_kwargs: None,
            logical_group_series_ids=lambda _session, _job_id, _group_key, **_kwargs: noop_ids(),
            reset_series_group_files=noop_reset,
            consolidate_logical_series_groups=noop_consolidate,
            run_file_matching=noop_match,
        )


async def noop_ids() -> list[int]:
    return []
