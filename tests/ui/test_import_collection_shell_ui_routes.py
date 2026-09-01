"""Route-contract tests for the unified Import workspace shell."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-import-collection-shell-ui")


async def _seed_import_progress_job(
    sec_db,
    *,
    status: str = "scanning",
) -> int:  # type: ignore[no-untyped-def]
    from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType

    async with sec_db() as session:
        job = ImportJob(
            source_path="/tmp/import-progress-contract",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus(status),
            scan_total_files=42,
            scan_total_dirs=6,
        )
        session.add(job)
        await session.commit()
        return job.id


async def _seed_import_execute_job(
    sec_db,
    *,
    status: str = "completed",
    series_imported: int = 8,
    series_failed: int = 0,
    total_files_imported: int = 24,
    total_files_failed: int = 0,
) -> int:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime

    from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType

    async with sec_db() as session:
        job = ImportJob(
            source_path="/tmp/import-execute-contract",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus(status),
            series_found=12,
            series_imported=series_imported,
            series_failed=series_failed,
            total_files_found=36,
            total_files_imported=total_files_imported,
            total_files_failed=total_files_failed,
            import_started_at=datetime.now(UTC),
            import_completed_at=datetime.now(UTC)
            if status in {"completed", "rolled_back", "cancelled", "failed"}
            else None,
            progress_snapshot={
                "phase": "rollback" if status in {"rolling_back", "rolled_back"} else "importing",
                "progress": 100 if status in {"completed", "rolled_back"} else 64,
                "status": status,
                "message": (
                    "Import rollback completed."
                    if status == "rolled_back"
                    else ("Import complete." if status == "completed" else "Import in progress.")
                ),
            },
        )
        session.add(job)
        await session.commit()
        return job.id


async def _seed_import_cancelled_job(sec_db) -> int:  # type: ignore[no-untyped-def]
    return await _seed_import_execute_job(sec_db, status="cancelled")


async def _seed_import_rolled_back_job(sec_db) -> int:  # type: ignore[no-untyped-def]
    return await _seed_import_execute_job(sec_db, status="rolled_back")


async def _seed_import_review_job(sec_db) -> int:  # type: ignore[no-untyped-def]
    from pullbox.models.import_job import (
        ImportedFile,
        ImportedFileStatus,
        ImportedSeries,
        ImportJob,
        ImportJobLog,
        ImportJobStatus,
        ImportSeriesStatus,
        ImportSourceType,
    )
    from pullbox.models.series import Series

    async with sec_db() as session:
        job = ImportJob(
            source_path="/tmp/import-review-contract",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
            series_found=12,
            series_duplicate=2,
            series_matched=8,
            series_no_match=2,
            total_files_found=36,
            total_files_matched=30,
            total_files_conflict=4,
            total_files_no_match=2,
        )
        session.add(job)
        await session.flush()
        series_statuses = (
            [ImportSeriesStatus.MATCHED] * 8
            + [ImportSeriesStatus.DUPLICATE] * 2
            + [ImportSeriesStatus.NO_MATCH] * 2
        )

        file_index = 0
        for idx, series_status in enumerate(series_statuses, start=1):
            series = ImportedSeries(
                import_job_id=job.id,
                status=series_status,
                raw_series_name=f"Review Series {idx}",
                raw_year=2020 + idx,
                raw_publisher="Test Publisher",
                file_count=3,
                files_total=3,
                files_matched=3 if series_status == ImportSeriesStatus.MATCHED and idx <= 6 else 1,
                files_conflict=2 if idx in {7, 8} else 0,
                files_no_match=1 if series_status == ImportSeriesStatus.NO_MATCH else 0,
                cv_match_score=0.87 if series_status == ImportSeriesStatus.MATCHED else None,
                sample_paths=[f"/tmp/review-{idx}/sample.cbz"],
                source_folder=f"/tmp/review-{idx}",
                diagnostics=(
                    {
                        "kind": "series_no_match",
                        "reason": "below_threshold",
                        "normalized_query": f"review series {idx}".lower(),
                        "raw_year": 2020 + idx,
                        "threshold": 0.7,
                        "top_candidates": [
                            {
                                "title": f"Candidate Series {idx}",
                                "year": 2019 + idx,
                                "publisher": "Candidate Publisher",
                                "issue_count": 12,
                                "score_pct": 64,
                                "rejection_reasons": ["Below 70% threshold"],
                            }
                        ],
                    }
                    if series_status == ImportSeriesStatus.NO_MATCH
                    else {}
                ),
            )
            if series_status == ImportSeriesStatus.DUPLICATE:
                library_series = Series(
                    title=f"Library Series {idx}",
                    sort_title=f"library series {idx}",
                    year_start=2010 + idx,
                    comicvine_id=400 + idx,
                )
                session.add(library_series)
                await session.flush()
                series.series_id = library_series.id
                series.files_matched = 1
                series.files_already_owned = 1
                series.files_no_match = 1
                series.diagnostics = {
                    "kind": "duplicate_series",
                    "duplicate_reason": "cv_id",
                    "existing_series_id": library_series.id,
                    "existing_series_title": f"Library Series {idx}",
                    "existing_series_year": 2010 + idx,
                    "actionable_duplicate_merge": True,
                    "has_importable_files": True,
                    "importable_files": 1,
                    "already_owned_files": 1,
                    "no_match_files": 1,
                    "conflict_files": 0,
                }
            session.add(series)
            await session.flush()

            for local_idx in range(3):
                status = ImportedFileStatus.MATCHED
                if idx in {7, 8} and local_idx < 2:
                    status = ImportedFileStatus.CONFLICT
                elif series_status == ImportSeriesStatus.NO_MATCH and local_idx == 0:
                    status = ImportedFileStatus.NO_MATCH
                elif series_status == ImportSeriesStatus.DUPLICATE:
                    if local_idx == 0:
                        status = ImportedFileStatus.MATCHED
                    elif local_idx == 1:
                        status = ImportedFileStatus.ALREADY_OWNED
                    else:
                        status = ImportedFileStatus.NO_MATCH

                file_index += 1
                session.add(
                    ImportedFile(
                        import_job_id=job.id,
                        import_series_id=series.id,
                        file_path=f"/tmp/review-{idx}/file-{local_idx + 1}.cbz",
                        file_name=f"Review Series {idx} #{local_idx + 1}.cbz",
                        file_size=1024 * file_index,
                        file_format="cbz",
                        parsed_series=f"Review Series {idx}",
                        parsed_issue_number=float(local_idx + 1),
                        status=status,
                        include_in_import=(
                            series_status == ImportSeriesStatus.DUPLICATE and local_idx == 0
                        ),
                        conflict_group_id=idx if idx in {7, 8} and local_idx < 2 else None,
                        is_preferred=idx in {7, 8} and local_idx == 0,
                        diagnostics=(
                            {
                                "kind": "file_conflict",
                                "conflict_group_id": idx,
                                "preferred_file_id": (
                                    file_index if local_idx == 0 else file_index - 1
                                ),
                                "preferred_file_name": f"Review Series {idx} #1.cbz",
                                "preferred_reasons": ["ComicInfo metadata present"],
                                "why_not_selected": (
                                    []
                                    if local_idx == 0
                                    else ["Lower match confidence than the preferred file"]
                                ),
                            }
                            if idx in {7, 8} and local_idx < 2
                            else (
                                {
                                    "kind": "duplicate_series_file",
                                    "target_state": (
                                        "wanted"
                                        if local_idx == 0
                                        else ("already_owned" if local_idx == 1 else "no_match")
                                    ),
                                }
                                if series_status == ImportSeriesStatus.DUPLICATE
                                else {}
                            )
                        ),
                    )
                )
        session.add(
            ImportJobLog(
                import_job_id=job.id,
                level="INFO",
                event="import_ready_for_review",
                message="Ready for review",
            )
        )
        await session.commit()
        return job.id


async def _seed_large_conflict_review_job(sec_db) -> int:  # type: ignore[no-untyped-def]
    from pullbox.models.import_job import (
        ImportedFile,
        ImportedFileStatus,
        ImportedSeries,
        ImportJob,
        ImportJobStatus,
        ImportSeriesStatus,
        ImportSourceType,
    )

    async with sec_db() as session:
        job = ImportJob(
            source_path="/tmp/import-conflict-pagination",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
            series_found=30,
            series_matched=30,
            total_files_found=60,
            total_files_conflict=60,
        )
        session.add(job)
        await session.flush()

        for idx in range(1, 31):
            series = ImportedSeries(
                import_job_id=job.id,
                status=ImportSeriesStatus.MATCHED,
                raw_series_name=f"Conflict Series {idx}",
                raw_year=2020 + idx,
                file_count=2,
                files_total=2,
                files_matched=0,
                files_conflict=2,
                source_folder=f"/tmp/conflict-{idx}",
            )
            session.add(series)
            await session.flush()

            for local_idx in range(2):
                session.add(
                    ImportedFile(
                        import_job_id=job.id,
                        import_series_id=series.id,
                        file_path=f"/tmp/conflict-{idx}/file-{local_idx + 1}.cbz",
                        file_name=f"Conflict Series {idx} #{local_idx + 1}.cbz",
                        file_size=2048 * (idx + local_idx),
                        file_format="cbz",
                        parsed_series=f"Conflict Series {idx}",
                        parsed_issue_number=1.0,
                        status=ImportedFileStatus.CONFLICT,
                        conflict_group_id=idx,
                        is_preferred=local_idx == 0,
                        diagnostics={
                            "kind": "file_conflict",
                            "conflict_group_id": idx,
                            "preferred_file_id": 0,
                            "preferred_file_name": f"Conflict Series {idx} #1.cbz",
                            "preferred_reasons": ["ComicInfo metadata present"],
                            "why_not_selected": (
                                []
                                if local_idx == 0
                                else ["Lower match confidence than the preferred file"]
                            ),
                        },
                    )
                )

        await session.commit()
        return job.id


async def _seed_import_pending_no_match_series_job(sec_db) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    from pullbox.models.import_job import (
        ImportedFile,
        ImportedFileStatus,
        ImportedSeries,
        ImportJob,
        ImportJobStatus,
        ImportSeriesStatus,
        ImportSourceType,
    )

    async with sec_db() as session:
        job = ImportJob(
            source_path="/tmp/import-pending-no-match",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
            series_found=1,
            series_no_match=1,
            total_files_found=2,
        )
        session.add(job)
        await session.flush()

        series = ImportedSeries(
            import_job_id=job.id,
            status=ImportSeriesStatus.NO_MATCH,
            raw_series_name="Negation (CrossGen )",
            raw_year=None,
            raw_publisher="CrossGen",
            file_count=2,
            files_total=2,
            files_no_match=0,
            source_folder="/tmp/negation",
            diagnostics={
                "kind": "series_no_match",
                "reason": "below_threshold",
                "normalized_query": "negation",
                "threshold": 0.7,
                "top_candidates": [],
            },
        )
        session.add(series)
        await session.flush()

        session.add_all(
            [
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=series.id,
                    file_path="/tmp/negation/Negation 02 (CrossGen 2003).cbz",
                    file_name="Negation 02 (CrossGen 2003).cbz",
                    file_size=1024,
                    file_format="cbz",
                    parsed_series="Negation",
                    parsed_issue_number=2.0,
                    status=ImportedFileStatus.PENDING,
                ),
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=series.id,
                    file_path="/tmp/negation/Negation 04 (CrossGen 2004).cbz",
                    file_name="Negation 04 (CrossGen 2004).cbz",
                    file_size=2048,
                    file_format="cbz",
                    parsed_series="Negation",
                    parsed_issue_number=4.0,
                    status=ImportedFileStatus.PENDING,
                ),
            ]
        )
        await session.commit()
        return job.id, series.id


async def _seed_mixed_conflict_review_job(sec_db) -> int:  # type: ignore[no-untyped-def]
    from pullbox.models.import_job import (
        ImportedFile,
        ImportedFileStatus,
        ImportedSeries,
        ImportJob,
        ImportJobStatus,
        ImportSeriesStatus,
        ImportSourceType,
    )

    async with sec_db() as session:
        job = ImportJob(
            source_path="/tmp/import-conflict-mixed",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
            series_found=1,
            series_matched=1,
            total_files_found=3,
            total_files_conflict=3,
        )
        session.add(job)
        await session.flush()

        series = ImportedSeries(
            import_job_id=job.id,
            status=ImportSeriesStatus.MATCHED,
            raw_series_name="Absolute Martian Manhunter",
            raw_year=2025,
            file_count=3,
            files_total=3,
            files_matched=0,
            files_conflict=3,
            source_folder="/tmp/import-conflict-mixed/folder",
        )
        session.add(series)
        await session.flush()

        file_specs = [
            ("Absolute Martian Manhunter 004.cbz", "Absolute Martian Manhunter", True),
            ("Abattoir 004.cbz", "Abattoir", False),
            ("Chicken Devil 004.cbz", "Chicken Devil", False),
        ]
        for idx, (file_name, parsed_series, preferred) in enumerate(file_specs, start=1):
            session.add(
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=series.id,
                    file_path=f"/tmp/import-conflict-mixed/{file_name}",
                    file_name=file_name,
                    file_size=2048 * idx,
                    file_format="cbz",
                    parsed_series=parsed_series,
                    parsed_issue_number=4.0,
                    status=ImportedFileStatus.CONFLICT,
                    conflict_group_id=1,
                    is_preferred=preferred,
                    has_comicinfo=preferred,
                    diagnostics={
                        "kind": "file_conflict",
                        "conflict_group_id": 1,
                        "preferred_file_id": 1,
                        "preferred_file_name": "Absolute Martian Manhunter 004.cbz",
                        "preferred_reasons": ["ComicInfo metadata present"],
                        "why_not_selected": (
                            [] if preferred else ["Different parsed series in same issue bucket"]
                        ),
                    },
                )
            )

        await session.commit()
        return job.id


async def _seed_filename_consistent_conflict_review_job(sec_db) -> int:  # type: ignore[no-untyped-def]
    from pullbox.models.import_job import (
        ImportedFile,
        ImportedFileStatus,
        ImportedSeries,
        ImportJob,
        ImportJobStatus,
        ImportSeriesStatus,
        ImportSourceType,
    )

    async with sec_db() as session:
        job = ImportJob(
            source_path="/tmp/import-conflict-filename-consistent",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
            series_found=1,
            series_matched=1,
            total_files_found=3,
            total_files_conflict=3,
        )
        session.add(job)
        await session.flush()

        series = ImportedSeries(
            import_job_id=job.id,
            status=ImportSeriesStatus.MATCHED,
            raw_series_name="Chicken Devil",
            raw_year=2021,
            file_count=3,
            files_total=3,
            files_matched=0,
            files_conflict=3,
            source_folder="/tmp/import-conflict-filename-consistent/folder",
        )
        session.add(series)
        await session.flush()

        file_specs = [
            ("Chicken Devil 004 (2022).cbz", "Chicken Devil", True),
            ("Chicken Devil 004 (2022).cb7", "Chicken Devils", False),
            ("Chicken Devil 004 (2022) copy.cbz", "Chicken Devil", False),
        ]
        for idx, (file_name, parsed_series, preferred) in enumerate(file_specs, start=1):
            session.add(
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=series.id,
                    file_path=f"/tmp/import-conflict-filename-consistent/{file_name}",
                    file_name=file_name,
                    file_size=3072 * idx,
                    file_format="cbz" if file_name.endswith(".cbz") else "cb7",
                    parsed_series=parsed_series,
                    parsed_issue_number=4.0,
                    status=ImportedFileStatus.CONFLICT,
                    conflict_group_id=1,
                    is_preferred=preferred,
                    diagnostics={
                        "source_metadata": {
                            "filename_parse": {
                                "series_name": "Chicken Devil",
                                "issue_number": 4.0,
                                "year": 2022,
                            }
                        },
                        "kind": "file_conflict",
                        "conflict_group_id": 1,
                        "preferred_file_id": 1,
                        "preferred_file_name": "Chicken Devil 004 (2022).cbz",
                        "preferred_reasons": ["ComicInfo metadata present"],
                        "why_not_selected": (
                            []
                            if preferred
                            else ["Different archive metadata, same filename-derived issue"]
                        ),
                    },
                )
            )

        await session.commit()
        return job.id


async def _seed_unsorted_conflict_review_job(sec_db) -> int:  # type: ignore[no-untyped-def]
    from pullbox.models.import_job import (
        ImportedFile,
        ImportedFileStatus,
        ImportedSeries,
        ImportJob,
        ImportJobStatus,
        ImportSeriesStatus,
        ImportSourceType,
    )

    async with sec_db() as session:
        job = ImportJob(
            source_path="/tmp/import-conflict-unsorted",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
            series_found=3,
            series_matched=3,
            total_files_found=6,
            total_files_conflict=6,
        )
        session.add(job)
        await session.flush()

        specs = [
            ("Zulu Force", 30),
            ("Abattoir", 10),
            ("Chicken Devil", 20),
        ]
        for idx, (series_name, conflict_group_id) in enumerate(specs, start=1):
            series = ImportedSeries(
                import_job_id=job.id,
                status=ImportSeriesStatus.MATCHED,
                raw_series_name=series_name,
                raw_year=2020 + idx,
                file_count=2,
                files_total=2,
                files_matched=0,
                files_conflict=2,
                source_folder=f"/tmp/{series_name.lower().replace(' ', '-')}",
            )
            session.add(series)
            await session.flush()

            for local_idx in range(2):
                file_stub = f"{series_name} #{local_idx + 1}.cbz"
                file_dir = series_name.lower().replace(" ", "-")
                session.add(
                    ImportedFile(
                        import_job_id=job.id,
                        import_series_id=series.id,
                        file_path=f"/tmp/{file_dir}/{file_stub}",
                        file_name=file_stub,
                        file_size=4096 * (idx + local_idx),
                        file_format="cbz",
                        parsed_series=series_name,
                        parsed_issue_number=1.0,
                        status=ImportedFileStatus.CONFLICT,
                        conflict_group_id=conflict_group_id,
                        is_preferred=local_idx == 0,
                        diagnostics={
                            "kind": "file_conflict",
                            "conflict_group_id": conflict_group_id,
                            "preferred_file_id": 0,
                            "preferred_file_name": f"{series_name} #1.cbz",
                            "preferred_reasons": ["ComicInfo metadata present"],
                            "why_not_selected": (
                                []
                                if local_idx == 0
                                else ["Lower match confidence than the preferred file"]
                            ),
                        },
                    )
                )

        await session.commit()
        return job.id


@pytest.mark.asyncio
class TestImportShellRouteContracts:
    """Verify the unified Import page renders a stable workspace shell."""

    async def test_app_shell_tracks_global_activity_across_navigation(self) -> None:
        script = Path("src/pullbox/ui/static/js/pullbox.js").read_text()
        start = script.index("function appShell()")
        end = script.index("function _readJsonBody")
        shell_controller = script[start:end]

        assert 'fetch("/api/v1/activity"' in shell_controller
        assert 'new EventSource("/api/v1/activity/stream")' in shell_controller
        assert 'source.addEventListener("progress", refreshFromEvent);' in shell_controller
        assert 'fetch("/api/v1/activity/" + operationId + "/acknowledge"' in shell_controller
        assert "activityOverallLabel: function" in shell_controller
        assert "activityItemLabel: function" in shell_controller
        assert "activityRateEtaLabel: function" in shell_controller
        assert "activityOverallIndeterminate: function" in shell_controller
        assert "activityItemIndeterminate: function" in shell_controller
        assert "scheduleActivityPoll(3000)" in shell_controller
        assert 'fetch("/api/v1/import/active"' not in shell_controller

    async def test_step_two_ephemeral_progress_uses_its_real_workflow_status(self) -> None:
        script = Path("src/pullbox/ui/static/js/pullbox.js").read_text()
        start = script.index("applyEphemeralFileProgressNow: function")
        end = script.index("showActionBar: function", start)
        handler = script[start:end]

        assert '"file_matching"' in handler
        assert "this.jobStatus = activeStatus;" in handler
        assert "this.phase = String(data.phase || activeStatus);" in handler
        assert 'this.jobStatus = "importing";' not in handler

    async def test_step_four_has_a_specific_story_arc_placement_retry_control(self) -> None:
        script = Path("src/pullbox/ui/static/js/pullbox.js").read_text()
        template = Path("src/pullbox/ui/templates/partials/import_step_progress.html").read_text()
        start = script.index("function importProgressData")
        end = script.index("function importSeriesDetailsModalData", start)
        controller = script[start:end]

        assert 'data-testid="import-progress-retry-story-arc-placements"' in template
        assert '@click="retryStoryArcPlacements()"' in template
        assert 'x-show="showRetryStoryArcPlacementsAction()"' in template
        assert 'data-testid="import-progress-story-arc-placement-retry-error"' in template
        assert 'data-testid="import-progress-story-arc-placement-retry-success"' in template
        assert "showRetryStoryArcPlacementsAction: function" in controller
        assert "retryStoryArcPlacements: async function" in controller
        assert '"/api/v1/import/" + this.jobId + "/story-arc-placements/retry"' in controller
        assert 'headers: { "X-CSRF-Token": readCsrfTokenFromBody() }' in controller

    async def test_import_footer_pagination_has_delegated_click_handler(self) -> None:
        script = Path("src/pullbox/ui/static/js/pullbox.js").read_text()

        assert "__pbImportFooterPaginationPending" in script
        assert "[data-testid='import-review-pagination'] [data-page-url]" in script
        assert "[data-testid='import-conflicts-pagination'] [data-page-url]" in script
        assert 'htmx.ajax("GET", url' in script
        assert "_scrollImportContentToTop();" in script

    async def test_import_tooltips_dismiss_when_window_or_tab_loses_focus(self) -> None:
        script = Path("src/pullbox/ui/static/js/pullbox.js").read_text()

        assert 'document.addEventListener("visibilitychange", function () {' in script
        assert "if (document.hidden) {" in script
        assert 'window.addEventListener("blur", function () {' in script
        assert "dismissTooltip();" in script

    async def test_import_log_viewer_live_mode_tails_newest_page(self) -> None:
        script = Path("src/pullbox/ui/static/js/pullbox.js").read_text()
        start = script.index("function importJobLogViewerData")
        end = script.index("function importProgressData")
        viewer_controller = script[start:end]

        assert "_shouldFollowLiveTail: function () {" in viewer_controller
        assert "var shouldFollowTail = this._shouldFollowLiveTail();" in viewer_controller
        assert "this.currentPage = this.totalPages;" in viewer_controller
        assert "this.isLive &&" in viewer_controller
        assert "!this.levelFilter &&" in viewer_controller
        assert "!this.searchQuery &&" in viewer_controller

    async def test_import_log_viewer_retains_only_a_bounded_recent_window(self) -> None:
        script = Path("src/pullbox/ui/static/js/pullbox.js").read_text()
        import_log_viewer_template = Path(
            "src/pullbox/ui/templates/components/import_log_viewer.html"
        ).read_text()
        start = script.index("function importJobLogViewerData")
        end = script.index("function importProgressData")
        viewer_controller = script[start:end]

        assert "var _MAX_RETAINED_ENTRIES" in viewer_controller
        assert "_trimRetainedEntries: function () {" in viewer_controller
        assert "this.entries.splice(0, overflow);" in viewer_controller
        assert '"&order=desc"' in viewer_controller
        assert "items.slice().reverse()" in viewer_controller
        assert "while (true)" not in viewer_controller
        assert '" recent of " + this.totalCount + " entries"' in viewer_controller
        assert 'pagination_status_text_expr="footerStatusText"' in import_log_viewer_template

    async def test_shared_log_viewer_keys_namespace_persisted_and_stream_rows(self) -> None:
        template = Path("src/pullbox/ui/templates/components/log_viewer.html").read_text()

        assert ':key="entry.id || idx"' not in template
        assert "'persisted:' + entry.id" in template
        assert "'stream:' + entry._streamToken" in template
        assert "'synthetic:' + idx" in template

    async def test_import_review_shell_reprocesses_htmx_after_morph_swaps(self) -> None:
        script = Path("src/pullbox/ui/static/js/pullbox.js").read_text()
        review_template = Path(
            "src/pullbox/ui/templates/partials/import_step_review.html"
        ).read_text()

        assert 'settledTarget.id === "import-step-review"' in script
        assert 'settledTarget.id === "conflicts-content"' in script
        assert "window.htmx.process(settledTarget);" in script
        assert "reviewData.rehydrateAfterShellSwap();" in script
        assert "currentConflictPanelData: function () {" in script
        assert "await panel.saveAllResolutions();" in script
        assert "function performHtmxSwap(method, url, options)" in script
        assert '"/api/v1/import/" + this.jobId + "/conflicts/resolve-bulk"' in script
        assert 'performHtmxSwap("GET", this.buildRefreshUrl(),' in script
        assert "openConflictView: function () {" not in script
        assert "x-on:import:open-conflicts.window" not in review_template

    async def test_import_review_bulk_selection_uses_canonical_shell_refresh(
        self,
    ) -> None:
        script = Path("src/pullbox/ui/static/js/pullbox.js").read_text()

        assert "function loadImportReviewShell(url)" in script
        assert "applyBulkSelectionUiState" not in script
        assert "this.refreshSeriesReview().catch(function () {" not in script
        assert (
            "await this.refreshSeriesReview();"
            in script[
                script.index("selectAllImportable: async function () {") : script.index(
                    "handleConflictsSaved: function"
                )
            ]
        )
        assert "selectCurrentPageMatched" not in script

    async def test_import_conflict_commit_state_requires_visible_group_match(self) -> None:
        script = Path("src/pullbox/ui/static/js/pullbox.js").read_text()

        assert "currentConflictPageGroupIds: function () {" in script
        assert (
            "var savedGroupIds = normalizeImportReviewSelection(pageState.groupIds || []);"
            in script
        )
        assert "savedGroupIds.length !== currentGroupIds.length" in script
        assert "if (currentGroupIds[i] !== savedGroupIds[i]) {" in script

    async def test_import_collection_renders_standardized_shell(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/import")

        assert response.status_code == 200
        assert 'data-testid="import-page"' in response.text
        assert 'data-testid="import-shell"' in response.text
        assert 'data-testid="import-header"' in response.text
        assert 'data-testid="import-header-gauges-spacer"' in response.text
        assert 'data-testid="import-header-gauges"' not in response.text
        assert 'data-testid="import-tabs"' in response.text
        assert 'data-testid="import-content"' in response.text
        assert 'data-testid="import-footer-dock"' in response.text
        assert 'data-testid="import-collection-footer-dock"' in response.text
        assert "importCollectionFooterData({" in response.text
        assert 'data-testid="import-tab-collection"' in response.text
        assert 'data-testid="import-tab-unmatched"' in response.text
        assert 'data-testid="import-tab-history"' in response.text
        assert 'data-testid="import-collection-page"' in response.text
        assert 'data-testid="import-collection-stepper"' in response.text
        assert 'data-testid="import-collection-body"' in response.text
        assert 'data-testid="import-collection-source-shell"' in response.text
        assert "Scan · match · review · import" in response.text
        assert 'data-testid="import-collection-source-filesystem"' in response.text
        assert 'data-testid="import-collection-source-mylar3"' in response.text
        assert "sourceType === 'filesystem' ? '/imports' : '/imports/mylar.db'" in response.text
        assert 'data-testid="import-collection-source-browse"' in response.text
        assert "Collection imports preserve source files." in response.text
        assert "Files and folders in the selected source stay untouched" in response.text
        assert 'data-testid="import-mylar-path-section"' in response.text
        assert 'data-testid="import-mylar-path-analyze"' in response.text
        assert 'data-testid="import-mylar-path-mapping-row"' in response.text
        assert 'data-testid="import-mylar-path-add"' in response.text
        assert 'data-testid="import-mylar-path-confirm"' in response.text
        assert 'data-testid="import-mylar-path-total"' in response.text
        assert 'data-testid="import-mylar-path-unmapped"' in response.text
        assert 'data-testid="import-mylar-path-invalid"' in response.text
        assert 'data-testid="import-mylar-path-identity-groups"' in response.text
        assert 'data-testid="import-mylar-path-mapping-blockers"' in response.text
        assert 'data-testid="import-mylar-path-mapping-examples"' in response.text
        assert 'data-testid="import-mylar-path-warnings"' in response.text
        assert "Path stored in Mylar" in response.text
        assert "Path visible inside Pullbox" in response.text
        assert 'data-testid="import-file-handling-section"' in response.text
        assert 'data-testid="import-file-handling-managed"' in response.text
        assert 'data-testid="import-file-handling-in-place"' in response.text
        assert "Copy into Pullbox library" in response.text
        assert "Keep files in place" in response.text
        assert "rename, convert, rewrite metadata, or change permissions on them." in response.text
        assert 'data-testid="import-managed-library-root"' in response.text
        assert 'data-testid="import-library-roots-manage"' in response.text
        assert 'data-testid="import-library-roots-refresh"' in response.text
        assert 'href="/settings?tab=media"' in response.text
        assert "Managed destination" in response.text
        assert "Preferred destination for future files" in response.text
        assert (
            "Each existing file remains associated with its containing library root"
            in response.text
        )
        assert 'data-testid="import-collection-layout-section"' in response.text
        assert 'data-testid="import-layout-auto"' in response.text
        assert 'data-testid="import-layout-series-folders"' in response.text
        assert 'data-testid="import-layout-publisher-series"' in response.text
        assert 'data-testid="import-layout-custom"' in response.text
        assert 'data-testid="import-layout-analyze"' in response.text
        assert 'data-testid="import-layout-preview"' in response.text
        assert 'data-testid="import-layout-fallback"' in response.text
        assert "How is this library organized?" in response.text
        assert "Use automatic detection for files that do not fit" in response.text
        assert "Files that do not fit will wait for review" in response.text
        assert 'data-testid="import-future-layout-section"' in response.text
        assert 'data-testid="import-future-layout-toggle"' in response.text
        assert "Use this layout for future files" in response.text
        assert "Existing files won't be renamed" in response.text
        assert "Current library policy" in response.text
        assert "Proposed for new files" in response.text
        assert 'data-testid="import-story-arc-section"' in response.text
        assert 'data-testid="import-story-arc-preview"' in response.text
        assert 'data-testid="import-story-arc-import-toggle"' in response.text
        assert 'data-testid="import-story-arc-materialize-toggle"' in response.text
        assert "Import logical story arcs and memberships" in response.text
        assert "Materialize and synchronize separate arc files" in response.text
        assert "Step 3 remains the final review" in response.text
        assert 'data-testid="file-browser-modal"' in response.text
        assert 'data-testid="import-collection-modal-host"' in response.text

    async def test_import_source_controller_previews_and_submits_frozen_layout(self) -> None:
        script = Path("src/pullbox/ui/static/js/pullbox.js").read_text()
        start = script.index("function importSourceData")
        end = script.index("function importJobLogViewerData", start)
        source_controller = script[start:end]

        assert 'fetch("/api/v1/import/layout-preview"' in source_controller
        assert "new AbortController()" in source_controller
        assert "layoutPreviewRequestId" in source_controller
        assert "sourceLayoutPayload: function" in source_controller
        assert "layoutFallbackToAuto: true" in source_controller
        assert "fallback_to_auto: this.layoutFallbackToAuto" in source_controller
        assert "source_layout: this.sourceLayoutPayload()" in source_controller
        assert 'fileHandlingMode: "managed_copy"' in source_controller
        assert "file_handling_mode: this.fileHandlingMode" in source_controller
        assert "root.is_default_managed_destination" in source_controller
        assert "root.allow_managed_writes" in source_controller
        assert "root.allow_referenced_registrations" in source_controller
        assert 'this.fileHandlingMode === "managed_copy"' in source_controller
        assert 'this.fileHandlingMode === "in_place"' in source_controller
        assert "this.layoutPreview.can_keep_in_place" in source_controller
        assert "futureLayoutRequested: false" in source_controller
        assert "this.layoutPreview.can_apply_future_policy" in source_controller
        assert "future_layout_requested: this.futureLayoutRequested" in source_controller
        assert "future_root_policy: this.futureLayoutRequested" in source_controller
        assert "target_library_root_id: this.targetLibraryRootId" in source_controller
        assert "this.targetLibraryRootId = null;" in source_controller
        assert '"/api/v1/config/library-roots/"' in source_controller
        assert 'fetch("/api/v1/config/library-roots"' in source_controller
        assert "refreshImportLibraryRoots: async function" in source_controller
        assert 'fetch("/api/v1/import/story-arc-preview"' in source_controller
        assert 'fetch("/api/v1/import/mylar-path-preview"' in source_controller
        assert "mylarPathPreviewRequestId" in source_controller
        assert "clearMylarPathPreview: function" in source_controller
        assert "mylarPathMappingChanged: function" in source_controller
        assert "this.mylarPathPreview.can_confirm" in source_controller
        assert "this.mylarPathPreview.requires_confirmation" in source_controller
        assert 'mylar3_path_map: this.sourceType === "mylar3"' in source_controller
        assert 'mylar3_path_map_confirmed: this.sourceType === "mylar3"' in source_controller
        assert "storyArcImportRequested: false" in source_controller
        assert "storyArcMaterializationRequested: false" in source_controller
        assert "story_arc_import_requested: this.storyArcImportRequested" in source_controller
        assert (
            "story_arc_materialization_requested: this.storyArcMaterializationRequested"
            in source_controller
        )

    async def test_split_series_review_requires_future_destination_without_relocation(self) -> None:
        template = Path("src/pullbox/ui/templates/partials/import_step_review.html").read_text()
        script = Path("src/pullbox/ui/static/js/pullbox.js").read_text()
        start = script.index("function importReviewData")
        end = script.index("function importResultsData", start)
        review_controller = script[start:end]

        assert 'data-testid="import-review-split-series"' in template
        assert 'data-testid="import-review-split-series-item"' in template
        assert 'data-testid="import-review-preferred-root"' in template
        assert "Existing files remain in place" in template
        assert "splitSeriesRequiresPreferredRoot" in review_controller
        assert "hasRequiredPreferredRoot" in review_controller
        assert "target_library_root_id: this.preferredRootId" in review_controller

    async def test_import_collection_exposes_stable_step_mounts(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/import")

        assert response.status_code == 200
        assert 'data-testid="import-collection-step-source"' in response.text
        assert 'data-testid="import-collection-step-progress"' in response.text
        assert 'data-testid="import-collection-step-review"' in response.text
        assert 'data-testid="import-collection-step-execute"' in response.text
        assert 'data-testid="import-collection-step-results"' in response.text

    async def test_import_collection_accepts_explicit_resume_query(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(
            f"/import?tab=collection&resume_job_id={job_id}&resume_step=2"
        )

        assert response.status_code == 200
        assert "resumeStep: 2" in response.text
        assert f"resumeJobId: {job_id}" in response.text
        assert "resumeJobStatus: &#34;review&#34;" in response.text

    async def test_import_collection_auto_resumes_active_matching_job(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_progress_job(sec_db, status="matching")

        response = await authenticated_client.get("/import?tab=collection")

        assert response.status_code == 200
        assert "resumeStep: 2" in response.text
        assert f"resumeJobId: {job_id}" in response.text
        assert "resumeJobStatus: &#34;matching&#34;" in response.text

    async def test_import_collection_auto_resumes_review_job_when_no_active_run(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get("/import?tab=collection")

        assert response.status_code == 200
        assert "resumeStep: 3" in response.text
        assert f"resumeJobId: {job_id}" in response.text
        assert "resumeJobStatus: &#34;review&#34;" in response.text

    async def test_import_collection_auto_resumes_active_import_job(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_execute_job(sec_db, status="importing")

        response = await authenticated_client.get("/import?tab=collection")

        assert response.status_code == 200
        assert "resumeStep: 4" in response.text
        assert f"resumeJobId: {job_id}" in response.text
        assert "resumeJobStatus: &#34;importing&#34;" in response.text

    async def test_import_collection_resumes_preparation_stall_from_import_snapshot(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType

        async with sec_db() as session:
            job = ImportJob(
                source_path="/tmp/import-preparation-stall",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.STALLED,
                progress_snapshot={
                    "mode": "import",
                    "phase": "queued",
                    "progress": 0,
                    "message": "Import stalled because the database was busy.",
                },
            )
            session.add(job)
            await session.commit()
            job_id = job.id

        response = await authenticated_client.get(
            f"/import?tab=collection&resume_job_id={job_id}&resume_step=4"
        )

        assert response.status_code == 200
        assert "resumeStep: 4" in response.text
        assert f"resumeJobId: {job_id}" in response.text
        assert "resumeJobStatus: &#34;stalled&#34;" in response.text

    async def test_import_tabs_preserve_active_matching_job_when_returning_to_collection(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        matching_job_id = await _seed_import_progress_job(sec_db, status="matching")

        response = await authenticated_client.get("/import?tab=history")

        assert response.status_code == 200
        assert (
            f'href="/import?tab=collection&amp;resume_job_id={matching_job_id}&amp;resume_step=2"'
            in response.text
        )
        assert (
            f'hx-get="/import?tab=collection&amp;resume_job_id={matching_job_id}&amp;resume_step=2"'
            in response.text
        )

    async def test_import_top_level_tab_switch_returns_content_bundle(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/import?tab=history",
            headers={"HX-Request": "true", "HX-Target": "import-content"},
        )

        assert response.status_code == 200
        assert 'data-testid="import-header"' in response.text
        assert 'data-testid="import-tabs"' in response.text
        assert 'hx-swap-oob="outerHTML"' in response.text
        assert 'data-testid="import-content"' in response.text
        assert 'data-testid="page-footer-dock"' in response.text
        assert 'data-testid="import-history-page"' in response.text
        assert 'data-testid="import-page"' not in response.text

    async def test_import_history_surfaces_resume_and_rollback_controls(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        matching_job_id = await _seed_import_progress_job(sec_db, status="matching")
        review_job_id = await _seed_import_review_job(sec_db)
        paused_job_id = await _seed_import_execute_job(sec_db, status="paused")
        cancelled_job_id = await _seed_import_cancelled_job(sec_db)
        rollback_job_id = await _seed_import_execute_job(sec_db, status="completed")
        rolled_back_job_id = await _seed_import_rolled_back_job(sec_db)

        response = await authenticated_client.get("/import?tab=history")

        assert response.status_code == 200
        assert f'data-testid="import-history-resume-{matching_job_id}"' in response.text
        assert f'data-testid="import-history-resume-{review_job_id}"' in response.text
        assert f'data-testid="import-history-resume-{paused_job_id}"' in response.text
        assert f'data-testid="import-history-rollback-{rollback_job_id}"' in response.text
        assert f'data-testid="import-history-retry-{cancelled_job_id}"' in response.text
        assert f'data-testid="import-history-delete-{cancelled_job_id}"' in response.text
        assert f'data-testid="import-history-retry-{rolled_back_job_id}"' in response.text
        assert f'data-testid="import-history-delete-{rolled_back_job_id}"' in response.text
        assert f'data-testid="import-history-delete-{rollback_job_id}"' not in response.text
        assert f'data-testid="import-history-rollback-{paused_job_id}"' not in response.text
        assert f"resumeActiveImport({matching_job_id}, 2," in response.text
        assert f"resumeActiveImport({review_job_id}, 3," in response.text
        assert f"resumeActiveImport({paused_job_id}, 4," in response.text

    async def test_import_progress_partial_uses_shared_progress_contract(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_progress_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/progress-partial")

        assert response.status_code == 200
        assert 'data-testid="import-collection-progress"' in response.text
        assert "Scan run" not in response.text
        assert "app-progress-track" in response.text
        assert "app-progress-fill" in response.text
        assert "app-progress-value" in response.text
        assert 'data-testid="import-progress-phase-detail"' in response.text
        assert 'data-testid="import-progress-eta"' in response.text
        assert 'data-testid="import-progress-current-file"' in response.text
        assert 'data-testid="import-progress-current-file-name"' in response.text
        assert 'data-testid="import-progress-current-file-stage"' in response.text
        assert 'data-testid="import-progress-current-file-detail"' in response.text
        assert "Current item" in response.text
        assert 'data-testid="import-progress-recent-log"' not in response.text
        assert 'data-log-viewer-contract="v1"' not in response.text
        assert 'data-testid="import-progress-log-download"' in response.text
        assert f'href="/api/v1/import/{job_id}/logs/download"' in response.text

    async def test_import_progress_partial_hydrates_review_snapshot(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/progress-partial")

        assert response.status_code == 200
        assert 'x-init="init()"' in response.text
        assert "&#34;status&#34;" in response.text
        assert "&#34;review&#34;" in response.text
        assert "&#34;review_summary&#34;" in response.text
        assert "&#34;recent_logs&#34;" in response.text
        assert 'data-testid="import-progress-phase-label"' in response.text
        assert 'data-testid="import-progress-continue"' in response.text
        action_bar_index = response.text.index('data-testid="import-progress-action-bar"')
        progress_card_index = response.text.index('class="section-card"')
        assert action_bar_index < progress_card_index

    async def test_import_progress_partial_restores_collection_footer_dock(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/progress-partial")

        assert response.status_code == 200
        assert (
            'id="page-footer-dock" data-testid="page-footer-dock" hx-swap-oob="innerHTML"'
        ) in response.text
        assert 'data-testid="import-collection-footer-dock"' in response.text
        assert 'data-testid="page-dock-pagination"' not in response.text
        assert 'data-testid="import-review-pagination"' not in response.text
        assert 'data-testid="import-conflicts-pagination"' not in response.text

    async def test_import_execute_progress_uses_top_action_bar_contract(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/progress-partial?next_step=5")

        assert response.status_code == 200
        action_bar_index = response.text.index('data-testid="import-progress-action-bar"')
        progress_card_index = response.text.index('class="section-card"')
        assert action_bar_index < progress_card_index

    async def test_import_execute_progress_holds_on_completion(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_execute_job(sec_db, status="completed")

        response = await authenticated_client.get(
            f"/import/{job_id}/progress-partial?next_step=5&mode=import"
        )

        assert response.status_code == 200
        assert 'data-testid="import-progress-continue-results"' in response.text
        assert "Continue to results" in response.text

    async def test_rollback_progress_holds_on_completion(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_execute_job(sec_db, status="rolled_back")

        response = await authenticated_client.get(
            f"/import/{job_id}/progress-partial?next_step=3&mode=rollback"
        )

        assert response.status_code == 200
        assert 'data-testid="import-progress-view-history"' in response.text
        assert 'data-testid="import-progress-log-download"' in response.text
        assert "Rollback log" not in response.text

    async def test_import_results_partial_restores_collection_footer_dock(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_execute_job(sec_db, status="completed")

        response = await authenticated_client.get(f"/import/{job_id}/results-partial")

        assert response.status_code == 200
        assert (
            'id="page-footer-dock" data-testid="page-footer-dock" hx-swap-oob="innerHTML"'
        ) in response.text
        assert 'data-testid="import-collection-footer-dock"' in response.text
        assert 'data-testid="page-dock-pagination"' not in response.text
        assert 'data-testid="import-review-pagination"' not in response.text
        assert 'data-testid="import-conflicts-pagination"' not in response.text

    async def test_import_progress_state_returns_durable_snapshot(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_progress_job(sec_db)
        from pullbox.models.import_job import ImportJob

        async with sec_db() as session:
            job = await session.get(ImportJob, job_id)
            assert job is not None
            job.progress_snapshot = {
                "snapshot_version": 2,
                "job_id": job_id,
                "status": "scanning",
                "mode": "scan",
                "phase": "scanning",
                "progress": 27,
                "message": "Discovered 3 series · 12/42 files processed.",
                "current_series": "Batman",
                "current_series_name": "Batman",
                "estimated_seconds_remaining": 91,
                "progress_revision": 4,
            }
            job.progress_revision = 4
            await session.commit()

        response = await authenticated_client.get(f"/import/{job_id}/progress-state")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "scanning"
        assert payload["phase"] == "scanning"
        assert payload["progress"] == 27
        assert payload["mode"] == "scan"
        assert payload["progress_revision"] == 4
        assert payload["message"] == "Discovered 3 series · 12/42 files processed."
        assert payload["current_series"] == "Batman"
        assert payload["estimated_seconds_remaining"] == 91
        assert payload["scan_total_files"] == 42
        assert payload["review_summary"]["files_total"] == 42
        assert "recent_logs" in payload

    async def test_import_progress_state_prefers_cancelled_job_state_over_stale_live_event(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from datetime import UTC, datetime

        from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
        from pullbox.schemas.import_job import ImportProgressEvent
        from pullbox.tasks.import_task import remove_progress_queue, set_latest_progress_event

        async with sec_db() as session:
            job = ImportJob(
                source_path="/tmp/import-progress-cancelled",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.CANCELLED,
                import_started_at=datetime.now(UTC),
                progress_snapshot={
                    "status": "paused",
                    "phase": "importing",
                    "progress": 76,
                    "message": "Processed 19/25 review groups",
                },
                error_message="Import cancelled by user.",
            )
            session.add(job)
            await session.commit()
            job_id = job.id

        set_latest_progress_event(
            ImportProgressEvent(
                job_id=job_id,
                status=ImportJobStatus.IMPORTING,
                phase="importing",
                progress=76,
                message="Processed 19/25 review groups",
                current_series="Stale series",
            )
        )

        try:
            response = await authenticated_client.get(f"/import/{job_id}/progress-state")
        finally:
            remove_progress_queue(job_id)

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "cancelled"
        assert payload["phase"] == "done"
        assert payload["progress"] == 100
        assert payload["message"] == "Import cancelled by user."

    async def test_import_progress_state_ignores_stale_live_event_cache(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportJob, ImportJobStatus
        from pullbox.schemas.import_job import ImportProgressEvent
        from pullbox.tasks.import_task import remove_progress_queue, set_latest_progress_event

        job_id = await _seed_import_progress_job(sec_db)

        async with sec_db() as session:
            job = await session.get(ImportJob, job_id)
            assert job is not None
            job.progress_snapshot = {
                "snapshot_version": 2,
                "job_id": job_id,
                "status": "scanning",
                "mode": "scan",
                "phase": "matching",
                "progress": 63,
                "message": "Matching against ComicVine...",
                "progress_revision": 9,
            }
            job.progress_revision = 9
            await session.commit()

        set_latest_progress_event(
            ImportProgressEvent(
                job_id=job_id,
                status=ImportJobStatus.SCANNING,
                mode="scan",
                phase="scanning",
                progress=12,
                message="Stale cache event",
                progress_revision=3,
            )
        )

        try:
            response = await authenticated_client.get(f"/import/{job_id}/progress-state")
        finally:
            remove_progress_queue(job_id)

        assert response.status_code == 200
        payload = response.json()
        assert payload["phase"] == "matching"
        assert payload["progress"] == 63
        assert payload["message"] == "Matching against ComicVine..."
        assert payload["progress_revision"] == 9

    async def test_import_progress_state_review_revision_beats_live_heartbeat(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import AsyncMock, patch

        from pullbox.models.import_job import ImportJob, ImportJobStatus, ImportSourceType
        from pullbox.schemas.import_job import ImportProgressEvent
        from pullbox.tasks.import_task import _publish_progress_event, remove_progress_queue

        async with sec_db() as session:
            job = ImportJob(
                source_path="/tmp/import-progress-review",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.REVIEW,
                progress_revision=13,
                progress_snapshot={
                    "snapshot_version": 2,
                    "status": "review",
                    "mode": "scan",
                    "phase": "review",
                    "progress": 100,
                    "message": "Ready for review",
                    "progress_revision": 13,
                },
            )
            session.add(job)
            await session.commit()
            job_id = job.id

        try:
            with patch("pullbox.tasks.import_task.publish", new_callable=AsyncMock):
                await _publish_progress_event(
                    ImportProgressEvent(
                        job_id=job_id,
                        status=ImportJobStatus.FILE_MATCHING,
                        mode="scan",
                        phase="file_matching",
                        progress=74,
                        message="Still loading issue targets...",
                        progress_revision=27,
                        ephemeral_progress=True,
                        current_item_kind="series",
                        current_item_stage="issue_targets",
                    )
                )

            response = await authenticated_client.get(f"/import/{job_id}/progress-state")
        finally:
            remove_progress_queue(job_id)

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "review"
        assert payload["phase"] == "review"
        assert payload["progress"] == 100
        assert payload["message"] == "Ready for review"
        assert payload["progress_revision"] == 28

    async def test_import_review_partial_renders_series_no_match_diagnostics(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 1)
            assert series is not None
            series.cv_id = 9001
            series.cv_title = "Review Series 1"
            series.cv_year = 2021
            series.cv_url = "https://comicvine.gamespot.com/review-series-1/4050-9001/"
            series.cv_publisher = "Test Publisher"
            series.cv_issue_count = 18
            series.cv_match_method = "exact_title_year"
            await session.commit()

        response = await authenticated_client.get(f"/import/{job_id}/review-partial")

        assert response.status_code == 200
        assert "Import settings" not in response.text
        assert 'data-testid="import-review-library-root-display"' not in response.text
        assert "Search on add" not in response.text
        assert 'data-testid="import-review-sort-found_series"' in response.text
        assert 'data-testid="import-review-sort-status"' in response.text
        assert '<th class="import-review-cv-year-header w-20">' in response.text
        assert 'data-testid="import-review-sort-cv_year"' in response.text
        assert 'data-testid="import-review-sort-cv_id"' in response.text
        assert 'href="https://comicvine.gamespot.com/review-series-1/4050-9001/"' in response.text
        assert 'data-testid="import-review-cv-id-link"' in response.text
        assert 'class="downloads-sort-btn' in response.text
        assert 'class="downloads-action-btn is-warn"' in response.text
        assert 'data-tip="Search ComicVine"' in response.text
        assert 'data-tip="Why no match"' in response.text
        assert "return openImportCvSearchModal({ jobId: " in response.text
        assert "In Library" in response.text
        assert 'data-testid="import-review-series-details-action"' not in response.text
        assert 'x-init="init()"' in response.text
        assert "data-import-review-selection-summary" in response.text
        assert "data-import-review-import-button" in response.text
        assert "data-import-review-import-label" in response.text
        assert "data-import-review-toolbar-selection-summary" in response.text
        assert "conflictSeriesCount:" in response.text
        assert "data-import-review-conflict-count" in response.text
        assert 'x-text="conflictSeriesCount"' in response.text
        assert (
            'x-on:import:conflict-visibility.window="visibleFileConflictGroupCount = '
            'Number(($event.detail && $event.detail.visibleFileConflictGroupCount) || 0)"'
            in response.text
        )
        assert (
            'x-on:import:conflicts-saved.window="handleConflictsSaved($event.detail)"'
            in response.text
        )
        assert (
            'x-on:import:conflicts-reset.window="handleConflictsReset($event.detail)"'
            in response.text
        )
        assert "In library" not in response.text
        assert "Test Publisher" in response.text
        assert "Existing library series matched by ComicVine ID." in response.text
        assert "87%" in response.text
        assert "Exact match" in response.text
        assert "18 issues in series" in response.text
        assert "exact_title_year" not in response.text
        assert "h-1 w-10 overflow-hidden" not in response.text
        assert ':checked="isSelected(1)"' not in response.text
        assert '@change="toggleSelection(1, $event.target.checked)"' in response.text
        assert 'data-import-review-selectable="1"' in response.text
        assert ">All<" in response.text
        assert "Select all importable" in response.text
        assert "2 items selected for import" in response.text
        assert "Import 2 items" in response.text
        assert "2 of 8 selected" in response.text
        assert 'data-overall-total="8"' in response.text
        assert "Status</span>" in response.text
        assert 'data-testid="import-review-status-header-row"' in response.text
        assert 'data-testid="import-review-status-tabs-row"' in response.text
        assert 'aria-label="Select Review Series 9 for import"' not in response.text
        assert 'aria-label="Select Review Series 11 for import"' not in response.text
        assert 'data-testid="import-review-why-action"' in response.text
        assert 'data-testid="import-review-matched-why-action"' in response.text
        assert 'data-testid="import-review-matched-diagnostics"' in response.text
        assert (
            'data-testid="import-review-matched-detail-grid" '
            'class="grid items-start gap-5 sm:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]"'
        ) in response.text
        assert (
            'data-testid="import-review-duplicate-detail-grid" '
            'class="grid items-start gap-5 sm:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]"'
        ) in response.text
        assert (
            'data-testid="import-review-no-match-detail-grid" '
            'class="grid items-start gap-5 sm:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]"'
        ) in response.text
        file_breakdown_start = response.text.index(
            'data-testid="import-review-matched-file-breakdown"'
        )
        file_breakdown_header = response.text[
            file_breakdown_start : response.text.index(
                '<div class="mt-2 overflow-hidden rounded-lg border border-pb-border">',
                file_breakdown_start,
            )
        ]
        assert "btn-ghost btn-sm" not in file_breakdown_header
        assert "Open series details" not in file_breakdown_header
        assert 'data-testid="import-review-no-match-diagnostics"' in response.text
        assert "ComicVine candidates" in response.text
        assert 'data-testid="import-review-candidate-row"' in response.text
        assert "Below 70% threshold" in response.text
        assert "rounded-xl border border-pb-border bg-pb-card/70" not in response.text
        assert 'data-testid="import-review-tab-files"' not in response.text
        assert 'data-testid="import-review-files-content"' not in response.text
        assert 'data-testid="import-review-tab-series"' in response.text
        assert 'class="chip-btn chip-btn-sm chip-btn-selected"' in response.text
        assert "currentView === 'series'" in response.text
        assert 'class="chip-btn chip-btn-sm chip-btn-neutral"' in response.text
        assert "'chip-btn-warning': currentView === 'conflicts'" in response.text
        assert 'id="import-step-review-shell"' in response.text
        assert "reviewToken:" in response.text
        assert 'hx-target="#import-step-review-shell"' in response.text
        assert 'hx-swap="outerHTML"' in response.text
        assert "data-import-review-toolbar-selection-summary" in response.text
        assert 'data-testid="import-review-save-conflict-choices"' not in response.text
        assert 'data-testid="import-review-reset-conflict-choices"' not in response.text
        assert '@click="saveConflictChoices()"' not in response.text
        assert '@click="resetConflictChoices()"' not in response.text
        assert "Save conflict choices" not in response.text
        action_bar_index = response.text.index('data-testid="import-review-action-bar"')
        status_bar_index = response.text.index('data-testid="import-review-series-filters"')
        assert action_bar_index < status_bar_index

    async def test_import_review_partial_explains_selected_layout_review(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 11)
            assert series is not None
            series.diagnostics = {
                "kind": "source_layout_review",
                "reason": "selected_layout_no_match",
                "rejection_reason": (
                    "This file does not fit the selected source layout. "
                    "Review its series before importing."
                ),
                "source_layout_review_files": 1,
            }
            await session.commit()

        response = await authenticated_client.get(f"/import/{job_id}/review-partial")

        assert response.status_code == 200
        assert 'data-testid="import-review-layout-review"' in response.text
        assert "Layout review" in response.text
        assert "1 file does not fit the selected source layout" in response.text
        assert "will not be matched automatically" in response.text

    async def test_import_review_partial_explains_incompatible_mylar_path(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 11)
            assert series is not None
            series.diagnostics = {
                "kind": "mylar3_path_incompatible",
                "reason": "mapped_path_missing",
                "rejection_reason": ("The mapped Mylar comic folder is not available to Pullbox."),
                "mylar3_path": {
                    "status": "missing",
                    "mapping_applied": True,
                },
            }
            await session.commit()

        response = await authenticated_client.get(f"/import/{job_id}/review-partial")

        assert response.status_code == 200
        assert 'data-testid="import-review-mylar-path-review"' in response.text
        assert "Mylar path review" in response.text
        assert "The mapped Mylar comic folder is not available to Pullbox." in response.text
        assert "Correct the Mylar path mapping and retry this import." in response.text

    async def test_import_review_repeats_confirmed_mylar_mapping_snapshot(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportJob, ImportSourceType

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            job = await session.get(ImportJob, job_id)
            assert job is not None
            job.source_type = ImportSourceType.MYLAR3
            job.mylar3_path_map = {
                "/books/current": "/comics/current",
                "/books/archive": "/comics/archive",
            }
            job.mylar3_path_map_confirmed = True
            await session.commit()

        response = await authenticated_client.get(f"/import/{job_id}/review-partial")

        assert response.status_code == 200
        assert 'data-testid="import-review-mylar-path-snapshot"' in response.text
        assert "Confirmed Mylar path mapping" in response.text
        assert "/books/current" in response.text
        assert "/comics/current" in response.text
        assert "/books/archive" in response.text
        assert "/comics/archive" in response.text

    async def test_import_review_partial_renders_matched_file_target_tables(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            matched_series = (
                await session.execute(
                    select(ImportedSeries).where(
                        ImportedSeries.import_job_id == job_id,
                        ImportedSeries.raw_series_name == "Review Series 1",
                    )
                )
            ).scalar_one()
            duplicate_series = (
                await session.execute(
                    select(ImportedSeries).where(
                        ImportedSeries.import_job_id == job_id,
                        ImportedSeries.raw_series_name == "Review Series 9",
                    )
                )
            ).scalar_one()
            matched_file = (
                await session.execute(
                    select(ImportedFile)
                    .where(
                        ImportedFile.import_series_id == matched_series.id,
                        ImportedFile.status == ImportedFileStatus.MATCHED,
                    )
                    .order_by(ImportedFile.id.asc())
                    .limit(1)
                )
            ).scalar_one()
            duplicate_file = (
                await session.execute(
                    select(ImportedFile)
                    .where(
                        ImportedFile.import_series_id == duplicate_series.id,
                        ImportedFile.status == ImportedFileStatus.MATCHED,
                    )
                    .order_by(ImportedFile.id.asc())
                    .limit(1)
                )
            ).scalar_one()

            matched_file.matched_issue_cv_id = 81001
            matched_file.parsed_issue_number = 1.0
            matched_file.diagnostics = {"target_issue_number": 4.0}
            duplicate_file.matched_issue_cv_id = 89001
            duplicate_file.parsed_issue_number = 1.0
            await session.commit()

        response = await authenticated_client.get(f"/import/{job_id}/review-partial")

        assert response.status_code == 200
        assert 'data-testid="import-review-matched-file-targets"' in response.text
        assert "Matched files" in response.text
        assert "Review Series 1 #1.cbz" in response.text
        assert "Review Series 9 #1.cbz" in response.text
        matched_file_row_start = response.text.index("Review Series 1 #1.cbz")
        matched_file_row = response.text[
            matched_file_row_start : response.text.index("</tr>", matched_file_row_start)
        ]
        duplicate_file_row_start = response.text.index("Review Series 9 #1.cbz")
        duplicate_file_row = response.text[
            duplicate_file_row_start : response.text.index("</tr>", duplicate_file_row_start)
        ]
        assert 'class="pill pill-muted">1.0 KB</span>' in matched_file_row
        assert 'class="pill pill-muted">25.0 KB</span>' in duplicate_file_row
        assert 'data-testid="import-review-file-cv-issue-link"' in response.text
        assert 'href="https://comicvine.gamespot.com/issue/4000-81001/"' in response.text
        assert 'href="https://comicvine.gamespot.com/issue/4000-89001/"' in response.text
        assert re.search(r">\s*#4\s*<", response.text)
        assert re.search(r">\s*#1\s*<", response.text)

    async def test_import_review_needs_series_dropdown_renders_file_details(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedFile, ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            needs_series = (
                await session.execute(
                    select(ImportedSeries).where(
                        ImportedSeries.import_job_id == job_id,
                        ImportedSeries.raw_series_name == "Review Series 11",
                    )
                )
            ).scalar_one()
            first_file = (
                await session.execute(
                    select(ImportedFile)
                    .where(ImportedFile.import_series_id == needs_series.id)
                    .order_by(ImportedFile.id.asc())
                    .limit(1)
                )
            ).scalar_one()
            first_file.has_comicinfo = True
            first_file.diagnostics = {
                "source_metadata": {
                    "filename_parse": {
                        "series_name": "Parsed Series 11",
                        "issue_number": 1.0,
                        "year": 2031,
                    },
                    "comicinfo": {
                        "series": "Embedded Series 11",
                        "number": "1",
                        "year": "2031",
                        "title": "Embedded Title",
                        "web": "https://comicvine.gamespot.com/example/4000-111111/",
                    },
                }
            }
            await session.commit()

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=needs_series"
        )

        assert response.status_code == 200
        assert 'data-testid="import-review-series-file-details"' in response.text
        assert 'data-testid="import-review-series-file-group-no_match"' in response.text
        assert "Source files" in response.text
        assert "Review Series 11 #1.cbz" in response.text
        assert "No Match" in response.text
        assert "Files that stayed unmatched or were blocked for manual review." in response.text
        assert "Files tied to this unresolved series row" not in response.text
        assert "/tmp/review-11/file-1.cbz" not in response.text
        assert "Parsed From File Name" not in response.text
        assert "Parsed Series 11" not in response.text
        assert "Embedded ComicInfo.xml" not in response.text
        assert "Embedded Series 11" not in response.text
        assert "https://comicvine.gamespot.com/example/4000-111111/" not in response.text

    async def test_import_review_series_status_views_keep_connected_table_contract(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        for status_filter in ("", "matched", "no_match", "duplicate"):
            query = f"?status={status_filter}" if status_filter else ""
            response = await authenticated_client.get(f"/import/{job_id}/review-partial{query}")

            assert response.status_code == 200
            status_bar = re.search(
                r'data-testid="import-review-series-filters"[^>]*class="([^"]+)"',
                response.text,
            )
            assert status_bar is not None
            assert "rounded-xl" not in status_bar.group(1)
            assert "border-pb-border" not in status_bar.group(1)
            assert "bg-pb-card/40" not in status_bar.group(1)

            table_shell = re.search(
                r'<div class="([^"]*downloads-table-wrap[^"]*)">',
                response.text,
            )
            assert table_shell is not None
            assert "is-clipped" in table_shell.group(1)
            table_shell_index = response.text.index('class="downloads-table-wrap is-clipped"')
            toolbar_index = response.text.index('class="downloads-history-toolbar"')
            table_index = response.text.index('class="downloads-table min-w-[1040px] text-sm"')
            assert table_shell_index < toolbar_index < table_index
            assert 'data-testid="import-collection-conflicts"' not in response.text
            assert "Loading conflicts..." not in response.text

    async def test_import_review_conflicts_filter_uses_series_count(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/review-partial")

        assert response.status_code == 200
        conflict_filter = re.search(
            r'data-testid="import-review-open-conflicts-filter"[\s\S]*?</button>',
            response.text,
        )
        assert conflict_filter is not None
        assert ">Conflicts<" in conflict_filter.group(0)
        assert ">2</span>" in conflict_filter.group(0)
        assert ">4</span>" not in conflict_filter.group(0)

    async def test_import_review_conflicts_view_expands_file_keep_choices_by_default(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=conflicts"
        )

        assert response.status_code == 200
        assert 'data-testid="import-review-save-conflict-choices"' in response.text
        assert ':disabled="!hasVisibleFileConflictGroups()"' in response.text
        assert 'x-data="{ detailsOpen: true }"' in response.text
        assert 'data-testid="import-conflict-choice-row"' not in response.text
        assert 'data-testid="import-series-conflict-candidate-radio"' not in response.text
        assert "Choose one file to keep" in response.text
        assert 'type="radio"' in response.text
        detail_row_index = response.text.index('data-testid="import-conflict-detail-row"')
        radio_index = response.text.index('type="radio"', detail_row_index)
        assert detail_row_index < radio_index

    async def test_import_review_conflicts_view_hides_file_choice_toolbar_for_series_conflicts(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import (
            ImportedFile,
            ImportedFileStatus,
            ImportedSeries,
            ImportSeriesStatus,
        )

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = (
                (
                    await session.execute(
                        select(ImportedSeries)
                        .where(ImportedSeries.import_job_id == job_id)
                        .order_by(ImportedSeries.id.asc())
                    )
                )
                .scalars()
                .first()
            )
            assert series is not None
            series.status = ImportSeriesStatus.NO_MATCH
            series.cv_id = None
            series.cv_title = None
            series.cv_match_score = None
            series.diagnostics = {
                "kind": "series_conflict",
                "reason": "ambiguous_candidates",
                "normalized_query": "review series 1",
                "selected_candidate": {
                    "title": "Review Series One",
                    "year": 2021,
                    "score_pct": 98,
                },
                "competing_candidate": {
                    "title": "Review Series",
                    "year": 2020,
                    "score_pct": 92,
                },
                "top_candidates": [
                    {
                        "title": "Review Series One",
                        "year": 2021,
                        "publisher": "Candidate Publisher",
                        "score_pct": 98,
                        "rejection_reasons": ["Highest score"],
                    },
                    {
                        "title": "Review Series",
                        "year": 2020,
                        "publisher": "Candidate Publisher",
                        "score_pct": 92,
                        "rejection_reasons": ["Ambiguous near match"],
                    },
                ],
            }

            conflict_files = (
                (
                    await session.execute(
                        select(ImportedFile).where(
                            ImportedFile.import_job_id == job_id,
                            ImportedFile.status == ImportedFileStatus.CONFLICT,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for imp_file in conflict_files:
                imp_file.status = ImportedFileStatus.MATCHED
                imp_file.conflict_group_id = None
                imp_file.is_preferred = False
            conflict_series = (
                (
                    await session.execute(
                        select(ImportedSeries).where(
                            ImportedSeries.import_job_id == job_id,
                            ImportedSeries.files_conflict > 0,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for item in conflict_series:
                item.files_conflict = 0
            await session.commit()

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=conflicts"
        )

        assert response.status_code == 200
        assert "Series match conflict" in response.text
        assert 'class="downloads-action-btn"' in response.text
        assert "Review ComicVine match" in response.text
        assert "Conflicting candidates" in response.text
        assert 'data-testid="import-review-save-conflict-choices"' not in response.text
        assert 'data-testid="import-review-reset-conflict-choices"' not in response.text
        assert "Save conflict choices" not in response.text
        assert 'type="radio"' not in response.text

    async def test_import_review_partial_uses_shared_pagination_contract(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_large_conflict_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/review-partial?page=2")

        assert response.status_code == 200
        assert 'name="review_page" value="2"' in response.text
        assert (
            'id="page-footer-dock" data-testid="page-footer-dock" hx-swap-oob="innerHTML"'
        ) in response.text
        assert 'data-testid="import-review-pagination"' in response.text
        assert 'data-testid="page-dock-pagination"' in response.text
        assert (
            f'hx-get="/import/{job_id}/review-partial?sort=confidence&amp;page=1"' in response.text
        )
        assert 'hx-push-url="false"' in response.text
        assert "per page" in response.text
        assert ">25<" in response.text
        table_shell_index = response.text.index('class="downloads-table-wrap is-clipped"')
        footer_index = response.text.index('data-testid="page-footer-dock"')
        assert table_shell_index < footer_index
        assert (
            'data-testid="import-review-pagination"'
            not in response.text[table_shell_index:footer_index]
        )

    async def test_import_review_partial_renders_series_candidate_conflict(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedSeries, ImportSeriesStatus

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 1)
            assert series is not None
            series.status = ImportSeriesStatus.NO_MATCH
            series.cv_id = None
            series.cv_title = None
            series.cv_year = None
            series.cv_publisher = None
            series.cv_issue_count = None
            series.cv_match_score = None
            series.cv_match_method = None
            series.diagnostics = {
                "kind": "series_conflict",
                "reason": "ambiguous_candidates",
                "normalized_query": "chicken devil",
                "raw_year": 2022,
                "threshold": 0.7,
                "selected_candidate": {
                    "title": "Chicken Devils",
                    "year": 2022,
                },
                "competing_candidate": {
                    "title": "Chicken Devil",
                    "year": 2021,
                },
                "top_candidates": [
                    {
                        "title": "Chicken Devils",
                        "year": 2022,
                        "publisher": "AfterShock Comics",
                        "issue_count": 4,
                        "score_pct": 97,
                        "rejection_reasons": ["Pluralized title outranked an exact-title near-tie"],
                    },
                    {
                        "title": "Chicken Devil",
                        "year": 2021,
                        "publisher": "AfterShock Comics",
                        "issue_count": 4,
                        "score_pct": 90,
                        "rejection_reasons": [
                            "Exact title candidate stayed within the ambiguity margin"
                        ],
                    },
                ],
            }
            await session.commit()

        response = await authenticated_client.get(f"/import/{job_id}/review-partial")

        assert response.status_code == 200
        assert ">Conflict</div>" in response.text
        assert "2 strong ComicVine candidates need review." in response.text
        assert 'data-tip="Review conflict"' in response.text
        assert "Conflicting ComicVine candidates" in response.text
        assert "Multiple strong ComicVine candidates conflicted on the best match" in response.text
        assert "return openImportCvSearchModal({ jobId: " in response.text
        assert "Chicken Devils" in response.text
        assert "Chicken Devil" in response.text
        conflict_filter = re.search(
            r'data-testid="import-review-open-conflicts-filter"[\s\S]*?</button>',
            response.text,
        )
        assert conflict_filter is not None
        assert ">3</span>" in conflict_filter.group(0)

    async def test_import_series_details_partial_renders_grouped_diagnostics(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/series/9/details-partial")

        assert response.status_code == 200
        assert 'data-testid="import-series-details-modal"' in response.text
        assert "max-h-[calc(100vh-5rem)]" in response.text
        assert 'class="modal-body min-h-0"' in response.text
        assert "Series details" in response.text
        assert "All the files tied to this series for review." in response.text
        assert "Importable files" in response.text
        assert "Already Owned" in response.text
        assert "Extra Incoming Files" in response.text
        assert "No Match" in response.text
        assert (
            "No import action is available for this file from the Series view." not in response.text
        )
        assert 'data-testid="import-series-details-group-' in response.text
        assert "rounded-xl border border-pb-border bg-pb-card/70" not in response.text
        assert 'data-testid="import-series-details-select-all-duplicate"' in response.text
        assert 'data-testid="import-series-details-deselect-all-duplicate"' in response.text
        assert 'data-testid="import-file-assign-action"' not in response.text
        assert 'data-testid="import-file-reassign-action"' not in response.text
        assert 'data-testid="import-file-repair-action"' not in response.text

    async def test_import_series_details_partial_links_conflicts_without_keep_controls(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/series/7/details-partial")

        assert response.status_code == 200
        assert "Conflicts" in response.text
        assert "Use Conflicts tab" in response.text
        assert 'data-testid="import-series-details-open-conflicts"' not in response.text

    async def test_import_series_details_partial_shows_pending_files_for_no_match_series(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id, series_id = await _seed_import_pending_no_match_series_job(sec_db)

        response = await authenticated_client.get(
            f"/import/{job_id}/series/{series_id}/details-partial"
        )

        assert response.status_code == 200
        assert 'data-testid="import-series-details-group-no_match"' in response.text
        assert "Negation 02 (CrossGen 2003).cbz" in response.text
        assert "Negation 04 (CrossGen 2004).cbz" in response.text
        assert "Diagnostics only" in response.text
        assert 'type="radio"' not in response.text

    async def test_import_review_partial_omits_removed_import_settings_card(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)
        response = await authenticated_client.get(f"/import/{job_id}/review-partial")

        assert response.status_code == 200
        assert "Import settings" not in response.text
        assert "Update embedded ComicInfo.xml from matched issue" not in response.text
        assert "Ingest behavior now follows the global Media settings" not in response.text
        assert 'data-testid="import-review-update-comicinfo-toggle"' not in response.text

    async def test_import_series_details_partial_renders_metadata_conflict_diagnostics(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import (
            ImportedFile,
            ImportedFileStatus,
            ImportedSeries,
            ImportSeriesStatus,
        )

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 1)
            assert series is not None
            series.status = ImportSeriesStatus.NO_MATCH
            series.diagnostics = {
                "kind": "series_no_match",
                "reason": "file_metadata_conflict",
                "normalized_query": "chicken devil",
                "raw_year": 2022,
                "threshold": 0.7,
                "top_candidates": [
                    {
                        "title": "Chicken Devils",
                        "year": 2022,
                        "publisher": "Aftershock Comics",
                        "issue_count": 4,
                        "score_pct": 97,
                        "rejection_reasons": [
                            (
                                "ComicInfo issue ID points to 'Chicken Devils', "
                                "but the source series title is 'Chicken Devil'."
                            )
                        ],
                    }
                ],
            }
            file_row = (
                (
                    await session.execute(
                        select(ImportedFile)
                        .where(ImportedFile.import_series_id == series.id)
                        .order_by(ImportedFile.id.asc())
                    )
                )
                .scalars()
                .first()
            )
            assert file_row is not None
            file_row.status = ImportedFileStatus.NO_MATCH
            file_row.parsed_series = "Chicken Devil"
            file_row.parsed_year = 2022
            file_row.has_comicinfo = True
            file_row.diagnostics = {
                "kind": "metadata_conflict",
                "rejection_reason": (
                    "ComicInfo issue ID points to 'Chicken Devils', "
                    "but the source series title is 'Chicken Devil'."
                ),
                "source_series": "Chicken Devil",
                "target_series": "Chicken Devils",
                "target_series_year": 2022,
                "series_similarity": 0.9741,
                "series_match_type": "fuzzy",
                "comicinfo": {
                    "series": "Chicken Devil",
                    "number": "4",
                    "year": 2022,
                    "title": "The Chicken is in the Details",
                    "web": "https://comicvine.gamespot.com/chicken-devil-4-the-chicken-is-in-the-details/4000-905404/",
                },
            }
            await session.commit()

        response = await authenticated_client.get(f"/import/{job_id}/series/1/details-partial")

        assert response.status_code == 200
        assert 'data-testid="import-series-details-metadata-conflict"' in response.text
        assert "ComicInfo.xml" in response.text
        assert (
            "The file name and embedded ComicInfo.xml point to different series." in response.text
        )
        assert "Review this file manually to verify its provenance." in response.text
        assert "Parsed From File Name" in response.text
        assert "Embedded ComicInfo.xml" in response.text
        assert "Chicken Devils" in response.text
        assert (
            "https://comicvine.gamespot.com/chicken-devil-4-the-chicken-is-in-the-details/4000-905404/"
            in response.text
        )

    async def test_import_series_details_partial_duplicate_series_shows_selection_controls(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/series/9/details-partial")

        assert response.status_code == 200
        assert 'data-testid="import-series-details-select-all-duplicate"' in response.text
        assert 'data-testid="import-series-details-deselect-all-duplicate"' in response.text
        assert 'data-testid="import-series-details-duplicate-file-select"' in response.text
        assert "Import this matched file" in response.text
        assert "importable selected" in response.text

    async def test_import_series_details_partial_duplicate_copy_rows_are_read_only(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from sqlalchemy import select

        from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 9)
            assert series is not None
            series.files_duplicate = 1
            files = list(
                (
                    await session.execute(
                        select(ImportedFile)
                        .where(ImportedFile.import_series_id == series.id)
                        .order_by(ImportedFile.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            files[2].status = ImportedFileStatus.DUPLICATE_FILE
            files[2].include_in_import = False
            files[2].duplicate_group_id = 77
            files[2].duplicate_of_file_id = files[0].id
            files[2].diagnostics = {
                "kind": "duplicate_copy",
                "duplicate_group_id": 77,
                "duplicate_of_file_id": files[0].id,
                "representative_file_name": files[0].file_name,
                "duplicate_reason": "exact_duplicate",
                "target_state": "wanted",
            }
            await session.commit()

        response = await authenticated_client.get(f"/import/{job_id}/series/9/details-partial")

        assert response.status_code == 200
        assert "Extra Incoming Files" in response.text
        assert (
            "Excluded because another incoming file for this same issue was kept" in response.text
        )
        assert "Kept incoming file Review Series 9 #1.cbz" in response.text
        assert "/tmp/review-9/file-3.cbz" in response.text
        assert 'data-testid="import-file-assign-action"' not in response.text
        assert 'data-testid="import-file-reassign-action"' not in response.text

    async def test_import_review_partial_non_actionable_duplicate_hides_review_action(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 9)
            assert series is not None
            series.files_matched = 0
            series.files_already_owned = 2
            series.files_no_match = 1
            series.diagnostics = {
                **(series.diagnostics or {}),
                "actionable_duplicate_merge": False,
                "fully_owned_series": True,
                "existing_issue_count": 1,
                "owned_issue_count": 1,
            }
            files = list(
                (
                    await session.execute(
                        select(ImportedFile).where(ImportedFile.import_series_id == series.id)
                    )
                )
                .scalars()
                .all()
            )
            files[0].status = ImportedFileStatus.ALREADY_OWNED
            files[0].include_in_import = False
            await session.commit()

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=duplicate"
        )

        assert response.status_code == 200
        assert "No wanted or missing issues remain in the existing series" not in response.text
        assert "All existing issues are already owned" in response.text
        assert "replacement and upgrade paths are excluded from import review" in response.text
        assert "/import/1/series/9/details-partial" not in response.text
        assert 'data-testid="import-review-duplicate-unmatch-action"' in response.text

    async def test_import_review_duplicate_rows_show_not_this_series_action(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=duplicate"
        )

        assert response.status_code == 200
        assert 'data-testid="import-review-duplicate-unmatch-action"' in response.text
        assert "Not this series" in response.text

    async def test_import_review_matched_rows_show_not_this_series_action(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = (
                (
                    await session.execute(
                        select(ImportedSeries)
                        .where(ImportedSeries.import_job_id == job_id)
                        .order_by(ImportedSeries.id.asc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            assert series is not None
            series.cv_id = 143970
            series.cv_title = "Savage Tales One-Shot"
            series.cv_year = 2022
            series.cv_match_score = 0.84
            await session.commit()

        response = await authenticated_client.get(f"/import/{job_id}/review-partial?status=matched")

        assert response.status_code == 200
        assert 'data-testid="import-review-unmatch-action"' in response.text
        assert "unmatchSeriesMatch(1)" in response.text
        assert "Not this series" in response.text

    async def test_import_series_details_partial_non_actionable_duplicate_is_read_only(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 9)
            assert series is not None
            series.files_matched = 0
            series.files_already_owned = 2
            series.files_no_match = 1
            series.diagnostics = {
                **(series.diagnostics or {}),
                "actionable_duplicate_merge": False,
                "fully_owned_series": True,
                "existing_issue_count": 1,
                "owned_issue_count": 1,
            }
            files = list(
                (
                    await session.execute(
                        select(ImportedFile).where(ImportedFile.import_series_id == series.id)
                    )
                )
                .scalars()
                .all()
            )
            files[0].status = ImportedFileStatus.ALREADY_OWNED
            files[0].include_in_import = False
            await session.commit()

        response = await authenticated_client.get(f"/import/{job_id}/series/9/details-partial")

        assert response.status_code == 200
        assert 'data-testid="import-series-details-select-all-duplicate"' not in response.text
        assert 'data-testid="import-series-details-deselect-all-duplicate"' not in response.text
        assert "informational only" in response.text
        assert 'data-testid="import-file-assign-action"' not in response.text
        assert 'data-testid="import-file-reassign-action"' not in response.text

    async def test_import_review_no_match_filter_hides_selection_column_and_disables_bulk_buttons(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)
        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=no_match"
        )

        assert response.status_code == 200
        assert '<th class="w-8"></th>' not in response.text
        assert 'aria-label="Select Review Series 11 for import"' not in response.text
        assert 'aria-label="Select Review Series 12 for import"' not in response.text
        assert "data-import-review-toolbar-selection-summary" in response.text
        assert "Select all matched" not in response.text
        assert "Deselect all" not in response.text

    async def test_import_review_no_match_filter_excludes_series_conflicts(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 11)
            assert series is not None
            series.diagnostics = {
                "kind": "series_conflict",
                "reason": "metadata_signal_conflict",
                "normalized_query": "review series 11",
                "top_candidates": [
                    {"title": "Review Series 11", "score_pct": 91},
                    {"title": "Review Series Eleven", "score_pct": 89},
                ],
            }
            await session.commit()

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=no_match"
        )

        assert response.status_code == 200
        assert "Review Series 11" not in response.text
        assert "Review Series 12" in response.text
        assert "Review conflict" not in response.text

    async def test_import_review_no_match_filter_labels_series_found_file_mismatches(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 11)
            assert series is not None
            series.cv_id = 170538
            series.cv_title = "Absolute Wonder Woman 2026 Annual"
            series.user_selected_cv_id = 170538
            series.diagnostics = {
                "kind": "series_no_match",
                "reason": "file_no_match",
                "normalized_query": "absolute wonder woman",
                "raw_year": 2026,
                "top_candidates": [
                    {
                        "title": "Absolute Wonder Woman 2026 Annual",
                        "score_pct": 100,
                        "rejection_reasons": [
                            (
                                "Series matched, but no files could be matched "
                                "to issues in that series."
                            )
                        ],
                    }
                ],
                "unmatched_files": [
                    {
                        "file_name": "Absolute Wonder Woman 2026 Annual.cbz",
                        "parsed_series": "Absolute Wonder Woman",
                        "parsed_issue_number": 1.0,
                    }
                ],
            }
            await session.commit()

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=no_match"
        )

        assert response.status_code == 200
        assert "Needs issue" in response.text
        assert 'data-testid="import-review-reconcile-action"' in response.text
        assert 'data-tip="Change ComicVine match"' in response.text
        assert "Absolute Wonder Woman 2026 Annual" in response.text

    async def test_import_review_needs_issue_match_view_uses_reconcile_action(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 11)
            assert series is not None
            series.cv_id = 166903
            series.cv_title = "Powers 25"
            series.user_selected_cv_id = 166903
            series.files_matched = 0
            series.files_no_match = 1
            series.diagnostics = {
                "kind": "series_no_match",
                "reason": "file_no_match",
                "normalized_query": "powers 25",
                "raw_year": 2026,
                "unmatched_files": [
                    {
                        "file_name": "Powers 009.cbz",
                        "parsed_issue_number": 25.0,
                    }
                ],
            }
            await session.commit()

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=needs_issue"
        )

        assert response.status_code == 200
        assert "Reconcile for Import" in response.text
        assert "Needs Issue Match" in response.text
        assert "Needs issue" in response.text
        assert 'data-testid="import-review-reconcile-action"' in response.text
        assert 'data-tip="Reconcile files"' in response.text
        assert 'data-tip="Change ComicVine match"' in response.text
        assert 'data-testid="import-review-search-cv-action"' not in response.text

    async def test_import_review_needs_issue_match_includes_duplicate_rows(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=needs_issue"
        )

        assert response.status_code == 200
        assert "Needs Issue Match" in response.text
        assert "Review Series 9" in response.text
        assert "In Library" in response.text
        assert 'data-testid="import-review-reconcile-action"' in response.text
        assert 'aria-label="Reconcile files for Review Series 9"' in response.text

    async def test_import_review_needs_series_match_view_keeps_comicvine_search(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=needs_series"
        )

        assert response.status_code == 200
        assert "Reconcile for Import" in response.text
        assert "Needs Series Match" in response.text
        assert 'data-testid="import-review-search-cv-action"' in response.text
        assert 'data-testid="import-review-reconcile-action"' not in response.text

    async def test_import_review_safety_blocked_view_shows_allow_and_skip_actions(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 1)
            assert series is not None
            series.files_matched = 0
            series.diagnostics = {"safety_blocked_files": 1}
            session.add(
                ImportedFile(
                    import_job_id=job_id,
                    import_series_id=series.id,
                    file_path="/tmp/review-1/oversized.cbz",
                    file_name="Oversized Omnibus.cbz",
                    file_size=4_248_712_282,
                    file_format="cbz",
                    status=ImportedFileStatus.SAFETY_BLOCKED,
                    error_message="Archive decompressed size exceeds limit",
                    diagnostics={
                        "safety_block": {
                            "kind": "file_safety_blocked",
                            "reason": "Archive decompressed size exceeds limit",
                            "details": ["/tmp/review-1/oversized.cbz"],
                            "overrideable": True,
                        }
                    },
                )
            )
            await session.commit()

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=safety_blocked"
        )

        assert response.status_code == 200
        assert "Blocked Files" in response.text
        assert "Safety review" in response.text
        assert "Oversized Omnibus.cbz" in response.text
        assert 'data-testid="import-review-safety-category-summary"' in response.text
        assert "Decompression-size limit" in response.text
        assert "Code: archive_decompressed_size_limit" in response.text
        assert "Retry alone will not help" in response.text
        assert "Trusted override available" in response.text
        assert "/tmp/review-1/oversized.cbz" not in response.text
        assert 'data-testid="import-review-allow-safety-file"' in response.text
        assert 'data-testid="import-review-skip-safety-file"' in response.text
        assert f'hx-post="/import/{job_id}/files/' in response.text
        assert "/safety/allow-once?status=safety_blocked" in response.text
        assert "/safety/skip?status=safety_blocked" in response.text
        assert 'hx-target="#import-step-review-shell"' in response.text

    async def test_import_review_non_overrideable_safety_block_hides_allow_action(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 1)
            assert series is not None
            series.files_matched = 0
            series.diagnostics = {"safety_blocked_files": 1}
            session.add(
                ImportedFile(
                    import_job_id=job_id,
                    import_series_id=series.id,
                    file_path="/tmp/review-1/corrupt.cbz",
                    file_name="Corrupt Download.cbz",
                    file_size=694,
                    file_format="cbz",
                    status=ImportedFileStatus.SAFETY_BLOCKED,
                    error_message="Archive could not be inspected",
                    diagnostics={
                        "safety_block": {
                            "kind": "file_safety_blocked",
                            "reason": "Archive could not be inspected",
                            "details": ["/tmp/review-1/corrupt.cbz"],
                            "overrideable": False,
                        }
                    },
                )
            )
            await session.commit()

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=safety_blocked"
        )

        assert response.status_code == 200
        assert "Corrupt Download.cbz" in response.text
        assert "Archive inspection failed" in response.text
        assert "Retry may help after remediation" in response.text
        assert "Override not allowed" in response.text
        assert "Cannot override" in response.text
        assert 'data-testid="import-review-allow-safety-file"' not in response.text
        assert "/safety/allow-once?status=safety_blocked" not in response.text
        assert 'data-testid="import-review-skip-safety-file"' in response.text
        assert "/safety/skip?status=safety_blocked" in response.text

    async def test_import_review_keeps_approved_file_in_safety_view_while_rematching(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 1)
            assert series is not None
            series.files_matched = 0
            series.diagnostics = {"safety_blocked_files": 1, "rematch_pending": True}
            session.add(
                ImportedFile(
                    import_job_id=job_id,
                    import_series_id=series.id,
                    file_path="/tmp/review-1/oversized.cbz",
                    file_name="Oversized Omnibus.cbz",
                    file_size=4_248_712_282,
                    file_format="cbz",
                    status=ImportedFileStatus.SAFETY_APPROVED,
                    diagnostics={"safety_exception": {"allowed_once": True}},
                )
            )
            await session.commit()

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=safety_blocked"
        )

        assert response.status_code == 200
        assert "Preparing this file for matching" in response.text
        assert "Preparing match" in response.text
        assert 'data-testid="import-review-safety-rematch-spinner"' in response.text
        assert 'hx-trigger="every 2s [window.pullboxLiveUpdatesEnabled()]"' in response.text
        assert 'data-testid="import-review-allow-safety-file"' not in response.text
        assert 'data-testid="import-review-skip-safety-file"' not in response.text

    async def test_import_review_returns_to_all_when_safety_tab_becomes_empty(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=safety_blocked"
        )

        assert response.status_code == 200
        assert 'name="review_status_filter" value=""' in response.text
        assert 'data-import-review-view="series"' in response.text

    async def test_import_review_allow_safety_file_post_refreshes_review(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from sqlalchemy import select

        from pullbox.api.middleware import SESSION_COOKIE_NAME
        from pullbox.models.import_job import (
            ImportedFile,
            ImportedFileStatus,
            ImportedSeries,
            ImportSeriesStatus,
        )
        from pullbox.services.auth_service import AuthService

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = (
                (
                    await session.execute(
                        select(ImportedSeries)
                        .where(ImportedSeries.import_job_id == job_id)
                        .order_by(ImportedSeries.id.asc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            assert series is not None
            series.status = ImportSeriesStatus.NO_MATCH
            series.files_matched = 0
            series.diagnostics = {"safety_blocked_files": 1}
            blocked_file = ImportedFile(
                import_job_id=job_id,
                import_series_id=series.id,
                file_path="/tmp/review-1/oversized.cbz",
                file_name="Oversized Omnibus.cbz",
                file_size=4_248_712_282,
                file_format="cbz",
                status=ImportedFileStatus.SAFETY_BLOCKED,
                error_message="Archive decompressed size exceeds limit",
                diagnostics={
                    "safety_block": {
                        "kind": "file_safety_blocked",
                        "reason": "Archive decompressed size exceeds limit",
                        "details": ["/tmp/review-1/oversized.cbz"],
                    }
                },
            )
            session.add(blocked_file)
            await session.flush()
            blocked_file_id = blocked_file.id
            await session.commit()

        session_token = authenticated_client.cookies.get(SESSION_COOKIE_NAME)
        assert session_token
        response = await authenticated_client.post(
            (f"/import/{job_id}/files/{blocked_file_id}/safety/allow-once?status=safety_blocked"),
            headers={"x-csrf-token": AuthService.get_csrf_token_from_session(session_token) or ""},
        )

        assert response.status_code == 200
        assert 'data-testid="import-collection-review"' in response.text
        async with sec_db() as session:
            refreshed_file = await session.get(ImportedFile, blocked_file_id)
            assert refreshed_file is not None
            assert refreshed_file.status == ImportedFileStatus.SAFETY_APPROVED
            assert refreshed_file.include_in_import is False
            assert refreshed_file.diagnostics["safety_exception"]["allowed_once"] is True

    async def test_import_review_duplicate_filter_shows_deliberate_file_selection(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)
        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=duplicate"
        )

        assert response.status_code == 200
        assert '<th class="w-8"></th>' in response.text
        assert 'data-testid="import-review-duplicate-select"' in response.text
        assert 'aria-label="Import matched files for Review Series 9"' in response.text
        assert "toggleDuplicateSeriesFiles(9" in response.text
        assert "data-import-review-toolbar-selection-summary" in response.text
        assert "Select all matched" not in response.text
        assert "Deselect all" not in response.text

    async def test_import_review_non_actionable_duplicate_hides_file_selection_checkbox(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 9)
            assert series is not None
            series.files_matched = 0
            series.files_already_owned = 2
            series.files_no_match = 1
            series.diagnostics = {
                **(series.diagnostics or {}),
                "actionable_duplicate_merge": False,
                "fully_owned_series": True,
                "existing_issue_count": 1,
                "owned_issue_count": 1,
            }
            files = list(
                (
                    await session.execute(
                        select(ImportedFile).where(ImportedFile.import_series_id == series.id)
                    )
                )
                .scalars()
                .all()
            )
            files[0].status = ImportedFileStatus.ALREADY_OWNED
            files[0].include_in_import = False
            await session.commit()

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=duplicate"
        )

        assert response.status_code == 200
        assert 'aria-label="Import matched files for Review Series 9"' not in response.text
        assert 'aria-label="Import matched files for Review Series 10"' in response.text

    async def test_import_review_conflicted_matched_rows_are_not_bulk_selectable(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/review-partial")

        assert response.status_code == 200
        assert "Resolve file conflicts before selecting this series." in response.text
        conflict_pill = r'<span class="pill pill-warning">[\s\S]*?Conflict[\s\S]*?</span>'
        assert re.search(conflict_pill, response.text)
        assert '@click="selectAllImportable()"' in response.text
        assert '@click="deselectAllImportable()"' in response.text
        assert 'data-import-review-selectable="7"' not in response.text
        assert 'aria-label="Select Review Series 7 for import"' not in response.text

    async def test_import_review_non_importable_status_rows_are_not_selectable(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedSeries, ImportSeriesStatus

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 6)
            assert series is not None
            series.status = ImportSeriesStatus.SKIPPED
            series.selected_for_import = False
            await session.commit()

        response = await authenticated_client.get(f"/import/{job_id}/review-partial")

        assert response.status_code == 200
        assert "Review Series 6" in response.text
        assert 'data-import-review-selectable="6"' not in response.text
        assert 'aria-label="Select Review Series 6 for import"' not in response.text

    async def test_import_review_matched_filter_excludes_conflicted_matched_rows(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/review-partial?status=matched")

        assert response.status_code == 200
        assert "Review Series 1" in response.text
        assert "Review Series 6" in response.text
        assert "Review Series 7" not in response.text
        assert "Review Series 8" not in response.text
        assert "Resolve file conflicts before selecting this series." not in response.text

    async def test_import_review_duplicate_owned_only_files_show_zero_importable_with_tooltip(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import ImportedSeries

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 9)
            assert series is not None
            series.files_total = 1
            series.files_matched = 0
            series.files_already_owned = 1
            series.files_duplicate = 0
            series.files_conflict = 0
            series.files_no_match = 0
            await session.commit()

        response = await authenticated_client.get(
            f"/import/{job_id}/review-partial?status=duplicate"
        )

        assert response.status_code == 200
        assert "Owned 1/1" not in response.text
        assert "0/1" in response.text
        assert "1 owned, 0 importable, 1 total." in response.text
        assert "100%" in response.text

    async def test_import_conflicts_partial_renders_conflict_diagnostics(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/conflicts-partial")

        assert response.status_code == 200
        assert 'data-testid="import-conflict-why-action"' in response.text
        assert "Auto-resolution details" in response.text
        assert "Selected file includes embedded ComicInfo.xml metadata." in response.text
        assert 'pill pill-success">ComicInfo' not in response.text
        assert "Choose one file to keep" in response.text
        assert "Review the auto-selected keep choice below." not in response.text
        assert "Preferred file:" not in response.text
        assert "Save file conflict choices" not in response.text
        assert "#?" not in response.text

    async def test_import_conflicts_partial_uses_connected_table_with_detail_rows(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/conflicts-partial")

        assert response.status_code == 200
        assert 'data-testid="import-conflicts-table-shell"' in response.text
        assert 'data-testid="import-conflicts-table"' in response.text
        assert 'class="downloads-table min-w-[980px] text-sm"' in response.text
        assert "Conflict review" not in response.text
        assert "Resolve same-issue file collisions" not in response.text
        assert 'class="downloads-history-toolbar"' not in response.text
        assert 'data-testid="import-conflict-detail-row"' in response.text
        for field in ("series", "conflict", "files", "signal", "status"):
            assert f'data-testid="import-conflicts-sort-{field}"' in response.text
        assert "<article" not in response.text
        assert "rounded-xl border border-pb-border bg-pb-card/60" not in response.text

    async def test_import_conflicts_partial_sorts_by_series_descending(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_unsorted_conflict_review_job(sec_db)

        response = await authenticated_client.get(
            f"/import/{job_id}/conflicts-partial?sort=-series"
        )

        assert response.status_code == 200
        assert 'name="conflicts_sort" value="-series"' in response.text
        zulu_index = response.text.index("Zulu Force")
        chicken_index = response.text.index("Chicken Devil")
        abattoir_index = response.text.index("Abattoir")
        assert zulu_index < chicken_index < abattoir_index

    async def test_import_conflicts_partial_surfaces_mixed_series_bucket(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_mixed_conflict_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/conflicts-partial")

        assert response.status_code == 200
        assert "Mixed series bucket" in response.text
        assert "Absolute Martian Manhunter" in response.text
        assert "Abattoir" in response.text
        assert "Chicken Devil" in response.text

    async def test_import_conflicts_partial_uses_filename_parse_for_mixed_bucket_warning(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_filename_consistent_conflict_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/conflicts-partial")

        assert response.status_code == 200
        assert "Mixed series bucket" not in response.text
        assert (
            "This group contains files that parsed to different series names." not in response.text
        )
        assert "Chicken Devil" in response.text
        assert "Chicken Devils" not in response.text

    async def test_import_conflicts_partial_renders_series_candidate_conflict_details(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.import_job import (
            ImportedFile,
            ImportedSeries,
            ImportSeriesStatus,
        )

        job_id = await _seed_import_review_job(sec_db)
        async with sec_db() as session:
            series = await session.get(ImportedSeries, 1)
            assert series is not None
            series.status = ImportSeriesStatus.NO_MATCH
            series.cv_id = None
            series.cv_title = None
            series.cv_year = None
            series.cv_publisher = None
            series.cv_issue_count = None
            series.cv_match_score = None
            series.cv_match_method = None
            series.diagnostics = {
                "kind": "series_conflict",
                "reason": "metadata_signal_conflict",
                "normalized_query": "chicken devil",
                "raw_year": 2022,
                "threshold": 0.7,
                "selected_signal": "comicinfo",
                "competing_signal": "filename",
                "signal_file_name": "Chicken Devil 004 (2022).cb7",
                "selected_candidate": {
                    "title": "Chicken Devils",
                    "year": 2022,
                    "score_pct": 97,
                    "match_method": "fuzzy_title",
                },
                "competing_candidate": {
                    "title": "Chicken Devil",
                    "year": 2021,
                    "score_pct": 90,
                },
                "top_candidates": [
                    {
                        "title": "Chicken Devils",
                        "year": 2022,
                        "publisher": "AfterShock Comics",
                        "issue_count": 4,
                        "score_pct": 97,
                        "rejection_reasons": ["Pluralized title outranked an exact-title near-tie"],
                    },
                    {
                        "title": "Chicken Devil",
                        "year": 2021,
                        "publisher": "AfterShock Comics",
                        "issue_count": 4,
                        "score_pct": 90,
                        "rejection_reasons": [
                            "Exact title candidate stayed within the ambiguity margin"
                        ],
                    },
                ],
            }
            file_row = (
                (
                    await session.execute(
                        select(ImportedFile)
                        .where(ImportedFile.import_series_id == series.id)
                        .order_by(ImportedFile.id.asc())
                    )
                )
                .scalars()
                .first()
            )
            assert file_row is not None
            file_row.has_comicinfo = True
            file_row.file_name = "Chicken Devil 004 (2022).cb7"
            file_row.file_path = "/Users/adam/Downloads/test/Chicken Devil 004 (2022).cb7"
            file_row.parsed_series = "Chicken Devils"
            file_row.parsed_year = 2023
            file_row.diagnostics = {
                "source_metadata": {
                    "has_comicinfo": True,
                    "comicinfo": {
                        "series": "Chicken Devils",
                        "number": "4",
                        "year": 2023,
                        "title": "The Chickens Made Me Do IT",
                        "web": (
                            "https://comicvine.gamespot.com/"
                            "chicken-devils-4-the-chickens-made-me-do-it/4000-996957/"
                        ),
                    },
                }
            }
            await session.commit()

        response = await authenticated_client.get(f"/import/{job_id}/conflicts-partial")

        assert response.status_code == 200
        assert "Series match conflict" in response.text
        assert "Review ComicVine match" in response.text
        assert "Conflicting candidates" in response.text
        assert "The file name and embedded ComicInfo.xml point to different series" in response.text
        assert (
            "Review this file manually to verify its provenance before assigning a series"
            in response.text
        )
        assert "Metadata mismatch" in response.text
        assert "Parsed From File Name" in response.text
        assert "Embedded ComicInfo.xml" in response.text
        assert "ComicInfo.xml" in response.text
        assert (
            'data-testid="import-conflict-metadata-grid" '
            'class="mt-3 grid gap-4 sm:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]"'
        ) in response.text
        assert re.search(
            (
                r"Parsed From File Name.*?"
                r"<dt class=\"text-pb-text-dim\">Series</dt>\s*"
                r"<dd class=\"text-pb-text\">\s*Chicken Devil\s*</dd>"
            ),
            response.text,
            re.S,
        )
        assert re.search(
            (
                r"Embedded ComicInfo\.xml.*?"
                r"<dt class=\"text-pb-text-dim\">Series</dt>\s*"
                r"<dd class=\"text-pb-text\">\s*Chicken Devils\s*</dd>"
            ),
            response.text,
            re.S,
        )
        assert (
            "https://comicvine.gamespot.com/chicken-devils-4-the-chickens-made-me-do-it/4000-996957/"
            in response.text
        )

    async def test_import_conflicts_partial_uses_60_40_auto_resolution_split(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_import_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/conflicts-partial")

        assert response.status_code == 200
        assert (
            'data-testid="import-conflict-auto-resolution-grid" '
            'class="mt-2 grid gap-4 sm:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]"'
        ) in response.text

    async def test_import_conflicts_partial_uses_shared_pagination_contract(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_large_conflict_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/conflicts-partial?page=2")

        assert response.status_code == 200
        assert 'name="conflicts_page" value="2"' in response.text
        assert (
            'id="page-footer-dock" data-testid="page-footer-dock" hx-swap-oob="innerHTML"'
        ) in response.text
        assert 'data-testid="import-conflicts-pagination"' in response.text
        assert 'data-testid="page-dock-pagination"' in response.text
        assert f"/import/{job_id}/conflicts-partial" in response.text
        assert "sort=series" in response.text
        assert "page=1" in response.text
        assert 'hx-push-url="false"' in response.text
        assert "per page" in response.text
        assert ">25<" in response.text
        assert response.text.count('data-testid="import-conflict-detail-row"') == 5
        table_shell_index = response.text.index('data-testid="import-conflicts-table-shell"')
        footer_index = response.text.index('data-testid="page-footer-dock"')
        assert table_shell_index < footer_index
        assert (
            'data-testid="import-conflicts-pagination"'
            not in response.text[table_shell_index:footer_index]
        )

    async def test_import_conflicts_partial_sorts_groups_by_series_name(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        job_id = await _seed_unsorted_conflict_review_job(sec_db)

        response = await authenticated_client.get(f"/import/{job_id}/conflicts-partial")

        assert response.status_code == 200
        abattoir_index = response.text.index("Abattoir")
        chicken_index = response.text.index("Chicken Devil")
        zulu_index = response.text.index("Zulu Force")
        assert abattoir_index < chicken_index < zulu_index
