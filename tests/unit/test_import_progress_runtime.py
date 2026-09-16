"""Shared import progress runtime contract tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pullbox.services.import_progress_runtime import (
    ImportGroupProgressPlan,
    ImportProgressFileProfile,
    ImportProgressSettings,
    ScanReviewFileMatchProfile,
    ScanReviewSeriesMatchProfile,
    current_item_payload,
    default_phase_message,
    elapsed_seconds_since,
    estimate_remaining_work_seconds,
    import_group_file_completed_weight,
    import_group_file_progress_pct,
    import_group_metadata_completed_weight,
    import_group_metadata_progress_pct,
    import_group_progress_plan,
    import_work_progress,
    phase_label,
    phase_range,
    scan_review_completed_weight,
    scan_review_file_target_weight,
    scan_review_progress_pct,
    scan_review_progress_plan,
    scan_review_series_match_weight,
    stage_label,
    weighted_import_progress_pct,
)


def test_import_work_keeps_precision_separate_from_display_percentage(monkeypatch) -> None:
    monkeypatch.setattr(
        "pullbox.services.import_progress_runtime.elapsed_seconds_since", lambda _start: 20
    )
    work = import_work_progress(
        [10.0] * 50_000, current_group_index=0, current_group_completed_weight=2.0
    )
    assert work.completed_weight == 2.0
    assert work.total_weight == 500_000.0
    assert work.progress_pct == 0
    assert work.remaining_seconds(datetime.now(UTC)) == 4_999_980


def test_import_work_does_not_round_away_files_within_large_group() -> None:
    plan = ImportGroupProgressPlan(2.0, tuple((idx, 3.5) for idx in range(2000)))
    metadata = import_group_metadata_completed_weight(plan, metadata_progress_pct=10)
    file_work = import_group_file_completed_weight(plan, file_index=1, current_file_pct=100)
    assert metadata == 0.2
    assert file_work == 5.5
    assert import_group_file_progress_pct(plan, file_index=1, current_file_pct=100) == 0
    work = import_work_progress(
        [plan.total_weight], current_group_index=0, current_group_completed_weight=file_work
    )
    assert work.completed_weight == 5.5
    assert work.progress_pct == 0


def test_import_work_keeps_unknown_and_completion_bounds() -> None:
    started = datetime.now(UTC) - timedelta(seconds=20)
    empty = import_work_progress([], current_group_index=0, current_group_completed_weight=0)
    assert empty.progress_pct == 0
    assert empty.remaining_seconds(started) is None
    pending = import_work_progress(
        [10.0], current_group_index=-1, current_group_completed_weight=-5
    )
    assert pending.completed_weight == 0
    assert pending.remaining_seconds(started) is None
    done = import_work_progress([10.0], current_group_index=0, current_group_completed_weight=1000)
    assert done.completed_weight == 10
    assert done.progress_pct == 99
    assert done.remaining_seconds(started) is None
    active = import_work_progress([10.0], current_group_index=0, current_group_completed_weight=1)
    assert active.remaining_seconds(None) is None
    assert active.remaining_seconds(datetime.now(UTC)) is None


def test_shared_progress_spec_owns_phase_ranges_labels_and_defaults() -> None:
    assert phase_range("inventory") == (0, 10)
    assert phase_range("scanning") == (10, 35)
    assert phase_range("matching") == (45, 80)
    assert phase_range("file_matching") == (80, 99)
    assert phase_range("importing") == (0, 100)

    assert phase_label("matching") == "Matching series against ComicVine..."
    assert phase_label("importing") == "Importing series into Pullbox..."
    assert default_phase_message("scan", "inventory") == "Inventorying collection..."
    assert default_phase_message("import", "importing") == "Importing selected files..."
    assert stage_label("comicinfo_metadata") == "Preparing ComicInfo metadata"


def test_current_item_payload_is_explicit_and_clamped() -> None:
    payload = current_item_payload(
        kind="series",
        stage="metadata_fetch_wait",
        name="2000AD",
        progress_pct=125,
        detail="Large series can take a few minutes.",
    )

    assert payload == {
        "current_item_kind": "series",
        "current_item_stage": "metadata_fetch_wait",
        "current_item_stage_label": "Fetching ComicVine metadata",
        "current_item_progress_pct": 100,
        "current_item_detail": "Large series can take a few minutes.",
    }


def test_elapsed_seconds_since_uses_wall_clock_runtime() -> None:
    started_at = datetime.now(UTC) - timedelta(seconds=42)

    elapsed = elapsed_seconds_since(started_at)

    assert elapsed is not None
    assert 40 <= elapsed <= 45
    assert elapsed_seconds_since(None) is None


def test_estimate_remaining_work_seconds_uses_total_units() -> None:
    started_at = datetime.now(UTC) - timedelta(seconds=20)

    remaining = estimate_remaining_work_seconds(
        started_at,
        completed_units=5,
        total_units=20,
    )

    assert remaining is not None
    assert 55 <= remaining <= 65


def test_estimate_remaining_work_seconds_counts_current_unit_progress() -> None:
    started_at = datetime.now(UTC) - timedelta(seconds=20)

    remaining = estimate_remaining_work_seconds(
        started_at,
        completed_units=0,
        total_units=10,
        current_unit_progress_pct=50,
    )

    assert remaining is not None
    assert 370 <= remaining <= 390


def test_estimate_remaining_work_seconds_omits_unstable_values() -> None:
    started_at = datetime.now(UTC) - timedelta(seconds=20)

    assert estimate_remaining_work_seconds(None, completed_units=5, total_units=20) is None
    assert (
        estimate_remaining_work_seconds(
            datetime.now(UTC),
            completed_units=5,
            total_units=20,
        )
        is None
    )
    assert estimate_remaining_work_seconds(started_at, completed_units=0, total_units=20) is None
    assert estimate_remaining_work_seconds(started_at, completed_units=20, total_units=20) is None
    assert estimate_remaining_work_seconds(started_at, completed_units=5, total_units=0) is None


def test_scan_review_plan_weights_large_issue_catalogs() -> None:
    small = ScanReviewFileMatchProfile(file_count=1, issue_count=12)
    giant = ScanReviewFileMatchProfile(file_count=1, issue_count=2_800)

    assert scan_review_file_target_weight(giant) > scan_review_file_target_weight(small) * 4


def test_scan_review_plan_weights_direct_series_matches_lower() -> None:
    direct = ScanReviewSeriesMatchProfile(file_count=1, direct_match=True)
    searched = ScanReviewSeriesMatchProfile(file_count=1, direct_match=False)

    assert scan_review_series_match_weight(direct) < scan_review_series_match_weight(searched)


def test_scan_review_progress_scales_post_scan_work() -> None:
    plan = scan_review_progress_plan(
        analysis_series_count=2,
        series_match_profiles=[
            ScanReviewSeriesMatchProfile(file_count=1, direct_match=False),
            ScanReviewSeriesMatchProfile(file_count=2, direct_match=True),
        ],
        file_match_profiles=[
            ScanReviewFileMatchProfile(file_count=1, issue_count=12),
            ScanReviewFileMatchProfile(file_count=2, issue_count=2_800),
        ],
    )

    after_analysis = scan_review_completed_weight(
        plan,
        phase="analyzing",
        completed_items=2,
    )
    after_first_series_match = scan_review_completed_weight(
        plan,
        phase="matching",
        completed_items=1,
    )
    first_file_match_started = scan_review_completed_weight(
        plan,
        phase="file_matching",
        completed_items=0,
        current_item_progress_pct=0,
    )

    assert scan_review_progress_pct(plan, completed_weight=0) == 35
    assert (
        35
        < scan_review_progress_pct(plan, completed_weight=after_analysis)
        < scan_review_progress_pct(plan, completed_weight=after_first_series_match)
        < scan_review_progress_pct(plan, completed_weight=first_file_match_started)
        < 99
    )
    assert scan_review_progress_pct(plan, completed_weight=plan.total_weight) == 99


def test_weighted_import_progress_prioritizes_large_conversion_work() -> None:
    settings = ImportProgressSettings(
        move_to_library=True,
        convert_to_preferred_format=True,
        update_embedded_comicinfo_from_match=True,
    )
    tiny_cbz = ImportProgressFileProfile(
        file_id=1,
        file_path="/imports/Tiny 001.cbz",
        file_size=1 * 1024 * 1024,
    )
    large_pdf = ImportProgressFileProfile(
        file_id=2,
        file_path="/imports/Giant Collection.pdf",
        file_size=900 * 1024 * 1024,
    )
    tiny_plan = import_group_progress_plan(settings, [tiny_cbz])
    large_plan = import_group_progress_plan(settings, [large_pdf])

    assert large_plan.total_weight > tiny_plan.total_weight * 5
    assert (
        weighted_import_progress_pct(
            [tiny_plan.total_weight, large_plan.total_weight],
            current_group_index=0,
            current_group_progress_pct=100,
        )
        < 20
    )


def test_import_group_progress_accounts_for_metadata_and_file_work() -> None:
    settings = ImportProgressSettings(
        move_to_library=True,
        convert_to_preferred_format=True,
        update_embedded_comicinfo_from_match=True,
    )
    files = [
        ImportProgressFileProfile(
            file_id=1,
            file_path="/imports/Alpha 001.cbz",
            file_size=10 * 1024 * 1024,
        ),
        ImportProgressFileProfile(
            file_id=2,
            file_path="/imports/Alpha 002.cbr",
            file_size=80 * 1024 * 1024,
        ),
    ]
    plan = import_group_progress_plan(settings, files)

    metadata_half = import_group_metadata_progress_pct(plan, metadata_progress_pct=50)
    first_file_started = import_group_file_progress_pct(plan, file_index=1, current_file_pct=0)
    first_file_done = import_group_file_progress_pct(plan, file_index=1, current_file_pct=100)

    assert 0 < metadata_half < first_file_started < first_file_done < 100
