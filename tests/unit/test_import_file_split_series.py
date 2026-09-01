"""Unit tests for import file split-series helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pullbox.core.source_metadata import MetadataSignal, SourceMetadata
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportSeriesStatus,
)
from pullbox.models.issue import IssueType
from pullbox.models.series import Series
from pullbox.services import import_file_split_series
from pullbox.services.import_file_split_series import move_file_to_split_series


def test_move_file_to_split_series_resets_match_state_and_updates_split_counts() -> None:
    parent_series = ImportedSeries(
        id=10,
        raw_series_name="Absolute Martian Manhunter",
        file_count=3,
    )
    split_series = ImportedSeries(
        id=20,
        raw_series_name="Absolute Martian Manhunter: Vol. 1: Martian Vision",
        sample_paths=[],
        file_count=0,
    )
    imp_file = ImportedFile(
        id=30,
        import_series_id=parent_series.id,
        file_path="/tmp/Absolute Martian Manhunter Vol 01.cbz",
        file_name="Absolute Martian Manhunter Vol 01.cbz",
        status=ImportedFileStatus.MATCHED,
        include_in_import=True,
        matched_issue_id=100,
        matched_issue_cv_id=200,
        match_confidence="high",
        match_method="comicvine_id",
        conflict_group_id=1,
        duplicate_group_id=2,
        duplicate_of_file_id=40,
        is_preferred=True,
        content_hash="abc123",
        diagnostics={"previous": "value"},
    )

    move_file_to_split_series(
        imp_file,
        split_series=split_series,
        parent_series=parent_series,
        trigger_issue_cv_id=111111,
        resolved_series_cv_id=168590,
    )

    assert imp_file.import_series_id == split_series.id
    assert imp_file.status == ImportedFileStatus.PENDING
    assert imp_file.include_in_import is False
    assert imp_file.matched_issue_id is None
    assert imp_file.matched_issue_cv_id is None
    assert imp_file.match_confidence is None
    assert imp_file.match_method is None
    assert imp_file.conflict_group_id is None
    assert imp_file.duplicate_group_id is None
    assert imp_file.duplicate_of_file_id is None
    assert imp_file.is_preferred is False
    assert imp_file.content_hash is None
    assert imp_file.diagnostics["previous"] == "value"
    assert imp_file.diagnostics["split_series"] == {
        "reason": "explicit_issue_series_mismatch",
        "source_import_series_id": parent_series.id,
        "source_import_series_name": parent_series.raw_series_name,
        "target_import_series_id": split_series.id,
        "target_series_cv_id": 168590,
        "trigger_issue_cv_id": 111111,
    }
    assert split_series.sample_paths == [imp_file.file_path]
    assert split_series.file_count == 1


@pytest.mark.asyncio
async def test_trusted_comicinfo_series_identity_skips_collection_provider_lookup() -> None:
    job = ImportJob(id=1)
    parent_series = ImportedSeries(
        id=10,
        import_job_id=job.id,
        raw_series_name="Konna Black Jack wa Iyada",
        cv_id=100,
        status=ImportSeriesStatus.MATCHED,
    )
    imp_file = ImportedFile(
        id=20,
        import_job_id=job.id,
        import_series_id=parent_series.id,
        file_path="/tmp/Issue 1 - Volume 1.cbz",
        file_name="Issue 1 - Volume 1.cbz",
        status=ImportedFileStatus.PENDING,
    )
    metadata = SourceMetadata(
        original_title=imp_file.file_name,
        series_name="Konna Black Jack wa Iyada",
        comicvine_series_id=100,
        comicvine_issue_id=501,
        issue_type=IssueType.TPB,
        signals={
            "comicvine_series_id": MetadataSignal.COMICINFO,
            "comicvine_issue_id": MetadataSignal.COMICINFO,
        },
    )
    provider = SimpleNamespace(get_issue=AsyncMock())

    remaining, split_ids = await import_file_split_series.split_explicit_issue_series_mismatches(
        AsyncMock(),
        job,
        parent_series,
        [imp_file],
        metadata_provider=provider,
        source_metadata_for_import_file=lambda _series, _file: metadata,
        target_series=Series(
            id=30,
            title="Konna Black Jack wa Iyada",
            sort_title="konna black jack wa iyada",
            comicvine_id=100,
        ),
        log_event=AsyncMock(),
    )

    assert remaining == [imp_file]
    assert split_ids == []
    provider.get_issue.assert_not_awaited()


@pytest.mark.asyncio
async def test_page_split_waits_for_parent_file_count_before_marking_series_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ImportJob(id=1)
    parent_series = ImportedSeries(
        id=10,
        import_job_id=job.id,
        raw_series_name="Parent",
        cv_id=100,
        status=ImportSeriesStatus.MATCHED,
        selected_for_import=True,
        sample_paths=["/tmp/first.cbz", "/tmp/second.cbz"],
        file_count=2,
    )
    split_series = ImportedSeries(
        id=20,
        import_job_id=job.id,
        raw_series_name="Split",
        cv_id=200,
        status=ImportSeriesStatus.MATCHED,
        sample_paths=[],
        file_count=0,
    )
    target_series = Series(id=30, title="Parent", sort_title="parent", comicvine_id=100)
    first_file = ImportedFile(
        id=40,
        import_job_id=job.id,
        import_series_id=parent_series.id,
        file_path="/tmp/first.cbz",
        file_name="first.cbz",
        status=ImportedFileStatus.PENDING,
    )
    second_file = ImportedFile(
        id=41,
        import_job_id=job.id,
        import_series_id=parent_series.id,
        file_path="/tmp/second.cbz",
        file_name="second.cbz",
        status=ImportedFileStatus.PENDING,
    )
    metadata_by_file_id = {
        first_file.id: SourceMetadata(
            original_title=first_file.file_name,
            series_name="Split",
            comicvine_issue_id=501,
            issue_type=IssueType.TPB,
            signals={"comicvine_issue_id": MetadataSignal.COMICINFO},
        ),
        second_file.id: SourceMetadata(
            original_title=second_file.file_name,
            series_name="Split",
            comicvine_issue_id=502,
            issue_type=IssueType.TPB,
            signals={"comicvine_issue_id": MetadataSignal.COMICINFO},
        ),
    }
    provider = SimpleNamespace(
        get_issue=AsyncMock(return_value=SimpleNamespace(series_provider_id="200"))
    )
    monkeypatch.setattr(
        import_file_split_series,
        "_load_or_create_split_series",
        AsyncMock(return_value=split_series),
    )
    session = AsyncMock()
    log_event = AsyncMock()

    remaining, _split_ids = await import_file_split_series.split_explicit_issue_series_mismatches(
        session,
        job,
        parent_series,
        [first_file],
        metadata_provider=provider,
        source_metadata_for_import_file=lambda _series, imp_file: metadata_by_file_id[imp_file.id],
        target_series=target_series,
        log_event=log_event,
    )

    assert remaining == []
    assert parent_series.file_count == 1
    assert parent_series.status == ImportSeriesStatus.MATCHED
    assert parent_series.selected_for_import is True

    remaining, _split_ids = await import_file_split_series.split_explicit_issue_series_mismatches(
        session,
        job,
        parent_series,
        [second_file],
        metadata_provider=provider,
        source_metadata_for_import_file=lambda _series, imp_file: metadata_by_file_id[imp_file.id],
        target_series=target_series,
        log_event=log_event,
    )

    assert remaining == []
    assert parent_series.file_count == 0
    assert parent_series.status == ImportSeriesStatus.SKIPPED
    assert parent_series.selected_for_import is False
    assert parent_series.diagnostics["moved_file_ids"] == [40, 41]
    assert parent_series.diagnostics["split_series_ids"] == [20]
