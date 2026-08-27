"""Tests for Task R-5.1 — Import execution with file placement.

Verifies that run_import() processes confirmed files via
register_library_file(), handles move vs leave-in-place, updates
file-level counters, and gracefully handles per-file errors.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

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
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series
from pullbox.schemas.import_job import (
    ConfirmImportRequest,
    ConflictResolution,
    FileMatchOverride,
)
from pullbox.services.import_service import ImportService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_service(
    *,
    series_service: AsyncMock | None = None,
    metadata_service: AsyncMock | None = None,
    event_bus: AsyncMock | None = None,
) -> ImportService:
    return ImportService(
        series_service=series_service or AsyncMock(),
        metadata_service=metadata_service or AsyncMock(),
        event_bus=event_bus or AsyncMock(),
    )


async def _setup_full_scenario(
    session: AsyncSession,
    *,
    file_statuses: list[ImportedFileStatus] | None = None,
    series_status: ImportSeriesStatus = ImportSeriesStatus.CONFIRMED,
    job_status: ImportJobStatus = ImportJobStatus.IMPORTING,
    num_issues: int = 3,
    create_library_root: bool = True,
) -> tuple[ImportJob, ImportedSeries, list[ImportedFile], Series, list[Issue]]:
    """Create a full import scenario with series, issues, and import records.

    Returns (job, imp_series, imp_files, series, issues).
    """
    pub = Publisher(name="DC Comics", comicvine_id=10)
    session.add(pub)
    await session.flush()

    series = Series(
        title="Batman",
        sort_title="batman",
        year_start=2016,
        comicvine_id=97508,
        publisher_id=pub.id,
    )
    session.add(series)
    await session.flush()

    issues: list[Issue] = []
    for i in range(1, num_issues + 1):
        issue = Issue(
            series_id=series.id,
            issue_number=float(i),
            comicvine_id=100000 + i,
            title=f"Issue #{i}",
            status=IssueStatus.WANTED,
        )
        session.add(issue)
        issues.append(issue)
    await session.flush()

    if create_library_root:
        root = LibraryRoot(name="Comics", path="/tmp/comics-lib", enabled=True)
        session.add(root)
        await session.flush()

    job = ImportJob(
        source_path="/tmp/comics",
        source_type=ImportSourceType.FILESYSTEM,
        status=job_status,
    )
    session.add(job)
    await session.flush()

    imp_series = ImportedSeries(
        import_job_id=job.id,
        raw_series_name="Batman",
        raw_year=2016,
        status=series_status,
        cv_id=97508,
        cv_match_score=0.95,
        cv_match_method="exact_title_year",
        series_id=series.id,
        file_count=num_issues,
    )
    session.add(imp_series)
    await session.flush()

    if file_statuses is None:
        file_statuses = [ImportedFileStatus.CONFIRMED] * num_issues

    imp_files: list[ImportedFile] = []
    for i, status in enumerate(file_statuses):
        idx = i + 1
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path=f"/tmp/comics/Batman {idx:03d}.cbz",
            file_name=f"Batman {idx:03d}.cbz",
            file_size=1024 * idx,
            file_format="cbz",
            parsed_series="Batman",
            parsed_issue_number=float(idx),
            status=status,
            matched_issue_id=issues[i].id if i < len(issues) else None,
            match_confidence="high",
            match_method="issue_number",
        )
        session.add(imp_file)
        imp_files.append(imp_file)
    await session.flush()

    return job, imp_series, imp_files, series, issues


def _mock_register_library_file() -> AsyncMock:
    """Create a mock for register_library_file that creates real LibraryFile rows."""
    mock = AsyncMock()

    async def _side_effect(
        session: object,
        source_path: object,
        issue: object,
        confidence: object,
        **kwargs: object,
    ) -> LibraryFile:
        from datetime import UTC, datetime

        from sqlalchemy import select as sa_sel
        from sqlalchemy.ext.asyncio import AsyncSession as AsyncSess

        from pullbox.models.library import FileFormat

        assert isinstance(session, AsyncSess)
        # Find the existing library root
        root_result = await session.execute(sa_sel(LibraryRoot).limit(1))
        root = root_result.scalars().first()
        root_id = root.id if root else 1

        # Create a real LibraryFile so FK constraints are satisfied
        lf = LibraryFile(
            file_path=str(source_path),
            file_name=Path(str(source_path)).name,
            file_size=1024,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(tz=UTC),
            match_confidence=confidence,
            issue_id=issue.id,  # type: ignore[union-attr]
            library_root_id=root_id,
        )
        session.add(lf)
        await session.flush()  # type: ignore[attr-defined]
        return lf

    mock.side_effect = _side_effect
    return mock


# ── R-5.1: File Processing During Import ─────────────────────────────────


class TestConfirmedFilesCallRegister:
    """Confirmed files call register_library_file() for each file."""

    @pytest.mark.asyncio
    async def test_register_called_per_confirmed_file(self, db_session: AsyncSession) -> None:
        job, _imp_series, _imp_files, series, _issues = await _setup_full_scenario(
            db_session, num_issues=3
        )
        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        assert mock_register.call_count == 3


class TestFileStatusUpdatedOnSuccess:
    """ImportedFile.status is set to IMPORTED and library_file_id is set."""

    @pytest.mark.asyncio
    async def test_file_imported_status_and_library_file_id(self, db_session: AsyncSession) -> None:
        job, _imp_series, imp_files, series, _issues = await _setup_full_scenario(
            db_session, num_issues=1
        )
        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        await db_session.refresh(imp_files[0])
        assert imp_files[0].status == ImportedFileStatus.IMPORTED
        assert imp_files[0].library_file_id is not None

    @pytest.mark.asyncio
    async def test_zero_issue_special_placeholder_creates_resolvable_issue(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        from datetime import UTC, datetime

        from pullbox.models.library import FileFormat
        from pullbox.services.import_file_execution import process_import_series_files

        pub = Publisher(name="King Features", comicvine_id=123)
        db_session.add(pub)
        await db_session.flush()
        series = Series(
            title="Flash Gordon: The 1995 Special",
            sort_title="flash gordon the 1995 special",
            year_start=None,
            comicvine_id=172262,
            publisher_id=pub.id,
            issue_count=0,
        )
        db_session.add(series)
        root = LibraryRoot(name="Comics", path=str(tmp_path / "library"), enabled=True)
        db_session.add(root)
        await db_session.flush()
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.IMPORTING,
        )
        db_session.add(job)
        await db_session.flush()
        imported = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Flash Gordon - The 1995 Special",
            status=ImportSeriesStatus.CONFIRMED,
            cv_id=172262,
            cv_title="Flash Gordon: The 1995 Special",
            cv_issue_count=0,
            series_id=series.id,
            file_count=1,
        )
        db_session.add(imported)
        await db_session.flush()
        source = tmp_path / "Flash Gordon - The 1995 Special.cbr"
        source.write_text("comic")
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imported.id,
            file_path=str(source),
            file_name=source.name,
            file_size=source.stat().st_size,
            file_format="cbr",
            parsed_series="Flash Gordon - The 1995",
            parsed_issue_number=None,
            parsed_year=2026,
            status=ImportedFileStatus.MATCHED,
            match_confidence="medium",
            match_method="provider_zero_issue_single_issue",
            diagnostics={
                "kind": "provider_zero_issue_placeholder",
                "target_issue_number": 1.0,
                "target_issue_type": IssueType.SPECIAL.value,
                "target_issue_title": "Flash Gordon: The 1995 Special",
            },
        )
        db_session.add(imp_file)
        await db_session.flush()

        captured_issue_ids: list[int] = []

        async def _register_file(
            session: AsyncSession,
            _source_path: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **_: object,
        ) -> LibraryFile:
            assert confidence == MatchConfidence.MEDIUM
            assert issue_arg.id is not None
            captured_issue_ids.append(issue_arg.id)
            library_file = LibraryFile(
                file_path=str(tmp_path / "library" / source.name),
                file_name=source.name,
                file_size=source.stat().st_size,
                file_format=FileFormat.CBR,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=confidence,
                issue_id=issue_arg.id,
                library_root_id=root.id,
            )
            session.add(library_file)
            await session.flush()
            return library_file

        files_imported, files_failed = await process_import_series_files(
            db_session,
            job,
            imported,
            load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
            load_trash_dir=AsyncMock(return_value=tmp_path / ".trash"),
            load_ingest_policy=AsyncMock(return_value=object()),
            load_permission_policy=AsyncMock(return_value=object()),
            raise_if_cancelled=AsyncMock(),
            prepare_file=AsyncMock(
                return_value=SimpleNamespace(
                    registration_source=str(source),
                    original_source=source,
                    converted=False,
                )
            ),
            build_comicinfo_payload=AsyncMock(return_value={}),
            apply_comicinfo=lambda *_args, **_kwargs: None,
            cleanup_prepared_file=lambda *_args, **_kwargs: None,
            record_action=AsyncMock(),
            log_event=AsyncMock(),
            register_file=_register_file,
            move_to_trash=AsyncMock(),
        )

        await db_session.refresh(series)
        await db_session.refresh(imp_file)
        created_issue = await db_session.get(Issue, captured_issue_ids[0])
        assert files_imported == 1
        assert files_failed == 0
        assert created_issue is not None
        assert created_issue.series_id == series.id
        assert created_issue.issue_number == 1.0
        assert created_issue.comicvine_id is None
        assert created_issue.issue_type == IssueType.SPECIAL
        assert created_issue.title == "Flash Gordon: The 1995 Special"
        assert series.issue_count == 1
        assert imp_file.status == ImportedFileStatus.IMPORTED
        assert imp_file.matched_issue_id == created_issue.id

    @pytest.mark.asyncio
    async def test_missing_provider_issue_placeholder_creates_provisional_issue(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        from datetime import UTC, datetime

        from pullbox.models.library import FileFormat
        from pullbox.services.import_file_execution import process_import_series_files

        pub = Publisher(name="Zenescope Entertainment", comicvine_id=321)
        db_session.add(pub)
        await db_session.flush()
        series = Series(
            title="King Dracula",
            sort_title="king dracula",
            year_start=2025,
            comicvine_id=169964,
            publisher_id=pub.id,
            issue_count=3,
        )
        db_session.add(series)
        root = LibraryRoot(name="Comics", path=str(tmp_path / "library"), enabled=True)
        db_session.add(root)
        await db_session.flush()
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.IMPORTING,
        )
        db_session.add(job)
        await db_session.flush()
        imported = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="King Dracula",
            status=ImportSeriesStatus.CONFIRMED,
            cv_id=169964,
            cv_title="King Dracula",
            cv_issue_count=3,
            series_id=series.id,
            file_count=1,
        )
        db_session.add(imported)
        await db_session.flush()
        source = tmp_path / "King Dracula 04 (of 04) (2026).cbr"
        source.write_text("comic")
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imported.id,
            file_path=str(source),
            file_name=source.name,
            file_size=source.stat().st_size,
            file_format="cbr",
            parsed_series="King Dracula",
            parsed_issue_number=4.0,
            parsed_year=2026,
            status=ImportedFileStatus.MATCHED,
            match_confidence="manual",
            match_method="import_reconcile_provisional_issue",
            diagnostics={
                "kind": "provider_missing_issue_placeholder",
                "target_issue_number": 4.0,
                "target_issue_type": IssueType.ISSUE.value,
                "target_issue_title": None,
            },
        )
        db_session.add(imp_file)
        await db_session.flush()

        captured_issue_ids: list[int] = []
        progress_events: list[tuple[str, int, int, str, bool, int]] = []
        original_commit = db_session.commit
        commit_count = 0

        async def _register_file(
            session: AsyncSession,
            _source_path: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **_: object,
        ) -> LibraryFile:
            assert confidence == MatchConfidence.MEDIUM
            assert issue_arg.id is not None
            captured_issue_ids.append(issue_arg.id)
            library_file = LibraryFile(
                file_path=str(tmp_path / "library" / source.name),
                file_name=source.name,
                file_size=source.stat().st_size,
                file_format=FileFormat.CBR,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=confidence,
                issue_id=issue_arg.id,
                library_root_id=root.id,
            )
            session.add(library_file)
            await session.flush()
            return library_file

        async def _tracked_commit(*args: object, **kwargs: object) -> None:
            nonlocal commit_count
            commit_count += 1
            await original_commit(*args, **kwargs)

        async def _report_file_progress(
            *,
            imp_file: ImportedFile,
            file_index: int,
            total_files: int,
            stage: str,
            current: int,
            total: int,
            unit: str,
            live_only: bool = False,
        ) -> None:
            _ = imp_file, file_index, total_files
            progress_events.append((stage, current, total, unit, live_only, commit_count))

        with patch.object(db_session, "commit", side_effect=_tracked_commit):
            files_imported, files_failed = await process_import_series_files(
                db_session,
                job,
                imported,
                load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
                load_trash_dir=AsyncMock(return_value=tmp_path / ".trash"),
                load_ingest_policy=AsyncMock(return_value=object()),
                load_permission_policy=AsyncMock(return_value=object()),
                raise_if_cancelled=AsyncMock(),
                prepare_file=AsyncMock(
                    return_value=SimpleNamespace(
                        registration_source=str(source),
                        original_source=source,
                        converted=False,
                    )
                ),
                build_comicinfo_payload=AsyncMock(return_value={}),
                apply_comicinfo=lambda *_args, **_kwargs: None,
                cleanup_prepared_file=lambda *_args, **_kwargs: None,
                record_action=AsyncMock(),
                log_event=AsyncMock(),
                register_file=_register_file,
                move_to_trash=AsyncMock(),
                report_file_progress=_report_file_progress,
            )

        await db_session.refresh(series)
        await db_session.refresh(imp_file)
        created_issue = await db_session.get(Issue, captured_issue_ids[0])
        assert files_imported == 1
        assert files_failed == 0
        assert created_issue is not None
        assert created_issue.series_id == series.id
        assert created_issue.issue_number == 4.0
        assert created_issue.comicvine_id is None
        assert created_issue.issue_type == IssueType.ISSUE
        assert created_issue.metadata_source == "provisional_import"
        assert series.issue_count == 4
        assert imp_file.status == ImportedFileStatus.IMPORTED
        assert imp_file.matched_issue_id == created_issue.id
        precommit_progress_events = [event for event in progress_events if event[-1] == 0]
        assert precommit_progress_events
        assert all(event[4] is True for event in precommit_progress_events)
        assert progress_events[-1] == ("finalizing", 4, 4, "steps", False, 1)


class TestFileStatusSetToFailedOnError:
    """ImportedFile.status set to FAILED with error_message on exception."""

    @pytest.mark.asyncio
    async def test_file_failure_sets_status_and_error(self, db_session: AsyncSession) -> None:
        job, _imp_series, imp_files, series, _issues = await _setup_full_scenario(
            db_session, num_issues=1
        )
        mock_register = AsyncMock(side_effect=FileNotFoundError("Source file missing"))
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        await db_session.refresh(imp_files[0])
        assert imp_files[0].status == ImportedFileStatus.FAILED
        assert "Source file missing" in (imp_files[0].error_message or "")

    @pytest.mark.asyncio
    async def test_resource_safety_failure_sets_safety_blocked_not_failed(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        from pullbox.services.import_file_execution import process_import_series_files

        job, imp_series, imp_files, _series, _issues = await _setup_full_scenario(
            db_session,
            num_issues=1,
        )
        source = tmp_path / "The Joker - Endgame.pdf"
        source.write_bytes(b"%PDF-1.7 placeholder")
        imp_files[0].file_path = str(source)
        imp_files[0].file_name = source.name
        imp_files[0].file_format = "pdf"
        await db_session.commit()

        async def _prepare_file(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError(
                "Archive worker failed during convert: DecompressionBombError: "
                "Image size exceeds limit of 178956970 pixels"
            )

        files_imported, files_failed = await process_import_series_files(
            db_session,
            job,
            imp_series,
            load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
            load_trash_dir=AsyncMock(return_value=tmp_path / ".trash"),
            load_ingest_policy=AsyncMock(return_value=object()),
            load_permission_policy=AsyncMock(return_value=object()),
            raise_if_cancelled=AsyncMock(),
            prepare_file=_prepare_file,
            build_comicinfo_payload=AsyncMock(return_value={}),
            apply_comicinfo=lambda *_args, **_kwargs: None,
            cleanup_prepared_file=lambda *_args, **_kwargs: None,
            record_action=AsyncMock(),
            log_event=AsyncMock(),
            register_file=AsyncMock(),
            move_to_trash=AsyncMock(),
        )

        await db_session.refresh(imp_files[0])
        assert files_imported == 0
        assert files_failed == 0
        assert imp_files[0].status == ImportedFileStatus.SAFETY_BLOCKED
        assert imp_files[0].include_in_import is False
        assert imp_files[0].diagnostics["safety_block"]["kind"] == "pillow_decompression_bomb"
        assert "safe image processing limit" in (imp_files[0].error_message or "")

    @pytest.mark.asyncio
    async def test_post_placement_failure_restores_source_and_removes_library_artifact(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        from datetime import UTC, datetime

        from pullbox.models.library import FileFormat
        from pullbox.services.import_file_execution import process_import_series_files
        from pullbox.utilities.settings import move_file_to_utility_trash

        job, imp_series, imp_files, _series, _issues = await _setup_full_scenario(
            db_session,
            num_issues=1,
        )
        await db_session.commit()
        job = await db_session.get(ImportJob, job.id)
        assert job is not None
        imp_series = await db_session.get(ImportedSeries, imp_series.id)
        assert imp_series is not None
        imp_file = await db_session.get(ImportedFile, imp_files[0].id)
        assert imp_file is not None

        original_source = tmp_path / "imports" / "Henchgirl.expanded.Edition.pdf"
        registration_source = tmp_path / "work" / "Henchgirl (2020) TPB 01.cbz"
        destination_path = tmp_path / "library" / "Henchgirl (2020) TPB 01.cbz"
        trash_dir = tmp_path / ".trash"
        original_source.parent.mkdir(parents=True)
        registration_source.parent.mkdir(parents=True)
        destination_path.parent.mkdir(parents=True)
        original_source.write_text("original-pdf")
        registration_source.write_text("converted-cbz")

        async def _register_file(
            session: AsyncSession,
            source_path: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **_: object,
        ) -> LibraryFile:
            destination_path.write_text(source_path.read_text())
            library_file = LibraryFile(
                file_path=str(destination_path),
                file_name=destination_path.name,
                file_size=destination_path.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=confidence,
                issue_id=issue_arg.id,
                library_root_id=1,
            )
            session.add(library_file)
            await session.flush()
            return library_file

        files_imported, files_failed = await process_import_series_files(
            db_session,
            job,
            imp_series,
            load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
            load_trash_dir=AsyncMock(return_value=trash_dir),
            load_ingest_policy=AsyncMock(return_value=object()),
            load_permission_policy=AsyncMock(return_value=object()),
            raise_if_cancelled=AsyncMock(),
            prepare_file=AsyncMock(
                return_value=SimpleNamespace(
                    registration_source=str(registration_source),
                    original_source=original_source,
                    converted=True,
                )
            ),
            build_comicinfo_payload=AsyncMock(return_value={}),
            apply_comicinfo=lambda *_args, **_kwargs: None,
            cleanup_prepared_file=lambda *_args, **_kwargs: None,
            record_action=AsyncMock(side_effect=RuntimeError("journal write failed")),
            log_event=AsyncMock(),
            register_file=_register_file,
            move_to_trash=move_file_to_utility_trash,
        )

        await db_session.refresh(imp_file)

        assert files_imported == 0
        assert files_failed == 1
        assert imp_file.status == ImportedFileStatus.FAILED
        assert "journal write failed" in (imp_file.error_message or "")
        assert original_source.exists()
        assert not destination_path.exists()
        assert not any(trash_dir.rglob("Henchgirl.expanded.Edition.pdf"))


class TestImportExecutionAutoflushDiscipline:
    """Heavy file placement work should begin before import rows become dirty."""

    @pytest.mark.asyncio
    async def test_register_file_starts_before_imported_file_becomes_dirty(
        self,
        db_session: AsyncSession,
    ) -> None:
        from datetime import UTC, datetime

        from pullbox.models.library import FileFormat
        from pullbox.services.import_file_execution import process_import_series_files

        job, imp_series, imp_files, _series, issues = await _setup_full_scenario(
            db_session,
            num_issues=1,
        )
        imp_file = imp_files[0]
        issue = issues[0]
        imp_file.matched_issue_id = None
        await db_session.flush()
        observations: dict[str, object] = {}

        async def _register_file(
            session: AsyncSession,
            source_path: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **_: object,
        ) -> LibraryFile:
            observations["dirty_before_register"] = bool(
                session.is_modified(imp_file, include_collections=False)
                or imp_file in session.dirty
            )
            observations["matched_issue_id_before_register"] = imp_file.matched_issue_id
            library_file = LibraryFile(
                file_path=str(source_path),
                file_name=Path(source_path).name,
                file_size=1024,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=confidence,
                issue_id=issue_arg.id,
                library_root_id=1,
            )
            session.add(library_file)
            await session.flush()
            return library_file

        files_imported, files_failed = await process_import_series_files(
            db_session,
            job,
            imp_series,
            load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
            load_trash_dir=AsyncMock(return_value=Path("/tmp/pullbox-trash")),
            load_ingest_policy=AsyncMock(return_value=object()),
            load_permission_policy=AsyncMock(return_value=object()),
            raise_if_cancelled=AsyncMock(),
            prepare_file=AsyncMock(
                return_value=SimpleNamespace(
                    registration_source=imp_file.file_path,
                    original_source=Path(imp_file.file_path),
                    converted=False,
                )
            ),
            build_comicinfo_payload=AsyncMock(return_value={}),
            apply_comicinfo=lambda *_args, **_kwargs: None,
            cleanup_prepared_file=lambda *_args, **_kwargs: None,
            record_action=AsyncMock(),
            log_event=AsyncMock(),
            register_file=_register_file,
            move_to_trash=lambda *args, **kwargs: Path("/tmp/trash.cbz"),
        )

        assert files_imported == 1
        assert files_failed == 0
        assert observations["dirty_before_register"] is False
        assert observations["matched_issue_id_before_register"] is None
        assert imp_file.matched_issue_id == issue.id

    @pytest.mark.asyncio
    async def test_series_file_processing_keeps_comicinfo_payloads_source_specific(
        self,
        db_session: AsyncSession,
    ) -> None:
        from datetime import UTC, datetime

        from pullbox.models.library import FileFormat
        from pullbox.services.import_file_execution import process_import_series_files

        job, imp_series, imp_files, _series, issues = await _setup_full_scenario(
            db_session,
            num_issues=2,
        )
        job.update_embedded_comicinfo_from_match = True
        imp_files[1].matched_issue_id = issues[0].id
        await db_session.flush()

        ingest_policy = object()
        permission_policy = object()
        comicinfo_payloads = [
            {"Series": "Batman", "Number": "1", "PageCount": 24},
            {"Series": "Batman", "Number": "1", "PageCount": 36},
        ]
        seen_kwargs: list[dict[str, object]] = []

        async def _register_file(
            session: AsyncSession,
            source_path: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **kwargs: object,
        ) -> LibraryFile:
            seen_kwargs.append(
                {
                    "issue_id": issue_arg.id,
                    "ingest_policy": kwargs["ingest_policy"],
                    "permission_policy": kwargs["permission_policy"],
                    "loaded_issue": kwargs["loaded_issue"],
                    "comicinfo_payload": kwargs["comicinfo_payload"],
                    "update_embedded_comicinfo_from_match": kwargs[
                        "update_embedded_comicinfo_from_match"
                    ],
                }
            )
            library_file = LibraryFile(
                file_path=str(source_path),
                file_name=Path(source_path).name,
                file_size=1024,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=confidence,
                issue_id=issue_arg.id,
                library_root_id=1,
            )
            session.add(library_file)
            await session.flush()
            return library_file

        build_payload = AsyncMock(side_effect=comicinfo_payloads)
        load_ingest_policy = AsyncMock(return_value=ingest_policy)
        load_permission_policy = AsyncMock(return_value=permission_policy)

        files_imported, files_failed = await process_import_series_files(
            db_session,
            job,
            imp_series,
            load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
            load_trash_dir=AsyncMock(return_value=Path("/tmp/pullbox-trash")),
            load_ingest_policy=load_ingest_policy,
            load_permission_policy=load_permission_policy,
            raise_if_cancelled=AsyncMock(),
            prepare_file=AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        registration_source=imp_files[0].file_path,
                        original_source=Path(imp_files[0].file_path),
                        converted=False,
                    ),
                    SimpleNamespace(
                        registration_source=imp_files[1].file_path,
                        original_source=Path(imp_files[1].file_path),
                        converted=False,
                    ),
                ]
            ),
            build_comicinfo_payload=build_payload,
            apply_comicinfo=lambda *_args, **_kwargs: None,
            cleanup_prepared_file=lambda *_args, **_kwargs: None,
            record_action=AsyncMock(),
            log_event=AsyncMock(),
            register_file=_register_file,
            move_to_trash=lambda *args, **kwargs: Path("/tmp/trash.cbz"),
        )

        assert files_imported == 2
        assert files_failed == 0
        assert load_ingest_policy.await_count == 1
        assert load_permission_policy.await_count == 1
        assert build_payload.await_count == 2
        assert [call.kwargs["source_path"] for call in build_payload.await_args_list] == [
            Path(imp_files[0].file_path),
            Path(imp_files[1].file_path),
        ]
        assert [call["issue_id"] for call in seen_kwargs] == [issues[0].id, issues[0].id]
        assert all(call["ingest_policy"] is ingest_policy for call in seen_kwargs)
        assert all(call["permission_policy"] is permission_policy for call in seen_kwargs)
        assert all(call["loaded_issue"] is issues[0] for call in seen_kwargs)
        assert [call["comicinfo_payload"] for call in seen_kwargs] == comicinfo_payloads
        assert all(call["update_embedded_comicinfo_from_match"] is True for call in seen_kwargs)

    @pytest.mark.asyncio
    async def test_source_preserving_series_file_processing_does_not_adopt_source_folder(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        from datetime import UTC, datetime

        from pullbox.models.library import FileFormat
        from pullbox.services.import_file_execution import process_import_series_files

        library_root_path = tmp_path / "comics"
        source_folder = library_root_path / "Old Mylar Folder"
        target_folder = library_root_path / "Batman (2016)"
        source_folder.mkdir(parents=True)
        source_path = source_folder / "Batman 001.cbz"
        source_path.write_text("comic", encoding="utf-8")
        (source_folder / "series.json").write_text("mylar", encoding="utf-8")

        root = LibraryRoot(name="Comics", path=str(library_root_path), enabled=True)
        series = Series(
            title="Batman",
            sort_title="batman",
            year_start=2016,
            comicvine_id=97508,
            path=str(target_folder),
        )
        db_session.add_all([root, series])
        await db_session.flush()
        issue = Issue(
            series_id=series.id,
            issue_number=1.0,
            comicvine_id=100001,
            title="Issue #1",
            status=IssueStatus.WANTED,
        )
        job = ImportJob(
            source_path=str(library_root_path),
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.IMPORTING,
            target_library_root_id=root.id,
            move_to_library=True,
            transfer_method="move",
            effective_transfer_method="copy",
            source_preserved=True,
        )
        db_session.add_all([issue, job])
        await db_session.flush()
        imp_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            status=ImportSeriesStatus.CONFIRMED,
            series_id=series.id,
            source_folder=str(source_folder),
            file_count=1,
            files_total=1,
        )
        db_session.add(imp_series)
        await db_session.flush()
        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=imp_series.id,
            file_path=str(source_path),
            file_name=source_path.name,
            file_size=source_path.stat().st_size,
            file_format="cbz",
            parsed_series="Batman",
            parsed_issue_number=1.0,
            status=ImportedFileStatus.CONFIRMED,
            matched_issue_id=issue.id,
            match_confidence="high",
            include_in_import=True,
        )
        db_session.add(imp_file)
        await db_session.flush()

        async def _prepare_file(
            _session: AsyncSession,
            _job: ImportJob,
            current_file: ImportedFile,
            **_kwargs: object,
        ) -> SimpleNamespace:
            return SimpleNamespace(
                registration_source=current_file.file_path,
                original_source=Path(current_file.file_path),
                converted=False,
            )

        async def _register_file(
            session: AsyncSession,
            source: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **_kwargs: object,
        ) -> LibraryFile:
            assert source == source_path
            assert _kwargs["transfer_method"] == "copy"
            library_file = LibraryFile(
                file_path=str(target_folder / "Batman 001.cbz"),
                file_name="Batman 001.cbz",
                file_size=source.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=confidence,
                issue_id=issue_arg.id,
                library_root_id=root.id,
            )
            session.add(library_file)
            await session.flush()
            return library_file

        record_action = AsyncMock()

        files_imported, files_failed = await process_import_series_files(
            db_session,
            job,
            imp_series,
            load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
            load_trash_dir=AsyncMock(return_value=tmp_path / "trash"),
            load_ingest_policy=AsyncMock(return_value=SimpleNamespace(rename_on_import=True)),
            load_permission_policy=AsyncMock(return_value=SimpleNamespace(enabled=False)),
            raise_if_cancelled=AsyncMock(),
            prepare_file=_prepare_file,
            build_comicinfo_payload=AsyncMock(),
            apply_comicinfo=lambda *_args, **_kwargs: None,
            cleanup_prepared_file=lambda *_args, **_kwargs: None,
            record_action=record_action,
            log_event=AsyncMock(),
            register_file=_register_file,
            move_to_trash=lambda *args, **kwargs: Path("/tmp/trash.cbz"),
        )

        assert files_imported == 1
        assert files_failed == 0
        assert source_folder.exists()
        assert (source_folder / "series.json").exists()
        assert imp_file.file_path == str(source_path)
        assert [call.kwargs["action_type"] for call in record_action.await_args_list] == [
            "library_file_registered"
        ]

    @pytest.mark.asyncio
    async def test_import_logs_conversion_comicinfo_and_final_destination_name(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        from datetime import UTC, datetime

        from pullbox.models.library import FileFormat
        from pullbox.services.import_file_execution import process_import_series_files

        job, imp_series, imp_files, _series, _issues = await _setup_full_scenario(
            db_session,
            num_issues=1,
        )
        source_path = tmp_path / "Batman 001.cbr"
        source_path.write_text("source-cbr")
        converted_path = tmp_path / "staged" / "Batman 001.cbz"
        converted_path.parent.mkdir()
        converted_path.write_text("converted-cbz")
        final_path = tmp_path / "library" / "Batman (2016) #001.cbz"
        imp_files[0].file_path = str(source_path)
        imp_files[0].file_name = source_path.name
        imp_files[0].file_format = "cbr"
        job.convert_to_preferred_format = True
        job.update_embedded_comicinfo_from_match = True
        await db_session.flush()

        async def _register_file(
            session: AsyncSession,
            source: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **kwargs: object,
        ) -> LibraryFile:
            assert source == converted_path
            assert kwargs["comicinfo_payload"] == {"Series": "Batman", "Number": "1"}
            library_file = LibraryFile(
                file_path=str(final_path),
                file_name=final_path.name,
                file_size=converted_path.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=confidence,
                issue_id=issue_arg.id,
                library_root_id=1,
            )
            session.add(library_file)
            await session.flush()
            return library_file

        log_event = AsyncMock()

        files_imported, files_failed = await process_import_series_files(
            db_session,
            job,
            imp_series,
            load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
            load_trash_dir=AsyncMock(return_value=tmp_path / ".trash"),
            load_ingest_policy=AsyncMock(return_value=object()),
            load_permission_policy=AsyncMock(return_value=object()),
            raise_if_cancelled=AsyncMock(),
            prepare_file=AsyncMock(
                return_value=SimpleNamespace(
                    registration_source=converted_path,
                    original_source=source_path,
                    cleanup_paths=[converted_path.parent],
                    converted=True,
                )
            ),
            build_comicinfo_payload=AsyncMock(return_value={"Series": "Batman", "Number": "1"}),
            apply_comicinfo=lambda *_args, **_kwargs: None,
            cleanup_prepared_file=lambda *_args, **_kwargs: None,
            record_action=AsyncMock(),
            log_event=log_event,
            register_file=_register_file,
            move_to_trash=lambda *args, **kwargs: tmp_path / ".trash" / source_path.name,
        )

        assert files_imported == 1
        assert files_failed == 0
        events = [call.args[3] for call in log_event.await_args_list]
        assert events[-3:] == [
            "import_file_converted_to_cbz",
            "import_file_comicinfo_updated",
            "import_file_placed",
        ]
        messages = [call.kwargs["message"] for call in log_event.await_args_list]
        assert messages[-3:] == [
            "Converted to CBZ: Batman 001.cbz",
            "ComicInfo.xml written: Batman (2016) #001.cbz",
            "File placed: Batman (2016) #001.cbz",
        ]
        placed_call = log_event.await_args_list[-1]
        assert placed_call.kwargs["source_path"] == str(source_path)
        assert placed_call.kwargs["destination_path"] == str(final_path)
        assert placed_call.kwargs["destination_file_name"] == final_path.name

    @pytest.mark.asyncio
    async def test_import_builds_comicinfo_payload_from_prepared_archive_path(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        from datetime import UTC, datetime

        from pullbox.models.library import FileFormat
        from pullbox.services.import_file_execution import process_import_series_files

        job, imp_series, imp_files, _series, _issues = await _setup_full_scenario(
            db_session,
            num_issues=1,
        )
        prepared_path = tmp_path / "prepared.cbz"
        with zipfile.ZipFile(prepared_path, "w") as archive:
            archive.writestr("001.jpg", b"page one")
            archive.writestr("002.png", b"page two")
            archive.writestr("ComicInfo.xml", b"<ComicInfo />")
        final_path = tmp_path / "library" / "Batman (2016) #001.cbz"
        imp_files[0].file_path = str(prepared_path)
        imp_files[0].file_name = prepared_path.name
        job.update_embedded_comicinfo_from_match = True
        await db_session.flush()

        seen_payloads: list[dict[str, object]] = []

        async def _build_payload(
            session: AsyncSession,
            issue: Issue,
            *,
            source_path: Path | None = None,
        ) -> dict[str, object]:
            assert source_path == prepared_path
            return {"Series": "Batman", "PageCount": 2}

        async def _register_file(
            session: AsyncSession,
            source: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **kwargs: object,
        ) -> LibraryFile:
            assert source == prepared_path
            seen_payloads.append(dict(kwargs["comicinfo_payload"]))  # type: ignore[arg-type]
            library_file = LibraryFile(
                file_path=str(final_path),
                file_name=final_path.name,
                file_size=prepared_path.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=confidence,
                issue_id=issue_arg.id,
                library_root_id=1,
            )
            session.add(library_file)
            await session.flush()
            return library_file

        files_imported, files_failed = await process_import_series_files(
            db_session,
            job,
            imp_series,
            load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
            load_trash_dir=AsyncMock(return_value=tmp_path / ".trash"),
            load_ingest_policy=AsyncMock(return_value=object()),
            load_permission_policy=AsyncMock(return_value=object()),
            raise_if_cancelled=AsyncMock(),
            prepare_file=AsyncMock(
                return_value=SimpleNamespace(
                    registration_source=prepared_path,
                    original_source=prepared_path,
                    cleanup_paths=[],
                    converted=False,
                )
            ),
            build_comicinfo_payload=_build_payload,
            apply_comicinfo=lambda *_args, **_kwargs: None,
            cleanup_prepared_file=lambda *_args, **_kwargs: None,
            record_action=AsyncMock(),
            log_event=AsyncMock(),
            register_file=_register_file,
            move_to_trash=lambda *args, **kwargs: tmp_path / ".trash" / prepared_path.name,
        )

        assert files_imported == 1
        assert files_failed == 0
        assert seen_payloads == [{"Series": "Batman", "PageCount": 2}]

    @pytest.mark.asyncio
    async def test_import_marks_deferred_comicinfo_enrichment_after_file_placement(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        from datetime import UTC, datetime

        from pullbox.models.library import FileFormat
        from pullbox.services.import_file_execution import process_import_series_files

        job, imp_series, imp_files, _series, issues = await _setup_full_scenario(
            db_session,
            num_issues=1,
        )
        prepared_path = tmp_path / "Batman 001.cbz"
        prepared_path.write_text("prepared")
        final_path = tmp_path / "library" / "Batman (2016) #001.cbz"
        job.update_embedded_comicinfo_from_match = True
        await db_session.flush()

        async def _build_payload(
            session: AsyncSession,
            issue: Issue,
            *,
            source_path: Path | None = None,
            defer_issue_enrichment: bool = False,
        ) -> dict[str, object]:
            assert issue.id == issues[0].id
            assert source_path == prepared_path
            assert defer_issue_enrichment is True
            return {"Series": "Batman", "Number": "1"}

        async def _register_file(
            session: AsyncSession,
            source: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **kwargs: object,
        ) -> LibraryFile:
            assert kwargs["comicinfo_payload"] == {"Series": "Batman", "Number": "1"}
            library_file = LibraryFile(
                file_path=str(final_path),
                file_name=final_path.name,
                file_size=prepared_path.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=confidence,
                issue_id=issue_arg.id,
                library_root_id=1,
            )
            session.add(library_file)
            await session.flush()
            return library_file

        record_action = AsyncMock()
        log_event = AsyncMock()

        files_imported, files_failed = await process_import_series_files(
            db_session,
            job,
            imp_series,
            load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
            load_trash_dir=AsyncMock(return_value=tmp_path / ".trash"),
            load_ingest_policy=AsyncMock(return_value=object()),
            load_permission_policy=AsyncMock(return_value=object()),
            raise_if_cancelled=AsyncMock(),
            prepare_file=AsyncMock(
                return_value=SimpleNamespace(
                    registration_source=prepared_path,
                    original_source=prepared_path,
                    converted=False,
                )
            ),
            build_comicinfo_payload=_build_payload,
            apply_comicinfo=lambda *_args, **_kwargs: None,
            cleanup_prepared_file=lambda *_args, **_kwargs: None,
            record_action=record_action,
            log_event=log_event,
            register_file=_register_file,
            move_to_trash=lambda *args, **kwargs: tmp_path / ".trash" / prepared_path.name,
            defer_comicinfo_enrichment=True,
        )

        assert files_imported == 1
        assert files_failed == 0
        await db_session.refresh(imp_files[0])
        pending = imp_files[0].diagnostics["comicinfo_enrichment"]
        assert pending["status"] == "pending"
        assert pending["reason"] == "deferred_during_import"
        assert pending["issue_id"] == issues[0].id
        assert pending["issue_cv_id"] == issues[0].comicvine_id
        assert pending["library_file_id"] is not None
        payload = record_action.await_args.kwargs["payload"]
        assert payload["embedded_comicinfo_enrichment_deferred"] is True
        events = [call.args[3] for call in log_event.await_args_list]
        assert "import_file_comicinfo_enrichment_deferred" in events

    @pytest.mark.asyncio
    async def test_series_file_processing_uses_bounded_parallel_file_workers(
        self,
        async_engine,
        tmp_path: Path,
    ) -> None:
        import asyncio
        from datetime import UTC, datetime

        from sqlalchemy import select as sa_select
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from pullbox.models.library import FileFormat
        from pullbox.services.import_file_execution import process_import_series_files

        session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
        async with session_factory() as setup_session:
            job, imp_series, imp_files, _series, _issues = await _setup_full_scenario(
                setup_session,
                num_issues=2,
            )
            await setup_session.commit()
            job_id = job.id
            item_id = imp_series.id
            imported_file_ids = [imp_file.id for imp_file in imp_files]

        current_concurrency = 0
        max_concurrency = 0

        async def _prepare_file(
            session: AsyncSession,
            current_job: ImportJob,
            imp_file: ImportedFile,
            *,
            progress_callback=None,
        ) -> SimpleNamespace:
            _ = session, current_job, progress_callback
            nonlocal current_concurrency, max_concurrency
            current_concurrency += 1
            max_concurrency = max(max_concurrency, current_concurrency)
            try:
                await asyncio.sleep(0.05)
                source_path = tmp_path / imp_file.file_name
                source_path.write_text("prepared")
                return SimpleNamespace(
                    registration_source=source_path,
                    original_source=source_path,
                    converted=False,
                )
            finally:
                current_concurrency -= 1

        async def _register_file(
            session: AsyncSession,
            source: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **kwargs: object,
        ) -> LibraryFile:
            _ = kwargs
            final_path = tmp_path / "library" / source.name
            final_path.parent.mkdir(exist_ok=True)
            final_path.write_text("library")
            library_file = LibraryFile(
                file_path=str(final_path),
                file_name=final_path.name,
                file_size=final_path.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=confidence,
                issue_id=issue_arg.id,
                library_root_id=1,
            )
            session.add(library_file)
            await session.flush()
            return library_file

        adoption_mock = AsyncMock(return_value=True)
        async with session_factory() as session:
            job = await session.get(ImportJob, job_id)
            imp_series = await session.get(ImportedSeries, item_id)
            assert job is not None
            assert imp_series is not None
            with patch(
                "pullbox.services.import_file_execution.apply_import_series_folder_adoption",
                adoption_mock,
            ):
                files_imported, files_failed = await process_import_series_files(
                    session,
                    job,
                    imp_series,
                    load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
                    load_trash_dir=AsyncMock(return_value=tmp_path / ".trash"),
                    load_ingest_policy=AsyncMock(return_value=object()),
                    load_permission_policy=AsyncMock(return_value=object()),
                    raise_if_cancelled=AsyncMock(),
                    prepare_file=_prepare_file,
                    build_comicinfo_payload=AsyncMock(return_value={}),
                    apply_comicinfo=lambda *_args, **_kwargs: None,
                    cleanup_prepared_file=lambda *_args, **_kwargs: None,
                    record_action=AsyncMock(),
                    log_event=AsyncMock(),
                    register_file=_register_file,
                    move_to_trash=lambda *args, **kwargs: tmp_path / ".trash" / "source.cbz",
                    session_factory=session_factory,
                    file_worker_count=2,
                )
            await session.commit()

        assert files_imported == 2
        assert files_failed == 0
        assert max_concurrency == 2
        adoption_mock.assert_awaited_once()
        async with session_factory() as session:
            statuses = (
                (
                    await session.execute(
                        sa_select(ImportedFile.status)
                        .where(ImportedFile.id.in_(imported_file_ids))
                        .order_by(ImportedFile.id)
                    )
                )
                .scalars()
                .all()
            )
            imported_series = await session.get(ImportedSeries, item_id)
            assert statuses == [ImportedFileStatus.IMPORTED, ImportedFileStatus.IMPORTED]
            assert imported_series is not None
            assert imported_series.files_imported == 2
            assert imported_series.files_failed == 0

    @pytest.mark.asyncio
    async def test_parallel_processing_serializes_duplicate_targets_when_skipping_existing(
        self,
        async_engine,
        tmp_path: Path,
    ) -> None:
        import asyncio
        from datetime import UTC, datetime

        from sqlalchemy import func as sa_func
        from sqlalchemy import select as sa_select
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from pullbox.models.library import FileFormat
        from pullbox.services.import_file_execution import process_import_series_files

        session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
        async with session_factory() as setup_session:
            job, imp_series, imp_files, _series, issues = await _setup_full_scenario(
                setup_session,
                num_issues=2,
            )
            imp_files[1].matched_issue_id = issues[0].id
            imp_files[1].matched_issue_cv_id = issues[0].comicvine_id
            imp_files[1].parsed_issue_number = issues[0].issue_number
            await setup_session.commit()
            job_id = job.id
            item_id = imp_series.id
            duplicate_issue_id = issues[0].id
            imported_file_ids = [imp_file.id for imp_file in imp_files]

        current_concurrency = 0
        max_concurrency = 0

        async def _prepare_file(
            session: AsyncSession,
            current_job: ImportJob,
            imp_file: ImportedFile,
            *,
            progress_callback=None,
        ) -> SimpleNamespace:
            _ = session, current_job, progress_callback
            nonlocal current_concurrency, max_concurrency
            current_concurrency += 1
            max_concurrency = max(max_concurrency, current_concurrency)
            try:
                await asyncio.sleep(0.05)
                source_path = tmp_path / imp_file.file_name
                source_path.write_text("prepared")
                return SimpleNamespace(
                    registration_source=source_path,
                    original_source=source_path,
                    converted=False,
                )
            finally:
                current_concurrency -= 1

        async def _register_file(
            session: AsyncSession,
            source: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **kwargs: object,
        ) -> LibraryFile:
            _ = kwargs
            final_path = tmp_path / "library" / source.name
            final_path.parent.mkdir(exist_ok=True)
            final_path.write_text("library")
            library_file = LibraryFile(
                file_path=str(final_path),
                file_name=final_path.name,
                file_size=final_path.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=confidence,
                issue_id=issue_arg.id,
                library_root_id=1,
            )
            session.add(library_file)
            await session.flush()
            return library_file

        async with session_factory() as session:
            job = await session.get(ImportJob, job_id)
            imp_series = await session.get(ImportedSeries, item_id)
            assert job is not None
            assert imp_series is not None
            files_imported, files_failed = await process_import_series_files(
                session,
                job,
                imp_series,
                load_media_settings=AsyncMock(return_value={"skip_existing_files": "true"}),
                load_trash_dir=AsyncMock(return_value=tmp_path / ".trash"),
                load_ingest_policy=AsyncMock(return_value=object()),
                load_permission_policy=AsyncMock(return_value=object()),
                raise_if_cancelled=AsyncMock(),
                prepare_file=_prepare_file,
                build_comicinfo_payload=AsyncMock(return_value={}),
                apply_comicinfo=lambda *_args, **_kwargs: None,
                cleanup_prepared_file=lambda *_args, **_kwargs: None,
                record_action=AsyncMock(),
                log_event=AsyncMock(),
                register_file=_register_file,
                move_to_trash=lambda *args, **kwargs: tmp_path / ".trash" / "source.cbz",
                session_factory=session_factory,
                file_worker_count=2,
            )
            await session.commit()

        assert files_imported == 1
        assert files_failed == 0
        assert max_concurrency == 1
        async with session_factory() as session:
            statuses = (
                (
                    await session.execute(
                        sa_select(ImportedFile.status)
                        .where(ImportedFile.id.in_(imported_file_ids))
                        .order_by(ImportedFile.id)
                    )
                )
                .scalars()
                .all()
            )
            library_file_count = await session.scalar(
                sa_select(sa_func.count())
                .select_from(LibraryFile)
                .where(LibraryFile.issue_id == duplicate_issue_id)
            )
            imported_series = await session.get(ImportedSeries, item_id)
            assert statuses == [ImportedFileStatus.IMPORTED, ImportedFileStatus.SKIPPED]
            assert library_file_count == 1
            assert imported_series is not None
            assert imported_series.files_imported == 1
            assert imported_series.files_failed == 0

    @pytest.mark.asyncio
    async def test_parallel_processing_serializes_duplicate_issue_targets_without_skip_existing(
        self,
        async_engine,
        tmp_path: Path,
    ) -> None:
        import asyncio
        from datetime import UTC, datetime

        from sqlalchemy import select as sa_select
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from pullbox.models.library import FileFormat
        from pullbox.services.import_file_execution import process_import_series_files

        session_factory = async_sessionmaker(async_engine, expire_on_commit=False)
        async with session_factory() as setup_session:
            job, imp_series, imp_files, _series, issues = await _setup_full_scenario(
                setup_session,
                num_issues=2,
            )
            imp_files[1].matched_issue_id = issues[0].id
            imp_files[1].matched_issue_cv_id = issues[0].comicvine_id
            imp_files[1].parsed_issue_number = issues[0].issue_number
            await setup_session.commit()
            job_id = job.id
            item_id = imp_series.id
            imported_file_ids = [imp_file.id for imp_file in imp_files]

        current_concurrency = 0
        max_concurrency = 0

        async def _prepare_file(
            session: AsyncSession,
            current_job: ImportJob,
            imp_file: ImportedFile,
            *,
            progress_callback=None,
        ) -> SimpleNamespace:
            _ = session, current_job, progress_callback
            nonlocal current_concurrency, max_concurrency
            current_concurrency += 1
            max_concurrency = max(max_concurrency, current_concurrency)
            try:
                await asyncio.sleep(0.05)
                source_path = tmp_path / imp_file.file_name
                source_path.write_text("prepared")
                return SimpleNamespace(
                    registration_source=source_path,
                    original_source=source_path,
                    converted=False,
                )
            finally:
                current_concurrency -= 1

        async def _register_file(
            session: AsyncSession,
            source: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **kwargs: object,
        ) -> LibraryFile:
            _ = kwargs
            final_path = tmp_path / "library" / source.name
            final_path.parent.mkdir(exist_ok=True)
            final_path.write_text("library")
            library_file = LibraryFile(
                file_path=str(final_path),
                file_name=final_path.name,
                file_size=final_path.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=confidence,
                issue_id=issue_arg.id,
                library_root_id=1,
            )
            session.add(library_file)
            await session.flush()
            return library_file

        async with session_factory() as session:
            job = await session.get(ImportJob, job_id)
            imp_series = await session.get(ImportedSeries, item_id)
            assert job is not None
            assert imp_series is not None
            files_imported, files_failed = await process_import_series_files(
                session,
                job,
                imp_series,
                load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
                load_trash_dir=AsyncMock(return_value=tmp_path / ".trash"),
                load_ingest_policy=AsyncMock(return_value=object()),
                load_permission_policy=AsyncMock(return_value=object()),
                raise_if_cancelled=AsyncMock(),
                prepare_file=_prepare_file,
                build_comicinfo_payload=AsyncMock(return_value={}),
                apply_comicinfo=lambda *_args, **_kwargs: None,
                cleanup_prepared_file=lambda *_args, **_kwargs: None,
                record_action=AsyncMock(),
                log_event=AsyncMock(),
                register_file=_register_file,
                move_to_trash=lambda *args, **kwargs: tmp_path / ".trash" / "source.cbz",
                session_factory=session_factory,
                file_worker_count=2,
            )
            await session.commit()

        assert files_imported == 2
        assert files_failed == 0
        assert max_concurrency == 1
        async with session_factory() as session:
            statuses = (
                (
                    await session.execute(
                        sa_select(ImportedFile.status)
                        .where(ImportedFile.id.in_(imported_file_ids))
                        .order_by(ImportedFile.id)
                    )
                )
                .scalars()
                .all()
            )
            imported_series = await session.get(ImportedSeries, item_id)
            assert statuses == [ImportedFileStatus.IMPORTED, ImportedFileStatus.IMPORTED]
            assert imported_series is not None
            assert imported_series.files_imported == 2
            assert imported_series.files_failed == 0

    @pytest.mark.asyncio
    async def test_series_file_processing_emits_comicinfo_metadata_heartbeat(
        self,
        db_session: AsyncSession,
        tmp_path: Path,
    ) -> None:
        import asyncio
        from datetime import UTC, datetime

        from pullbox.models.library import FileFormat
        from pullbox.services.import_file_execution import process_import_series_files

        job, imp_series, imp_files, _series, _issues = await _setup_full_scenario(
            db_session,
            num_issues=1,
        )
        prepared_path = tmp_path / "Batman 001.cbz"
        with zipfile.ZipFile(prepared_path, "w") as archive:
            archive.writestr("001.jpg", b"page one")
        imp_files[0].file_path = str(prepared_path)
        imp_files[0].file_name = prepared_path.name
        job.update_embedded_comicinfo_from_match = True
        await db_session.flush()

        progress_events: list[tuple[str, int, int, str, bool]] = []

        async def _build_payload(
            session: AsyncSession,
            issue: Issue,
            *,
            source_path: Path | None = None,
        ) -> dict[str, object]:
            _ = session, issue, source_path
            assert progress_events[-1] == (
                "comicinfo_metadata",
                0,
                1,
                "steps",
                True,
            )
            await asyncio.sleep(0.03)
            return {"Series": "Batman", "PageCount": 1}

        async def _register_file(
            session: AsyncSession,
            source: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **kwargs: object,
        ) -> LibraryFile:
            assert kwargs["comicinfo_payload"] == {"Series": "Batman", "PageCount": 1}
            library_file = LibraryFile(
                file_path=str(source),
                file_name=source.name,
                file_size=prepared_path.stat().st_size,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=confidence,
                issue_id=issue_arg.id,
                library_root_id=1,
            )
            session.add(library_file)
            await session.flush()
            return library_file

        async def _report_file_progress(
            *,
            imp_file: ImportedFile,
            file_index: int,
            total_files: int,
            stage: str,
            current: int,
            total: int,
            unit: str,
            live_only: bool = False,
        ) -> None:
            _ = imp_file, file_index, total_files
            progress_events.append((stage, current, total, unit, live_only))

        with patch(
            "pullbox.services.import_file_execution._COMICINFO_METADATA_PROGRESS_HEARTBEAT_SECONDS",
            0.01,
        ):
            files_imported, files_failed = await process_import_series_files(
                db_session,
                job,
                imp_series,
                load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
                load_trash_dir=AsyncMock(return_value=tmp_path / ".trash"),
                load_ingest_policy=AsyncMock(return_value=object()),
                load_permission_policy=AsyncMock(return_value=object()),
                raise_if_cancelled=AsyncMock(),
                prepare_file=AsyncMock(
                    return_value=SimpleNamespace(
                        registration_source=prepared_path,
                        original_source=prepared_path,
                        cleanup_paths=[],
                        converted=False,
                    )
                ),
                build_comicinfo_payload=_build_payload,
                apply_comicinfo=lambda *_args, **_kwargs: None,
                cleanup_prepared_file=lambda *_args, **_kwargs: None,
                record_action=AsyncMock(),
                log_event=AsyncMock(),
                register_file=_register_file,
                move_to_trash=lambda *args, **kwargs: tmp_path / ".trash" / prepared_path.name,
                report_file_progress=_report_file_progress,
            )

        assert files_imported == 1
        assert files_failed == 0
        metadata_events = [event for event in progress_events if event[0] == "comicinfo_metadata"]
        assert len(metadata_events) >= 2
        assert all(event[4] is True for event in metadata_events)
        assert metadata_events[0] == ("comicinfo_metadata", 0, 1, "steps", True)
        assert metadata_events[-1] == ("comicinfo_metadata", 1, 1, "steps", True)

    @pytest.mark.asyncio
    async def test_series_file_processing_emits_finalizing_stage_after_file_work(
        self,
        db_session: AsyncSession,
    ) -> None:
        from datetime import UTC, datetime

        from pullbox.models.library import FileFormat
        from pullbox.services.import_file_execution import process_import_series_files

        job, imp_series, imp_files, _series, _issues = await _setup_full_scenario(
            db_session,
            num_issues=1,
        )
        job.update_embedded_comicinfo_from_match = True
        await db_session.flush()

        progress_events: list[tuple[str, int, int, str, bool, int]] = []
        original_commit = db_session.commit
        commit_count = 0

        async def _register_file(
            session: AsyncSession,
            source_path: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **_kwargs: object,
        ) -> LibraryFile:
            library_file = LibraryFile(
                file_path=str(source_path),
                file_name=Path(source_path).name,
                file_size=1024,
                file_format=FileFormat.CBZ,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=confidence,
                issue_id=issue_arg.id,
                library_root_id=1,
            )
            session.add(library_file)
            await session.flush()
            return library_file

        async def _tracked_commit(*args: object, **kwargs: object) -> None:
            nonlocal commit_count
            commit_count += 1
            await original_commit(*args, **kwargs)

        async def _report_file_progress(
            *,
            imp_file: ImportedFile,
            file_index: int,
            total_files: int,
            stage: str,
            current: int,
            total: int,
            unit: str,
            live_only: bool = False,
        ) -> None:
            _ = imp_file, file_index, total_files
            progress_events.append((stage, current, total, unit, live_only, commit_count))

        with patch.object(db_session, "commit", side_effect=_tracked_commit):
            files_imported, files_failed = await process_import_series_files(
                db_session,
                job,
                imp_series,
                load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
                load_trash_dir=AsyncMock(return_value=Path("/tmp/pullbox-trash")),
                load_ingest_policy=AsyncMock(return_value=object()),
                load_permission_policy=AsyncMock(return_value=object()),
                raise_if_cancelled=AsyncMock(),
                prepare_file=AsyncMock(
                    return_value=SimpleNamespace(
                        registration_source=imp_files[0].file_path,
                        original_source=Path(imp_files[0].file_path),
                        converted=False,
                    )
                ),
                build_comicinfo_payload=AsyncMock(return_value={"Series": "Batman", "Number": "1"}),
                apply_comicinfo=lambda *_args, **_kwargs: None,
                cleanup_prepared_file=lambda *_args, **_kwargs: None,
                record_action=AsyncMock(),
                log_event=AsyncMock(),
                register_file=_register_file,
                move_to_trash=lambda *args, **kwargs: Path("/tmp/trash.cbz"),
                report_file_progress=_report_file_progress,
            )

        assert files_imported == 1
        assert files_failed == 0
        finalizing_events = [event for event in progress_events if event[0] == "finalizing"]
        assert finalizing_events == [
            ("finalizing", 0, 4, "steps", True, 0),
            ("finalizing", 1, 4, "steps", True, 0),
            ("finalizing", 2, 4, "steps", True, 0),
            ("finalizing", 3, 4, "steps", True, 1),
            ("finalizing", 4, 4, "steps", False, 1),
        ]


class TestOneFileFailsOthersContinue:
    """One file fails but others in the series still get processed."""

    @pytest.mark.asyncio
    async def test_partial_file_failure(self, db_session: AsyncSession) -> None:
        job, _imp_series, imp_files, series, _issues = await _setup_full_scenario(
            db_session, num_issues=3
        )
        # First call fails, second and third succeed
        ok_mock = _mock_register_library_file()
        call_count = 0

        async def _side_effect(*args: object, **kwargs: object) -> LibraryFile:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("Disk full")
            return await ok_mock(*args, **kwargs)

        mock_register = AsyncMock(side_effect=_side_effect)
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        await db_session.refresh(imp_files[0])
        await db_session.refresh(imp_files[1])
        await db_session.refresh(imp_files[2])
        assert imp_files[0].status == ImportedFileStatus.FAILED
        assert imp_files[1].status == ImportedFileStatus.IMPORTED
        assert imp_files[2].status == ImportedFileStatus.IMPORTED

    @pytest.mark.asyncio
    async def test_file_processing_reloads_future_files_and_issues_after_rollback(
        self,
        db_session: AsyncSession,
    ) -> None:
        from pullbox.services import import_file_execution as file_execution

        job, _imp_series, imp_files, series, issues = await _setup_full_scenario(
            db_session,
            num_issues=2,
        )
        for imp_file in imp_files:
            imp_file.matched_issue_id = None
        await db_session.flush()
        issue_ids = [issue.id for issue in issues]

        loaded_file_ids: list[int] = []
        loaded_issue_ids: list[int] = []
        original_load_file = file_execution._load_imported_file_for_processing
        original_load_issue = file_execution._load_issue_for_processing

        async def _tracked_load_file(session: AsyncSession, imported_file_id: int):
            loaded_file_ids.append(imported_file_id)
            return await original_load_file(session, imported_file_id)

        async def _tracked_load_issue(session: AsyncSession, issue_id: int):
            loaded_issue_ids.append(issue_id)
            return await original_load_issue(session, issue_id)

        ok_mock = _mock_register_library_file()
        register_call_count = 0

        async def _side_effect(*args: object, **kwargs: object) -> LibraryFile:
            nonlocal register_call_count
            register_call_count += 1
            if register_call_count == 1:
                raise OSError("Disk full")
            assert kwargs["loaded_issue"] is not None
            assert kwargs["loaded_issue"].id == issue_ids[1]
            return await ok_mock(*args, **kwargs)

        mock_register = AsyncMock(side_effect=_side_effect)
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with (
            patch.object(
                file_execution,
                "_load_imported_file_for_processing",
                side_effect=_tracked_load_file,
            ),
            patch.object(
                file_execution,
                "_load_issue_for_processing",
                side_effect=_tracked_load_issue,
            ),
            patch(
                "pullbox.services.import_service.register_library_file",
                mock_register,
            ),
        ):
            await svc.run_import(db_session, job.id)

        await db_session.refresh(imp_files[0])
        await db_session.refresh(imp_files[1])
        assert imp_files[0].status == ImportedFileStatus.FAILED
        assert imp_files[1].status == ImportedFileStatus.IMPORTED
        assert loaded_file_ids == [imp_files[0].id, imp_files[1].id]
        assert loaded_issue_ids == issue_ids


class TestSkippedFilesNotProcessed:
    """SKIPPED files are not passed to register_library_file()."""

    @pytest.mark.asyncio
    async def test_skipped_files_ignored(self, db_session: AsyncSession) -> None:
        job, _imp_series, imp_files, series, _issues = await _setup_full_scenario(
            db_session,
            num_issues=3,
            file_statuses=[
                ImportedFileStatus.CONFIRMED,
                ImportedFileStatus.SKIPPED,
                ImportedFileStatus.CONFIRMED,
            ],
        )
        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        # Only 2 calls (file 1 and 3), not 3
        assert mock_register.call_count == 2

        # SKIPPED file stays SKIPPED
        await db_session.refresh(imp_files[1])
        assert imp_files[1].status == ImportedFileStatus.SKIPPED


class TestNoMatchFilesNotProcessed:
    """NO_MATCH files are not passed to register_library_file()."""

    @pytest.mark.asyncio
    async def test_no_match_files_ignored(self, db_session: AsyncSession) -> None:
        job, _imp_series, imp_files, series, _issues = await _setup_full_scenario(
            db_session,
            num_issues=2,
            file_statuses=[
                ImportedFileStatus.CONFIRMED,
                ImportedFileStatus.NO_MATCH,
            ],
        )
        # NO_MATCH file has no matched_issue_id
        imp_files[1].matched_issue_id = None
        await db_session.flush()

        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        assert mock_register.call_count == 1

        await db_session.refresh(imp_files[1])
        assert imp_files[1].status == ImportedFileStatus.NO_MATCH


class TestFileCountersUpdatedOnSeries:
    """File counters on ImportedSeries are updated after import."""

    @pytest.mark.asyncio
    async def test_series_file_counters(self, db_session: AsyncSession) -> None:
        job, imp_series, _imp_files, series, _issues = await _setup_full_scenario(
            db_session, num_issues=3
        )
        # Make first call fail
        ok_mock = _mock_register_library_file()
        call_count = 0

        async def _side_effect(*args: object, **kwargs: object) -> LibraryFile:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("Disk full")
            return await ok_mock(*args, **kwargs)

        mock_register = AsyncMock(side_effect=_side_effect)
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        await db_session.refresh(imp_series)
        assert imp_series.files_imported == 2
        assert imp_series.files_failed == 1


class TestJobFileCountersUpdated:
    """File counters on ImportJob are updated after import."""

    @pytest.mark.asyncio
    async def test_job_file_counters(self, db_session: AsyncSession) -> None:
        job, _imp_series, _imp_files, series, _issues = await _setup_full_scenario(
            db_session, num_issues=2
        )
        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        await db_session.refresh(job)
        assert job.total_files_imported == 2
        assert job.total_files_failed == 0


class TestMoveToLibraryPassedThrough:
    """move_to_library flag is passed to register_library_file."""

    @pytest.mark.asyncio
    async def test_move_to_library_true(self, db_session: AsyncSession) -> None:
        job, _imp_series, _imp_files, series, _issues = await _setup_full_scenario(
            db_session, num_issues=1
        )
        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        # Default is move_to_library=True
        call_kwargs = mock_register.call_args_list[0].kwargs
        assert call_kwargs.get("move_to_library") is True

    @pytest.mark.asyncio
    async def test_import_execution_disables_nested_normalization_but_keeps_metadata_update(
        self,
        db_session: AsyncSession,
    ) -> None:
        job, _imp_series, _imp_files, series, _issues = await _setup_full_scenario(
            db_session, num_issues=1
        )
        job.convert_to_preferred_format = True
        job.update_embedded_comicinfo_from_match = True
        await db_session.flush()

        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        call_kwargs = mock_register.call_args_list[0].kwargs
        assert call_kwargs.get("normalize_to_cbz") is False
        assert call_kwargs.get("update_embedded_comicinfo_from_match") is True

    @pytest.mark.asyncio
    async def test_import_execution_skips_embedded_metadata_when_pdf_safety_fallback_triggers(
        self,
        db_session: AsyncSession,
    ) -> None:
        from pullbox.services.import_file_execution import process_import_series_files

        job, imp_series, imp_files, _series, _issues = await _setup_full_scenario(
            db_session, num_issues=1
        )
        job.update_embedded_comicinfo_from_match = True
        await db_session.flush()

        log_event = AsyncMock()
        seen_kwargs: list[dict[str, object]] = []

        async def _register_file(
            session: AsyncSession,
            source_path: Path,
            issue_arg: Issue,
            confidence: MatchConfidence,
            **kwargs: object,
        ) -> LibraryFile:
            from datetime import UTC, datetime

            from pullbox.models.library import FileFormat

            seen_kwargs.append(kwargs)
            library_file = LibraryFile(
                file_path=str(source_path),
                file_name=Path(source_path).name,
                file_size=1024,
                file_format=FileFormat.PDF,
                file_modified_at=datetime.now(tz=UTC),
                match_confidence=confidence,
                issue_id=issue_arg.id,
                library_root_id=1,
            )
            session.add(library_file)
            await session.flush()
            return library_file

        files_imported, files_failed = await process_import_series_files(
            db_session,
            job,
            imp_series,
            load_media_settings=AsyncMock(return_value={"skip_existing_files": "false"}),
            load_trash_dir=AsyncMock(return_value=Path("/tmp/pullbox-trash")),
            load_ingest_policy=AsyncMock(return_value=object()),
            load_permission_policy=AsyncMock(return_value=object()),
            raise_if_cancelled=AsyncMock(),
            prepare_file=AsyncMock(
                return_value=SimpleNamespace(
                    registration_source=imp_files[0].file_path,
                    original_source=Path(imp_files[0].file_path),
                    converted=False,
                    skip_embedded_comicinfo=True,
                    preparation_warning=(
                        "Skipped PDF normalization for issue.pdf because it exceeded safe "
                        "rasterization limits; importing the original PDF and skipping "
                        "embedded ComicInfo update."
                    ),
                )
            ),
            build_comicinfo_payload=AsyncMock(return_value={"Series": "Batman", "Number": "1"}),
            apply_comicinfo=lambda *_args, **_kwargs: None,
            cleanup_prepared_file=lambda *_args, **_kwargs: None,
            record_action=AsyncMock(),
            log_event=log_event,
            register_file=_register_file,
            move_to_trash=lambda *args, **kwargs: Path("/tmp/trash.cbz"),
        )

        assert files_imported == 1
        assert files_failed == 0
        assert seen_kwargs
        assert seen_kwargs[0]["update_embedded_comicinfo_from_match"] is False
        assert seen_kwargs[0]["comicinfo_payload"] is None
        warning_calls = [
            call
            for call in log_event.await_args_list
            if call.args[3] == "import_file_normalization_skipped_for_safety"
        ]
        assert len(warning_calls) == 1


class TestProgressEventsIncludeFileStats:
    """Progress events emitted during import include file-level stats."""

    @pytest.mark.asyncio
    async def test_progress_has_file_counters(self, db_session: AsyncSession) -> None:
        job, _imp_series, _imp_files, series, _issues = await _setup_full_scenario(
            db_session, num_issues=2
        )
        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        callback = AsyncMock()
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id, progress_callback=callback)

        assert callback.call_count >= 1
        last_event = callback.call_args_list[-1][0][0]
        assert last_event.total_files_imported is not None
        assert last_event.total_files_imported >= 0


class TestConfidenceMapping:
    """Match confidence string is mapped to MatchConfidence enum."""

    @pytest.mark.asyncio
    async def test_high_confidence_maps_correctly(self, db_session: AsyncSession) -> None:
        job, _imp_series, _imp_files, series, _issues = await _setup_full_scenario(
            db_session, num_issues=1
        )
        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        call_args = mock_register.call_args_list[0]
        confidence_arg = (
            call_args[0][3] if len(call_args[0]) > 3 else call_args.kwargs.get("confidence")
        )
        assert confidence_arg == MatchConfidence.HIGH


class TestDuplicateSeriesSkipsCreation:
    """Duplicate series (already in library) skips add_from_comicvine."""

    @pytest.mark.asyncio
    async def test_existing_series_files_still_registered(self, db_session: AsyncSession) -> None:
        """When imp_series already has a series_id, skip series creation but
        still register files."""
        job, imp_series, imp_files, series, _issues = await _setup_full_scenario(
            db_session,
            num_issues=1,
            series_status=ImportSeriesStatus.CONFIRMED,
        )
        # The series_id is already set — duplicate scenario
        imp_series.status = ImportSeriesStatus.CONFIRMED
        await db_session.flush()

        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        # add_from_comicvine returns the same series (simulating existing)
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        # File should still be registered
        assert mock_register.call_count == 1
        await db_session.refresh(imp_files[0])
        assert imp_files[0].status == ImportedFileStatus.IMPORTED


class TestMediumConfidenceMapping:
    """Medium confidence string maps to MatchConfidence.MEDIUM."""

    @pytest.mark.asyncio
    async def test_medium_confidence(self, db_session: AsyncSession) -> None:
        job, _imp_series, imp_files, series, _issues = await _setup_full_scenario(
            db_session, num_issues=1
        )
        imp_files[0].match_confidence = "medium"
        await db_session.flush()

        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        call_args = mock_register.call_args_list[0]
        confidence_arg = (
            call_args[0][3] if len(call_args[0]) > 3 else call_args.kwargs.get("confidence")
        )
        assert confidence_arg == MatchConfidence.MEDIUM


class TestFileWithNoMatchedIssueSkipped:
    """A MATCHED file with no resolvable issue is skipped gracefully."""

    @pytest.mark.asyncio
    async def test_no_matched_issue_skipped(self, db_session: AsyncSession) -> None:
        job, _imp_series, imp_files, series, _issues = await _setup_full_scenario(
            db_session, num_issues=1
        )
        # Clear all match identifiers so the file cannot be resolved
        imp_files[0].matched_issue_id = None
        imp_files[0].parsed_issue_number = None
        imp_files[0].comicvine_issue_id = None
        await db_session.flush()

        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        assert mock_register.call_count == 0


class TestSeriesFailureDoesNotProcessFiles:
    """If series creation fails, its files are not processed."""

    @pytest.mark.asyncio
    async def test_series_failure_skips_files(self, db_session: AsyncSession) -> None:
        job, imp_series, _imp_files, _series, _issues = await _setup_full_scenario(
            db_session, num_issues=2
        )
        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.side_effect = Exception("CV API down")
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        # No files should be registered because the series import failed
        assert mock_register.call_count == 0

        await db_session.refresh(imp_series)
        assert imp_series.status == ImportSeriesStatus.FAILED


class TestPendingFilesNotProcessed:
    """PENDING files are not passed to register_library_file()."""

    @pytest.mark.asyncio
    async def test_pending_files_ignored(self, db_session: AsyncSession) -> None:
        job, _imp_series, imp_files, series, _issues = await _setup_full_scenario(
            db_session,
            num_issues=2,
            file_statuses=[
                ImportedFileStatus.CONFIRMED,
                ImportedFileStatus.PENDING,
            ],
        )
        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        assert mock_register.call_count == 1

        await db_session.refresh(imp_files[1])
        assert imp_files[1].status == ImportedFileStatus.PENDING


class TestConflictFilesNotProcessedUnlessConfirmed:
    """CONFLICT files are only processed if promoted to CONFIRMED."""

    @pytest.mark.asyncio
    async def test_conflict_files_skipped(self, db_session: AsyncSession) -> None:
        job, _imp_series, _imp_files, series, _issues = await _setup_full_scenario(
            db_session,
            num_issues=2,
            file_statuses=[
                ImportedFileStatus.CONFIRMED,
                ImportedFileStatus.CONFLICT,
            ],
        )
        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        assert mock_register.call_count == 1


class TestSourcePathPassedCorrectly:
    """Source path from ImportedFile.file_path is passed as Path object."""

    @pytest.mark.asyncio
    async def test_source_path_is_path_object(self, db_session: AsyncSession) -> None:
        job, _imp_series, _imp_files, series, _issues = await _setup_full_scenario(
            db_session, num_issues=1
        )
        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        call_args = mock_register.call_args_list[0]
        source_path_arg = call_args[0][1]
        assert isinstance(source_path_arg, Path)
        assert str(source_path_arg) == "/tmp/comics/Batman 001.cbz"


class TestConfirmImportAppliesConflictResolutions:
    """confirm_import() applies conflict_resolutions before transitioning."""

    @pytest.mark.asyncio
    async def test_unresolved_conflicts_cannot_be_confirmed(self, db_session: AsyncSession) -> None:
        _job, imp_series, imp_files, _series, issues = await _setup_full_scenario(
            db_session,
            num_issues=2,
            job_status=ImportJobStatus.REVIEW,
            series_status=ImportSeriesStatus.MATCHED,
            file_statuses=[
                ImportedFileStatus.CONFLICT,
                ImportedFileStatus.CONFLICT,
            ],
        )
        imp_series.files_total = 2
        imp_series.files_conflict = 2
        imp_files[0].conflict_group_id = 1
        imp_files[0].is_preferred = True
        imp_files[1].conflict_group_id = 1
        imp_files[1].is_preferred = False
        imp_files[1].matched_issue_id = issues[0].id
        await db_session.flush()

        svc = _make_service()
        request = ConfirmImportRequest(series_ids=[imp_series.id])

        with pytest.raises(ValidationError, match="Resolve file conflicts"):
            await svc.confirm_import(db_session, imp_series.import_job_id, request)

        await db_session.refresh(imp_files[0])
        await db_session.refresh(imp_files[1])
        assert imp_files[0].status == ImportedFileStatus.CONFLICT
        assert imp_files[1].status == ImportedFileStatus.CONFLICT

    @pytest.mark.asyncio
    async def test_conflict_resolution_marks_preferred_confirmed(
        self, db_session: AsyncSession
    ) -> None:
        job, imp_series, imp_files, _series, issues = await _setup_full_scenario(
            db_session,
            num_issues=2,
            job_status=ImportJobStatus.REVIEW,
            series_status=ImportSeriesStatus.MATCHED,
            file_statuses=[
                ImportedFileStatus.CONFLICT,
                ImportedFileStatus.CONFLICT,
            ],
        )
        # Both files are in conflict group 1
        imp_files[0].conflict_group_id = 1
        imp_files[0].is_preferred = True
        imp_files[1].conflict_group_id = 1
        imp_files[1].is_preferred = False
        # Both match same issue
        imp_files[1].matched_issue_id = issues[0].id
        await db_session.flush()

        svc = _make_service()
        request = ConfirmImportRequest(
            series_ids=[imp_series.id],
            conflict_resolutions=[
                ConflictResolution(conflict_group_id=1, chosen_file_id=imp_files[0].id),
            ],
        )
        await svc.confirm_import(db_session, job.id, request)

        await db_session.refresh(imp_files[0])
        await db_session.refresh(imp_files[1])
        assert imp_files[0].status == ImportedFileStatus.CONFIRMED
        assert imp_files[1].status == ImportedFileStatus.SKIPPED


class TestConfirmImportAppliesFileOverrides:
    """confirm_import() applies file_overrides to update matched_issue_id."""

    @pytest.mark.asyncio
    async def test_file_override_updates_match(self, db_session: AsyncSession) -> None:
        job, imp_series, imp_files, _series, issues = await _setup_full_scenario(
            db_session,
            num_issues=2,
            job_status=ImportJobStatus.REVIEW,
            series_status=ImportSeriesStatus.MATCHED,
            file_statuses=[
                ImportedFileStatus.MATCHED,
                ImportedFileStatus.NO_MATCH,
            ],
        )
        imp_files[1].matched_issue_id = None
        await db_session.flush()

        svc = _make_service()
        request = ConfirmImportRequest(
            series_ids=[imp_series.id],
            file_overrides=[
                FileMatchOverride(
                    imported_file_id=imp_files[1].id,
                    issue_id=issues[1].id,
                ),
            ],
        )
        await svc.confirm_import(db_session, job.id, request)

        await db_session.refresh(imp_files[1])
        assert imp_files[1].matched_issue_id == issues[1].id
        assert imp_files[1].status == ImportedFileStatus.CONFIRMED


class TestConfirmImportMarksMatchedAsConfirmed:
    """confirm_import() marks non-conflict MATCHED files as CONFIRMED."""

    @pytest.mark.asyncio
    async def test_matched_files_become_confirmed(self, db_session: AsyncSession) -> None:
        job, imp_series, imp_files, _series, _issues = await _setup_full_scenario(
            db_session,
            num_issues=2,
            job_status=ImportJobStatus.REVIEW,
            series_status=ImportSeriesStatus.MATCHED,
            file_statuses=[
                ImportedFileStatus.MATCHED,
                ImportedFileStatus.MATCHED,
            ],
        )

        svc = _make_service()
        request = ConfirmImportRequest(series_ids=[imp_series.id])
        await svc.confirm_import(db_session, job.id, request)

        for f in imp_files:
            await db_session.refresh(f)
        assert imp_files[0].status == ImportedFileStatus.CONFIRMED
        assert imp_files[1].status == ImportedFileStatus.CONFIRMED


class TestConfirmImportAddsMoveToLibrary:
    """ConfirmImportRequest includes move_to_library flag."""

    def test_schema_accepts_move_to_library(self) -> None:
        req = ConfirmImportRequest(
            series_ids=[1],
            move_to_library=False,
        )
        assert req.move_to_library is False

    def test_schema_default_move_to_library(self) -> None:
        req = ConfirmImportRequest(series_ids=[1])
        assert req.move_to_library is None

    @pytest.mark.asyncio
    async def test_confirm_rejects_move_to_library_false(self, db_session: AsyncSession) -> None:
        """confirm_import() rejects the deprecated move_to_library=false override."""
        job, imp_series, _imp_files, _series, _issues = await _setup_full_scenario(
            db_session,
            num_issues=1,
            series_status=ImportSeriesStatus.MATCHED,
            job_status=ImportJobStatus.REVIEW,
        )
        svc = _make_service()
        req = ConfirmImportRequest(
            series_ids=[imp_series.id],
            move_to_library=False,
        )
        with pytest.raises(ValidationError, match="always creates library artifacts"):
            await svc.confirm_import(db_session, job.id, req)

    @pytest.mark.asyncio
    async def test_confirm_stores_move_to_library_true(self, db_session: AsyncSession) -> None:
        """confirm_import() stores move_to_library=True (default) on the ImportJob."""
        job, imp_series, _imp_files, _series, _issues = await _setup_full_scenario(
            db_session,
            num_issues=1,
            series_status=ImportSeriesStatus.MATCHED,
            job_status=ImportJobStatus.REVIEW,
        )
        svc = _make_service()
        req = ConfirmImportRequest(series_ids=[imp_series.id])
        updated_job = await svc.confirm_import(db_session, job.id, req)
        assert updated_job.move_to_library is True

    @pytest.mark.asyncio
    async def test_run_import_passes_job_move_to_library_false(
        self, db_session: AsyncSession
    ) -> None:
        """run_import() passes job.move_to_library=False to register_library_file."""
        job, _imp_series, _imp_files, series, _issues = await _setup_full_scenario(
            db_session, num_issues=1
        )
        job.move_to_library = False
        await db_session.flush()

        mock_register = _mock_register_library_file()
        mock_ss = AsyncMock()
        mock_ss.add_from_comicvine.return_value = series
        svc = _make_service(series_service=mock_ss)

        with patch(
            "pullbox.services.import_service.register_library_file",
            mock_register,
        ):
            await svc.run_import(db_session, job.id)

        call_kwargs = mock_register.call_args_list[0].kwargs
        assert call_kwargs.get("move_to_library") is False
