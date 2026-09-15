"""Review lanes describe work without changing import or recovery decisions."""

from __future__ import annotations

import pytest

from pullbox.ui.import_review_lanes import ReviewFacts, classify_review_row


def test_partial_series_keeps_ready_files_while_needing_issue_decisions() -> None:
    row = classify_review_row(
        ReviewFacts(status="matched", known_target=True, ready_files=13, unmatched_files=3)
    )
    assert row.lane == "decide"
    assert row.reasons == ("needs_issue",)
    assert row.ready_files == 13
    assert row.attention_files == 3


@pytest.mark.parametrize(
    ("category", "lane", "reason"),
    [
        ("single_page_comic", "decide", "single_page_comic"),
        ("decompression_size_limit", "decide", "decompression_size_limit"),
        ("source_missing", "info", "source_missing"),
        ("permission_unreadable", "fix_source", "permission_unreadable"),
        ("archive_inspection_failed", "fix_source", "archive_inspection_failed"),
        ("outside_approved_root", "fix_source", "outside_approved_root"),
        ("dangerous_path_or_payload", "blocked", "dangerous_path_or_payload"),
        ("unsupported_file_type", "blocked", "unsupported_file_type"),
        ("unknown", "blocked", "unknown"),
    ],
)
def test_safety_reasons_have_honest_lanes(category: str, lane: str, reason: str) -> None:
    row = classify_review_row(
        ReviewFacts(status="no_match", known_target=True, safety_counts={category: 1})
    )
    assert row.lane == lane
    assert row.reasons == (reason,)
    assert row.ready_files == 0


def test_mixed_row_appears_once_and_preserves_secondary_reasons() -> None:
    row = classify_review_row(
        ReviewFacts(
            status="matched",
            known_target=True,
            ready_files=2,
            unmatched_files=1,
            safety_counts={"decompression_size_limit": 1, "source_missing": 2},
        )
    )
    assert row.lane == "decide"
    assert row.reasons == ("needs_issue", "decompression_size_limit", "source_missing")
    assert row.ready_files == 2
    assert row.attention_files == 4


def test_preparing_match_is_processing_not_confirmation() -> None:
    row = classify_review_row(
        ReviewFacts(status="no_match", known_target=True, pending=True, approved_files=1)
    )
    assert row.lane == "decide"
    assert row.updating is True
    assert row.reasons == ("preparing_match",)


def test_approved_file_without_pending_worker_still_allows_series_matching() -> None:
    row = classify_review_row(ReviewFacts(status="no_match", approved_files=1))
    assert row.reasons == ("needs_series",)
    assert row.updating is False
    assert row.attention_files == 1


def test_already_owned_and_skipped_are_information_without_decisions() -> None:
    for status in ("duplicate", "skipped", "imported"):
        row = classify_review_row(ReviewFacts(status=status, known_target=True))
        assert row.lane == "info"
        assert row.attention_files == 0


def test_conflicting_identity_is_not_a_suggested_duplicate_keeper() -> None:
    row = classify_review_row(
        ReviewFacts(status="matched", known_target=True, conflict_files=2, identity_conflict=True)
    )
    assert row.lane == "decide"
    assert row.reasons == ("same_comic_review",)


def test_duplicate_candidates_require_a_decision_but_exact_copies_do_not() -> None:
    conflict = classify_review_row(
        ReviewFacts(status="matched", known_target=True, conflict_files=2)
    )
    ready = classify_review_row(ReviewFacts(status="matched", known_target=True, ready_files=1))
    assert conflict.lane == "confirm"
    assert conflict.reasons == ("duplicate_copy_confirm",)
    assert ready.lane == "ready"


@pytest.mark.asyncio
async def test_workspace_lanes_partition_rows_without_changing_saved_recovery(db_session) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from pullbox.models.import_job import (
        ImportedFile,
        ImportedFileStatus,
        ImportedSeries,
        ImportJob,
        ImportJobStatus,
        ImportSeriesStatus,
        ImportSourceType,
    )
    from pullbox.ui.import_review_context import load_import_review_context

    job = ImportJob(
        source_path="/fixtures", source_type=ImportSourceType.MYLAR3, status=ImportJobStatus.REVIEW
    )
    db_session.add(job)
    await db_session.flush()
    partial = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Dawn of X",
        status=ImportSeriesStatus.MATCHED,
        cv_id=123,
        files_total=16,
        file_count=16,
        files_matched=13,
        files_no_match=3,
        selected_for_import=True,
    )
    missing = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Old Mylar reference",
        status=ImportSeriesStatus.NO_MATCH,
        cv_id=456,
        file_count=1,
        files_total=1,
        diagnostics={"kind": "mylar3_path_incompatible"},
    )
    db_session.add_all([partial, missing])
    await db_session.flush()
    for index in range(16):
        db_session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=partial.id,
                file_path=f"/fixtures/dawn-{index}.cbz",
                file_name=f"dawn-{index}.cbz",
                file_format="cbz",
                status=ImportedFileStatus.MATCHED if index < 13 else ImportedFileStatus.NO_MATCH,
            )
        )
    stale = ImportedFile(
        import_job_id=job.id,
        import_series_id=missing.id,
        file_path="/fixtures/missing.cbz",
        file_name="missing.cbz",
        file_format="cbz",
        status=ImportedFileStatus.SAFETY_BLOCKED,
        diagnostics={
            "safety_block": {"code": "source_missing", "category": "source_missing"},
            "source_metadata": {"mylar3_issue": {"IssueID": "987"}},
        },
    )
    db_session.add(stale)
    await db_session.flush()
    before = dict(stale.diagnostics)
    context = await load_import_review_context(db_session, job, status="decide", page=1, sort=None)
    assert [row.id for row in context["series_items"]] == [partial.id]
    assert context["lane_counts"]["decide"] == 1
    assert context["lane_counts"]["info"] == 1
    assert sum(context["lane_counts"].values()) == 2
    assert context["review_rows"][partial.id].ready_files == 13
    assert context["review_summary"]["selected_items_total"] == 1
    assert not db_session.dirty
    assert (
        await db_session.scalar(select(ImportedFile).where(ImportedFile.id == stale.id))
    ).diagnostics == before
