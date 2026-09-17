"""Provisional targets must join proven issue identities before copy review."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pullbox.core.exceptions import JobCancelledError
from pullbox.core.source_metadata import MetadataSignal, SourceMetadata
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.services import import_file_matching, import_provisional_targets
from pullbox.services.import_file_conflicts import detect_conflicts
from pullbox.services.import_file_match_targets import PROVIDER_MISSING_ISSUE_PLACEHOLDER_METHOD
from pullbox.services.import_service import ImportService


async def _pair(session, *, source_type, mode, reverse=False, number=1.0, text="1"):
    job = ImportJob(
        source_path="/fixtures",
        source_type=source_type,
        status=ImportJobStatus.FILE_MATCHING,
        file_handling_mode=mode,
    )
    session.add(job)
    await session.flush()
    series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="X-Force",
        cv_title="X-Force",
        cv_id=71614,
        cv_match_method=(
            "mylar3_cv_id" if source_type == ImportSourceType.MYLAR3 else "folder_cv_id"
        ),
        cv_match_score=1.0,
        status=ImportSeriesStatus.MATCHED,
        file_count=2,
    )
    session.add(series)
    await session.flush()
    common = {
        "import_job_id": job.id,
        "import_series_id": series.id,
        "status": ImportedFileStatus.MATCHED,
        "parsed_series": "X-Force",
        "parsed_year": 2014,
        "parsed_issue_number": number,
        "issue_number_raw": text,
        "match_confidence": "high",
    }
    evidence = {
        "comicvine_series_id": 71614,
        "source_issue_type": "issue",
        "source_metadata": {"has_comicinfo": False},
    }
    provisional = ImportedFile(
        **common,
        file_path=f"/fixtures/X-Force {text} (2014).cbr",
        file_name=f"X-Force {text} (2014).cbr",
        file_size=37522498,
        file_format="cbr",
        has_comicinfo=False,
        match_method=PROVIDER_MISSING_ISSUE_PLACEHOLDER_METHOD,
        diagnostics={
            **deepcopy(evidence),
            "kind": "provider_missing_issue_placeholder",
            "target_state": "provisional_issue_target",
            "target_series_cv_id": 71614,
            "target_issue_number": number,
            "target_issue_type": "issue",
            "rejection_reason": "Metadata will hydrate after import.",
            "review_selection": True,
        },
        source_signature={"size": 37522498, "inode": 100},
    )
    exact = ImportedFile(
        **common,
        file_path=f"/fixtures/X-Force {text} (2014).cbz",
        file_name=f"X-Force {text} (2014).cbz",
        file_size=39155934,
        file_format="cbz",
        has_comicinfo=True,
        comicvine_issue_id=445188,
        matched_issue_cv_id=445188,
        match_method="comicvine_id",
        diagnostics={
            **deepcopy(evidence),
            "target_issue_summary": {
                "provider_id": "445188",
                "issue_number": number,
                "issue_number_text": text,
                "issue_type": "issue",
                "title": "Offensive Acts",
                "release_date": None,
                "cover_url": None,
            },
        },
    )
    session.add_all([exact, provisional] if reverse else [provisional, exact])
    await session.flush()
    return job, series, provisional, exact


async def _finalize(session, job, series):
    return await import_file_matching._finalize_import_series_file_groups(
        session,
        job,
        series,
        duplicate_group_counter=0,
        conflict_group_counter=0,
        detect_duplicate_copies=AsyncMock(return_value=(0, 0, [])),
        detect_conflicts=detect_conflicts,
        log_event=AsyncMock(),
        raise_if_cancelled=AsyncMock(),
    )


@pytest.mark.parametrize("source_type", [ImportSourceType.MYLAR3, ImportSourceType.FILESYSTEM])
@pytest.mark.parametrize(
    "mode", [ImportFileHandlingMode.IN_PLACE, ImportFileHandlingMode.MANAGED_COPY]
)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("number,text", [(1.0, "1"), (13.0, "13a"), (0.5, "0.5"), (-1.0, "-1")])
async def test_finalization_groups_provisional_and_exact_copies_across_pages(
    db_session, monkeypatch, source_type, mode, reverse, number, text
):
    job, series, provisional, exact = await _pair(
        db_session, source_type=source_type, mode=mode, reverse=reverse, number=number, text=text
    )
    monkeypatch.setattr(import_provisional_targets, "_PAGE_SIZE", 1)
    signature = deepcopy(provisional.source_signature)
    evidence = deepcopy(provisional.diagnostics["source_metadata"])

    _duplicates, conflicts = await _finalize(db_session, job, series)

    assert conflicts == 1
    assert provisional.status == exact.status == ImportedFileStatus.CONFLICT
    assert provisional.conflict_group_id == exact.conflict_group_id
    assert provisional.matched_issue_cv_id == exact.matched_issue_cv_id == 445188
    assert provisional.match_method == "issue_number"
    assert exact.is_preferred is True
    assert provisional.include_in_import is False
    previous = provisional.diagnostics["previous_diagnostics"]
    assert (
        previous["target_issue_summary"]
        == exact.diagnostics["previous_diagnostics"]["target_issue_summary"]
    )
    assert previous["provisional_target_reconciliation"]["evidence_file_id"] == exact.id
    assert "target_state" not in previous
    assert previous["source_metadata"] == evidence
    assert provisional.source_signature == signature
    assert provisional.comicvine_issue_id is None


@pytest.mark.parametrize(
    "case",
    [
        "annual",
        "letter",
        "unknown_type",
        "conflicting_identity",
        "other_series",
        "other_job",
        "ambiguous",
        "blocked",
        "skipped",
        "manual",
        "manual_provisional",
        "wrong_summary",
        "changed_number",
        "source_issue_id",
        "unsafe_target",
        "unmatched_target",
    ],
)
async def test_provisional_reconciliation_preserves_uncertain_or_reviewed_files(db_session, case):
    job, series, provisional, exact = await _pair(
        db_session, source_type=ImportSourceType.MYLAR3, mode=ImportFileHandlingMode.IN_PLACE
    )
    if case in {"annual", "unknown_type"}:
        diagnostics = deepcopy(exact.diagnostics)
        diagnostics["target_issue_summary"]["issue_type"] = (
            "annual" if case == "annual" else "unknown"
        )
        diagnostics["source_issue_type"] = diagnostics["target_issue_summary"]["issue_type"]
        exact.diagnostics = diagnostics
    elif case == "letter":
        exact.issue_number_raw = "1a"
        diagnostics = deepcopy(exact.diagnostics)
        diagnostics["target_issue_summary"]["issue_number_text"] = "1a"
        exact.diagnostics = diagnostics
    elif case == "conflicting_identity":
        provisional.diagnostics = {
            **provisional.diagnostics,
            "source_metadata": {"identity_conflicts": [{"field": "comicvine_issue_id"}]},
        }
    elif case in {"other_series", "other_job"}:
        other_job, other_series, _other_provisional, _other_exact = await _pair(
            db_session, source_type=ImportSourceType.MYLAR3, mode=ImportFileHandlingMode.IN_PLACE
        )
        if case == "other_series":
            exact.import_series_id = other_series.id
        else:
            exact.import_job_id = other_job.id
    elif case == "ambiguous":
        competing = ImportedFile(
            import_job_id=job.id,
            import_series_id=series.id,
            file_path="/fixtures/competing.cbz",
            file_name="X-Force 1 (2014) variant.cbz",
            file_size=1000,
            file_format="cbz",
            parsed_issue_number=1,
            issue_number_raw="1",
            matched_issue_cv_id=999999,
            status=ImportedFileStatus.MATCHED,
            diagnostics={
                **deepcopy(exact.diagnostics),
                "target_issue_summary": {
                    **exact.diagnostics["target_issue_summary"],
                    "provider_id": "999999",
                },
            },
        )
        db_session.add(competing)
    elif case in {"blocked", "skipped"}:
        provisional.status = (
            ImportedFileStatus.SAFETY_BLOCKED if case == "blocked" else ImportedFileStatus.SKIPPED
        )
    elif case == "manual":
        provisional.match_method = "orphan_recovery"
    elif case == "manual_provisional":
        provisional.diagnostics = {
            **provisional.diagnostics,
            "resolution": "provisional_issue_created",
        }
    elif case == "wrong_summary":
        exact.diagnostics = {
            **exact.diagnostics,
            "target_issue_summary": {
                **exact.diagnostics["target_issue_summary"],
                "provider_id": "999999",
            },
        }
    elif case == "changed_number":
        provisional.diagnostics = {**provisional.diagnostics, "target_issue_number": 2}
    elif case == "source_issue_id":
        provisional.comicvine_issue_id = 999999
    elif case == "unsafe_target":
        exact.diagnostics = {**exact.diagnostics, "safety_block": {"code": "unsafe_archive"}}
    elif case == "unmatched_target":
        exact.status = ImportedFileStatus.NO_MATCH
    await db_session.flush()
    before = deepcopy(provisional.diagnostics)

    count = await import_provisional_targets.reconcile_provisional_targets(
        db_session, job, series, raise_if_cancelled=AsyncMock()
    )

    assert count == 0
    assert provisional.matched_issue_cv_id is None
    assert provisional.diagnostics == before


async def test_reconciliation_is_idempotent_and_cancellable(db_session):
    job, series, provisional, exact = await _pair(
        db_session, source_type=ImportSourceType.FILESYSTEM, mode=ImportFileHandlingMode.IN_PLACE
    )
    cancelled = AsyncMock(side_effect=JobCancelledError("stop"))
    with pytest.raises(JobCancelledError, match="stop"):
        await import_provisional_targets.reconcile_provisional_targets(
            db_session, job, series, raise_if_cancelled=cancelled
        )
    assert provisional.matched_issue_cv_id is None
    first = await import_provisional_targets.reconcile_provisional_targets(
        db_session, job, series, raise_if_cancelled=AsyncMock()
    )
    second = await import_provisional_targets.reconcile_provisional_targets(
        db_session, job, series, raise_if_cancelled=AsyncMock()
    )
    assert first == 1
    assert second == 0
    assert provisional.matched_issue_cv_id == exact.matched_issue_cv_id


@pytest.mark.parametrize("source_type", [ImportSourceType.MYLAR3, ImportSourceType.FILESYSTEM])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("page_size", [1, 2])
async def test_matching_pipeline_groups_late_identity_without_provider_or_file_io(
    db_session, monkeypatch, source_type, reverse, page_size
):
    job, series, provisional, exact = await _pair(
        db_session,
        source_type=source_type,
        mode=ImportFileHandlingMode.IN_PLACE,
        reverse=reverse,
    )
    for file in [provisional, exact]:
        file.status = ImportedFileStatus.PENDING
        file.matched_issue_cv_id = None
        file.match_method = None
        file.diagnostics = {
            "source_issue_type": "issue",
            "comicvine_series_id": series.cv_id,
            "metadata_signals": {
                "comicvine_series_id": (
                    "mylar3" if source_type == ImportSourceType.MYLAR3 else "sidecar"
                ),
            },
            "source_metadata": {
                "archive_metadata_deferred": source_type == ImportSourceType.MYLAR3,
                "archive_entry_issue_hint_checked": True,
            },
        }
    if source_type == ImportSourceType.MYLAR3:
        exact.comicvine_issue_id = None
    else:
        exact.diagnostics = {
            **exact.diagnostics,
            "metadata_signals": {
                **exact.diagnostics["metadata_signals"],
                "comicvine_issue_id": "comicinfo",
            },
        }
    await db_session.commit()
    provider = Mock()
    provider.cache_metrics.return_value = {}
    for name in [
        "get_series", "get_issue", "get_issues_for_series", "get_issues_for_series_by_numbers"
    ]:
        setattr(provider, name, AsyncMock(side_effect=AssertionError("unexpected provider call")))
    service = ImportService(
        series_service=AsyncMock(),
        metadata_service=SimpleNamespace(_provider=provider),
        event_bus=AsyncMock(),
    )

    async def local_metadata(item, file):
        tagged = file.id == exact.id
        return SourceMetadata(
            original_title=file.file_name,
            series_name=item.raw_series_name,
            issue_number=1,
            issue_number_text="1",
            year=2014,
            comicvine_series_id=71614,
            comicvine_issue_id=445188 if tagged else None,
            signals={
                "comicvine_series_id": MetadataSignal.MYLAR3,
                **({"comicvine_issue_id": MetadataSignal.COMICINFO} if tagged else {}),
            },
            diagnostics={
                "has_comicinfo": tagged,
                "archive_metadata_loaded": True,
                "archive_metadata_deferred": False,
                "archive_entry_issue_hint_checked": True,
            },
        )

    loader = AsyncMock(side_effect=local_metadata)
    monkeypatch.setattr(service, "_load_deferred_source_metadata_for_import_file", loader)
    monkeypatch.setattr(service, "_detect_duplicate_copies", AsyncMock(return_value=(0, 0, [])))
    monkeypatch.setattr(import_file_matching, "_FILE_PAGE_SIZE", page_size)
    await service._run_file_matching(db_session, job)

    assert provisional.status == exact.status == ImportedFileStatus.CONFLICT
    assert provisional.conflict_group_id == exact.conflict_group_id
    assert provisional.matched_issue_cv_id == exact.matched_issue_cv_id == 445188
    assert job.total_files_conflict == 2
    assert job.total_files_matched == 0
    assert series.status == ImportSeriesStatus.MATCHED
    assert not [call for call in provider.mock_calls if "get_" in call[0]]
    assert loader.await_count == (2 if source_type == ImportSourceType.MYLAR3 else 0)
