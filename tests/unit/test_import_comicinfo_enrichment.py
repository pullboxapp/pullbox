from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pullbox.core.exceptions import ProviderError
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.operation_progress import OperationProgress, OperationProgressState
from pullbox.models.series import Series
from pullbox.services import import_comicinfo_enrichment as enrichment_module
from pullbox.services.import_comicinfo_enrichment import (
    PreparedComicInfoEnrichment,
    run_import_comicinfo_enrichment,
    run_pending_import_comicinfo_enrichment,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_run_import_comicinfo_enrichment_rewrites_pending_library_file(
    async_engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    archive_path = tmp_path / "King Dracula 004.cbz"
    archive_path.write_text("archive")

    async with session_factory() as session:
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
        series = Series(
            title="King Dracula",
            sort_title="king dracula",
            year_start=2024,
            comicvine_id=171911,
            issue_count=4,
        )
        session.add_all([root, series])
        await session.flush()
        issue = Issue(
            series_id=series.id,
            issue_number=4.0,
            comicvine_id=1234567,
            issue_type=IssueType.ISSUE,
            status=IssueStatus.WANTED,
        )
        session.add(issue)
        await session.flush()
        library_file = LibraryFile(
            file_path=str(archive_path),
            file_name=archive_path.name,
            file_size=archive_path.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(tz=UTC),
            match_confidence=MatchConfidence.HIGH,
            issue_id=issue.id,
            library_root_id=root.id,
        )
        job = ImportJob(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.COMPLETED,
        )
        imported_series = ImportedSeries(
            import_job_id=1,
            raw_series_name="King Dracula",
            status=ImportSeriesStatus.IMPORTED,
            series_id=series.id,
        )
        session.add_all([library_file, job])
        await session.flush()
        imported_series.import_job_id = job.id
        session.add(imported_series)
        await session.flush()
        imported_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imported_series.id,
            file_path=str(tmp_path / "source.cbz"),
            file_name="source.cbz",
            file_size=100,
            file_format="cbz",
            parsed_series="King Dracula",
            parsed_issue_number=4.0,
            status=ImportedFileStatus.IMPORTED,
            matched_issue_id=issue.id,
            library_file_id=library_file.id,
            diagnostics={
                "comicinfo_enrichment": {
                    "status": "pending",
                    "reason": "deferred_during_import",
                    "issue_id": issue.id,
                    "issue_cv_id": issue.comicvine_id,
                    "library_file_id": library_file.id,
                }
            },
        )
        session.add(imported_file)
        await session.commit()
        job_id = job.id
        issue_id = issue.id
        imported_file_id = imported_file.id

    payload_session: AsyncSession | None = None
    build_calls = 0

    async def build_payload(
        session: AsyncSession,
        issue: Issue,
        *,
        source_path: Path | None = None,
        defer_issue_enrichment: bool = False,
        propagate_retryable_provider_errors: bool = False,
    ) -> dict[str, Any]:
        nonlocal build_calls, payload_session
        assert source_path == archive_path
        assert defer_issue_enrichment is False
        assert propagate_retryable_provider_errors is True
        build_calls += 1
        payload_session = session
        issue.description = "Refreshed ComicVine summary."
        issue.release_date = date(2026, 6, 17)
        issue.comicvine_url = "https://comicvine.gamespot.com/king-dracula-4/4000-1234567/"
        return {
            "Series": "King Dracula",
            "Number": "4",
            "Summary": issue.description,
            "Year": issue.release_date.year,
        }

    applied: list[tuple[Path, dict[str, Any]]] = []
    event_loop_thread_id = threading.get_ident()
    apply_thread_ids: list[int] = []

    def apply_comicinfo(artifact_path: Path, payload: dict[str, Any]) -> None:
        apply_thread_ids.append(threading.get_ident())
        assert payload_session is not None
        assert not payload_session.in_transaction()
        applied.append((artifact_path, dict(payload)))
        artifact_path.write_text("updated archive")

    log_events: list[str] = []

    async def log_event(
        session: AsyncSession,
        job_id: int,
        level: str,
        event: str,
        *,
        message: str,
        **details: Any,
    ) -> None:
        _ = session, job_id, level, message, details
        log_events.append(event)

    original_commit = AsyncSession.commit
    lock_injected = False

    async def commit_with_one_transient_lock(session: AsyncSession) -> None:
        nonlocal lock_injected
        if build_calls == 1 and not lock_injected:
            lock_injected = True
            raise OperationalError(
                "UPDATE issues",
                {},
                sqlite3.OperationalError("database is locked"),
            )
        await original_commit(session)

    monkeypatch.setattr(AsyncSession, "commit", commit_with_one_transient_lock)
    monkeypatch.setattr(enrichment_module, "sqlite_lock_retry_delay", lambda _attempt: 0.0)

    await run_import_comicinfo_enrichment(
        session_factory,
        job_id=job_id,
        build_comicinfo_payload=build_payload,
        apply_comicinfo=apply_comicinfo,
        log_event=log_event,
    )

    assert applied == [
        (
            archive_path,
            {
                "Series": "King Dracula",
                "Number": "4",
                "Summary": "Refreshed ComicVine summary.",
                "Year": 2026,
            },
        )
    ]
    assert "import_file_comicinfo_enrichment_completed" in log_events
    assert apply_thread_ids != [event_loop_thread_id]
    assert lock_injected is True
    assert build_calls == 2
    async with session_factory() as session:
        imported_file = await session.get(ImportedFile, imported_file_id)
        issue = await session.get(Issue, issue_id)
        assert imported_file is not None
        assert issue is not None
        assert imported_file.library_file_id is not None
        library_file = await session.get(LibraryFile, imported_file.library_file_id)
        assert library_file is not None
        assert library_file.has_comicinfo is True
        assert imported_file.diagnostics["comicinfo_enrichment"]["status"] == "complete"
        assert imported_file.diagnostics["comicinfo_enrichment"]["library_file_id"] is not None
        assert issue.description == "Refreshed ComicVine summary."
        progress = (await session.scalars(select(OperationProgress))).one_or_none()
        assert progress is not None
        assert progress.operation_key == f"metadata:{job_id}"
        assert progress.state == OperationProgressState.COMPLETED
        assert progress.overall_percent == 100
        assert progress.item_key is None


@pytest.mark.asyncio
async def test_locked_pending_file_does_not_stop_later_enrichment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    later_archive = tmp_path / "Later Issue.cbz"
    later_archive.write_text("archive")
    prepared = PreparedComicInfoEnrichment(
        artifact_path=later_archive,
        payload={"Series": "Later Series", "Number": "2"},
        library_file_id=22,
        library_file_name=later_archive.name,
        issue_id=202,
        issue_cv_id=2002,
    )

    async def load_pending_ids(_factory: object, *, job_id: int) -> list[int]:
        assert job_id == 7
        return [101, 202]

    async def prepare_pending(
        _factory: object,
        *,
        imported_file_id: int,
        build_comicinfo_payload: object,
    ) -> PreparedComicInfoEnrichment | None:
        _ = build_comicinfo_payload
        return None if imported_file_id == 101 else prepared

    completed: list[int] = []

    async def job_is_completed(_factory: object, *, job_id: int) -> bool:
        assert job_id == 7
        return True

    async def mark_complete(
        _factory: object,
        *,
        job_id: int,
        imported_file_id: int,
        prepared: PreparedComicInfoEnrichment,
        log_event: object,
    ) -> bool:
        _ = job_id, prepared, log_event
        completed.append(imported_file_id)
        return True

    monkeypatch.setattr(enrichment_module, "_load_pending_imported_file_ids", load_pending_ids)
    monkeypatch.setattr(enrichment_module, "_import_job_is_completed", job_is_completed)
    monkeypatch.setattr(
        enrichment_module,
        "_prepare_pending_imported_file_with_retry",
        prepare_pending,
    )
    monkeypatch.setattr(
        enrichment_module,
        "_mark_pending_file_complete_with_retry",
        mark_complete,
    )

    applied: list[Path] = []

    def apply_comicinfo(path: Path, payload: dict[str, Any]) -> None:
        assert payload == {"Series": "Later Series", "Number": "2"}
        applied.append(path)

    async def unused_build_payload(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("preparation was replaced for this control-flow test")

    async def unused_log_event(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("completion persistence was replaced for this test")

    await run_import_comicinfo_enrichment(
        object(),  # type: ignore[arg-type]
        job_id=7,
        build_comicinfo_payload=unused_build_payload,
        apply_comicinfo=apply_comicinfo,
        log_event=unused_log_event,
    )

    assert applied == [later_archive]
    assert completed == [202]


@pytest.mark.asyncio
async def test_retryable_provider_error_stops_enrichment_and_leaves_queue_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def job_is_completed(_factory: object, *, job_id: int) -> bool:
        assert job_id == 7
        return True

    async def load_pending_ids(_factory: object, *, job_id: int) -> list[int]:
        assert job_id == 7
        return [101, 202]

    prepared_ids: list[int] = []

    async def prepare_pending(
        _factory: object,
        *,
        imported_file_id: int,
        build_comicinfo_payload: object,
    ) -> PreparedComicInfoEnrichment | None:
        _ = build_comicinfo_payload
        prepared_ids.append(imported_file_id)
        raise ProviderError(
            "comicvine",
            "HTTP 420: /issue/4000-1234/",
            details={"status_code": 420, "retryable": True},
        )

    async def should_not_mark_failed(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("retryable enrichment must remain pending")

    monkeypatch.setattr(enrichment_module, "_load_pending_imported_file_ids", load_pending_ids)
    monkeypatch.setattr(enrichment_module, "_import_job_is_completed", job_is_completed)
    monkeypatch.setattr(
        enrichment_module,
        "_prepare_pending_imported_file_with_retry",
        prepare_pending,
    )
    monkeypatch.setattr(enrichment_module, "_mark_pending_file_failed", should_not_mark_failed)

    def should_not_apply(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("partial ComicInfo must not be applied")

    async def unused_build_payload(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("preparation was replaced for this control-flow test")

    async def unused_log_event(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no durable file event is expected")

    await run_import_comicinfo_enrichment(
        object(),  # type: ignore[arg-type]
        job_id=7,
        build_comicinfo_payload=unused_build_payload,
        apply_comicinfo=should_not_apply,
        log_event=unused_log_event,
    )

    assert prepared_ids == [101]


@pytest.mark.asyncio
async def test_run_pending_import_comicinfo_enrichment_recovers_completed_jobs_after_restart(
    async_engine,
    tmp_path: Path,
) -> None:
    """Startup recovery should resume deferred ComicInfo work lost during restart."""
    session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
    archive_path = tmp_path / "Recovered Issue.cbz"
    archive_path.write_text("archive")

    async with session_factory() as session:
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
        series = Series(
            title="Recovered Series",
            sort_title="recovered series",
            year_start=2026,
            comicvine_id=999001,
            issue_count=1,
        )
        session.add_all([root, series])
        await session.flush()
        issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            comicvine_id=999101,
            issue_type=IssueType.ISSUE,
            status=IssueStatus.WANTED,
        )
        session.add(issue)
        await session.flush()
        library_file = LibraryFile(
            file_path=str(archive_path),
            file_name=archive_path.name,
            file_size=archive_path.stat().st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(tz=UTC),
            match_confidence=MatchConfidence.HIGH,
            issue_id=issue.id,
            library_root_id=root.id,
        )
        completed_job = ImportJob(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.COMPLETED,
        )
        running_job = ImportJob(
            source_path=str(tmp_path),
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.IMPORTING,
        )
        session.add_all([library_file, completed_job, running_job])
        await session.flush()
        completed_series = ImportedSeries(
            import_job_id=completed_job.id,
            raw_series_name="Recovered Series",
            status=ImportSeriesStatus.IMPORTED,
            series_id=series.id,
        )
        running_series = ImportedSeries(
            import_job_id=running_job.id,
            raw_series_name="Running Series",
            status=ImportSeriesStatus.IMPORTING,
            series_id=series.id,
        )
        session.add_all([completed_series, running_series])
        await session.flush()
        session.add_all(
            [
                ImportedFile(
                    import_job_id=completed_job.id,
                    import_series_id=completed_series.id,
                    file_path=str(tmp_path / "source.cbz"),
                    file_name="source.cbz",
                    file_size=100,
                    file_format="cbz",
                    parsed_series="Recovered Series",
                    parsed_issue_number=1.0,
                    status=ImportedFileStatus.IMPORTED,
                    matched_issue_id=issue.id,
                    library_file_id=library_file.id,
                    diagnostics={
                        "comicinfo_enrichment": {
                            "status": "pending",
                            "reason": "deferred_during_import",
                            "issue_id": issue.id,
                            "issue_cv_id": issue.comicvine_id,
                            "library_file_id": library_file.id,
                        }
                    },
                ),
                ImportedFile(
                    import_job_id=running_job.id,
                    import_series_id=running_series.id,
                    file_path=str(tmp_path / "running.cbz"),
                    file_name="running.cbz",
                    file_size=100,
                    file_format="cbz",
                    parsed_series="Running Series",
                    parsed_issue_number=1.0,
                    status=ImportedFileStatus.IMPORTED,
                    matched_issue_id=issue.id,
                    library_file_id=library_file.id,
                    diagnostics={
                        "comicinfo_enrichment": {
                            "status": "pending",
                            "reason": "deferred_during_import",
                            "issue_id": issue.id,
                            "issue_cv_id": issue.comicvine_id,
                            "library_file_id": library_file.id,
                        }
                    },
                ),
            ]
        )
        await session.commit()

    applied: list[Path] = []

    async def build_payload(
        session: AsyncSession,
        issue: Issue,
        *,
        source_path: Path | None = None,
        defer_issue_enrichment: bool = False,
        propagate_retryable_provider_errors: bool = False,
    ) -> dict[str, Any]:
        _ = session, issue, source_path
        assert defer_issue_enrichment is False
        assert propagate_retryable_provider_errors is True
        return {"Series": "Recovered Series", "Number": "1"}

    def apply_comicinfo(artifact_path: Path, payload: dict[str, Any]) -> None:
        _ = payload
        applied.append(artifact_path)
        artifact_path.write_text("recovered archive")

    log_events: list[str] = []

    async def log_event(
        session: AsyncSession,
        job_id: int,
        level: str,
        event: str,
        *,
        message: str,
        **details: Any,
    ) -> None:
        _ = session, job_id, level, message, details
        log_events.append(event)

    recovered_jobs = await run_pending_import_comicinfo_enrichment(
        session_factory,
        build_comicinfo_payload=build_payload,
        apply_comicinfo=apply_comicinfo,
        log_event=log_event,
    )

    assert recovered_jobs == 1
    assert applied == [archive_path]
    assert archive_path.read_text() == "recovered archive"
    assert log_events == ["import_file_comicinfo_enrichment_completed"]
