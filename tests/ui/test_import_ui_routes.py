"""Tests for Import Collection UI routes — Phase CI-6.

Covers:
- GET /import — wizard page
- GET /import/{id}/progress-partial — scan progress partial
- GET /import/{id}/review-partial — review table partial
- GET /import/{id}/results-partial — import results partial
- GET /import/{id}/log-panel — log viewer partial

Run:
    pytest tests/ui/test_import_ui_routes.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportFileHandlingMode,
    ImportJob,
    ImportJobLog,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryRoot
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series, SeriesStatus
from pullbox.models.user import APIKey, User
from pullbox.services.auth_service import AuthService

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-import-ui")


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
async def _db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def _user_cookie(
    _db_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Create a test user + API key, return the raw key string for auth."""
    raw_key = "pb_k1_" + "c" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="importuiuser",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(user)
        await session.flush()
        api_key = APIKey(
            user_id=user.id,
            key_hash=key_hash,
            name="test-import-ui",
        )
        session.add(api_key)
        await session.commit()
    return raw_key


# ── Helpers ────────────────────────────────────────────────────────────


async def _seed_library_root(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    """Create a library root, return its id."""
    async with factory() as session:
        root = LibraryRoot(
            name="Test Root",
            path="/tmp/library",
            enabled=True,
        )
        session.add(root)
        await session.commit()
        return root.id


async def _seed_series_with_issues(
    factory: async_sessionmaker[AsyncSession],
    *,
    title: str = "Batman",
) -> int:
    """Create a library series with a couple of issues, returning the series id."""
    async with factory() as session:
        root = LibraryRoot(
            name="Lookup Root",
            path="/tmp/lookup-library",
            enabled=True,
        )
        publisher = Publisher(name="DC Comics")
        session.add_all([root, publisher])
        await session.flush()

        series = Series(
            title=title,
            sort_title=title,
            year_start=2016,
            status=SeriesStatus.CONTINUING,
            monitored=True,
            issue_count=2,
            publisher_id=publisher.id,
            library_root_id=root.id,
            path=f"/tmp/lookup-library/{title.lower().replace(' ', '-')}",
        )
        session.add(series)
        await session.flush()

        session.add_all(
            [
                Issue(
                    series_id=series.id,
                    issue_number=1.0,
                    title=f"{title} #1",
                    status=IssueStatus.OWNED,
                ),
                Issue(
                    series_id=series.id,
                    issue_number=2.0,
                    title=f"{title} #2",
                    status=IssueStatus.WANTED,
                ),
            ]
        )
        await session.commit()
        return series.id


async def _seed_import_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    status: ImportJobStatus = ImportJobStatus.PENDING,
    source_path: str = "/tmp/comics",
    with_series: int = 0,
    with_logs: int = 0,
) -> int:
    """Create an import job and optionally seed related series/logs. Returns job_id."""
    async with factory() as session:
        job = ImportJob(
            source_path=source_path,
            source_type=ImportSourceType.FILESYSTEM,
            status=status,
        )
        session.add(job)
        await session.flush()

        for i in range(with_series):
            series_status = (
                ImportSeriesStatus.MATCHED if i % 2 == 0 else ImportSeriesStatus.NO_MATCH
            )
            item = ImportedSeries(
                import_job_id=job.id,
                raw_series_name=f"Series {i}",
                raw_year=2020 + i,
                file_count=5 + i,
                has_files=True,
                sample_paths=[f"/tmp/comics/series_{i}/issue_1.cbz"],
                status=series_status,
                cv_id=10000 + i if series_status == ImportSeriesStatus.MATCHED else None,
                cv_title=f"CV Series {i}" if series_status == ImportSeriesStatus.MATCHED else None,
            )
            session.add(item)

        for i in range(with_logs):
            log = ImportJobLog(
                import_job_id=job.id,
                level="INFO" if i % 2 == 0 else "WARNING",
                event=f"test_event_{i}",
                data={"key": f"value_{i}"},
                logged_at=datetime.now(UTC),
            )
            session.add(log)

        await session.commit()
        return job.id


# ── Direct Handler Tests ──────────────────────────────────────────────


class TestImportPage:
    """Test GET /import (unified import workspace)."""

    @pytest.mark.asyncio
    async def test_import_page_handler(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """import_page returns HTML with the unified import workspace shell."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_page

        await _seed_import_job(_db_factory, status=ImportJobStatus.COMPLETED)

        request = MagicMock()
        request.url.path = "/import"
        request.state.csrf_token = "test-token"
        request.headers = {}

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_page(
                    request,
                    MagicMock(),
                    session,
                    tab="collection",
                    view="all",
                    page=1,
                    resume_job_id=None,
                    resume_step=None,
                )

                mock_templates.TemplateResponse.assert_called_once()
                call_args = mock_templates.TemplateResponse.call_args
                assert call_args[0][1] == "pages/import.html"
                ctx = call_args[0][2]
                assert ctx["tab"] == "collection"
                assert "library_roots" in ctx
                assert "recent_jobs" in ctx
                assert "unmatched_count" in ctx

    @pytest.mark.asyncio
    async def test_import_page_empty_state(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """import_page works with no jobs or roots."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_page

        request = MagicMock()
        request.url.path = "/import"
        request.state.csrf_token = "test-token"
        request.headers = {}

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_page(
                    request,
                    MagicMock(),
                    session,
                    tab="collection",
                    view="all",
                    page=1,
                    resume_job_id=None,
                    resume_step=None,
                )

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["tab"] == "collection"
                assert ctx["library_roots"] == []
                assert ctx["recent_jobs"] == []


class TestImportProgressPartial:
    """Test GET /import/{job_id}/progress-partial."""

    @pytest.mark.asyncio
    async def test_progress_partial(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_progress_partial

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.SCANNING)

        request = MagicMock()
        request.url.path = f"/import/{job_id}/progress-partial"
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_progress_partial(job_id, request, MagicMock(), session)

                call_args = mock_templates.TemplateResponse.call_args
                assert call_args[0][1] == "partials/import_step_progress.html"
                ctx = call_args[0][2]
                assert ctx["job"].id == job_id
                assert ctx["progress_mode"] == "scan"
                assert ctx["completion_target_step"] == 3

    @pytest.mark.asyncio
    async def test_progress_partial_next_step(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Progress partial passes next_step to template context."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_progress_partial

        job_id = await _seed_import_job(
            _db_factory, status=ImportJobStatus.IMPORTING, with_series=0
        )

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_progress_partial(job_id, request, MagicMock(), session, next_step=5)

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["next_step"] == 5
                assert ctx["progress_mode"] == "import"
                assert ctx["completion_target_step"] == 5

    @pytest.mark.asyncio
    async def test_progress_partial_rollback_mode(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Rollback progress uses the explicit rollback shell mode."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_progress_partial

        job_id = await _seed_import_job(
            _db_factory, status=ImportJobStatus.ROLLING_BACK, with_series=0
        )

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_progress_partial(
                    job_id,
                    request,
                    MagicMock(),
                    session,
                    next_step=3,
                    mode="rollback",
                )

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["progress_mode"] == "rollback"
                assert ctx["completion_target_step"] == 3

    @pytest.mark.asyncio
    async def test_progress_partial_default_next_step(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Progress partial defaults next_step to 3."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_progress_partial

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.SCANNING, with_series=0)

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_progress_partial(job_id, request, MagicMock(), session, next_step=3)

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["next_step"] == 3

    @pytest.mark.asyncio
    async def test_progress_partial_not_found(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from unittest.mock import MagicMock

        from pullbox.core.exceptions import NotFoundError
        from pullbox.ui.routes import import_progress_partial

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with pytest.raises(NotFoundError):
                await import_progress_partial(999, request, MagicMock(), session)


class TestImportReviewPartial:
    """Test GET /import/{job_id}/review-partial."""

    @pytest.mark.asyncio
    async def test_review_partial(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_review_partial

        await _seed_library_root(_db_factory)
        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=5)

        async with _db_factory() as session:
            selected_series = (
                (
                    await session.execute(
                        select(ImportedSeries).where(
                            ImportedSeries.import_job_id == job_id,
                            ImportedSeries.status == ImportSeriesStatus.MATCHED,
                        )
                    )
                )
                .scalars()
                .first()
            )
            assert selected_series is not None
            selected_series.selected_for_import = True
            await session.commit()

        request = MagicMock()
        request.url.path = f"/import/{job_id}/review-partial"
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_review_partial(
                    job_id,
                    request,
                    MagicMock(),
                    session,
                    status=None,
                    page=1,
                    sort=None,
                )

                call_args = mock_templates.TemplateResponse.call_args
                assert call_args[0][1] == "partials/import_step_review.html"
                ctx = call_args[0][2]
                assert ctx["job"].id == job_id
                assert ctx["sort"] == "confidence"
                assert len(ctx["series_items"]) == 5
                assert ctx["total"] == 5
                assert len(ctx["library_roots"]) == 1
                assert "status_counts" in ctx
                assert ctx["selected_series_ids"] == [selected_series.id]

    @pytest.mark.asyncio
    async def test_review_partial_exposes_selected_split_series_destination_requirement(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_review_partial

        first_path = tmp_path / "main"
        second_path = tmp_path / "archive"
        first_path.mkdir()
        second_path.mkdir()
        async with _db_factory() as session:
            first_root = LibraryRoot(name="Main", path=str(first_path), enabled=True)
            second_root = LibraryRoot(name="Archive", path=str(second_path), enabled=True)
            session.add_all([first_root, second_root])
            await session.flush()
            job = ImportJob(
                source_path=str(tmp_path),
                source_type=ImportSourceType.MYLAR3,
                status=ImportJobStatus.REVIEW,
                file_handling_mode=ImportFileHandlingMode.IN_PLACE,
            )
            session.add(job)
            await session.flush()
            item = ImportedSeries(
                import_job_id=job.id,
                raw_series_name="Batman",
                status=ImportSeriesStatus.MATCHED,
                cv_id=796,
                selected_for_import=True,
                file_count=2,
                files_total=2,
                files_matched=2,
            )
            session.add(item)
            await session.flush()
            first_file = first_path / "Batman 001.cbz"
            second_file = second_path / "Batman 002.cbz"
            first_file.write_bytes(b"first")
            second_file.write_bytes(b"second")
            session.add_all(
                [
                    ImportedFile(
                        import_job_id=job.id,
                        import_series_id=item.id,
                        file_path=str(first_file),
                        file_name=first_file.name,
                        file_size=first_file.stat().st_size,
                        file_format="cbz",
                        status=ImportedFileStatus.MATCHED,
                    ),
                    ImportedFile(
                        import_job_id=job.id,
                        import_series_id=item.id,
                        file_path=str(second_file),
                        file_name=second_file.name,
                        file_size=second_file.stat().st_size,
                        file_format="cbz",
                        status=ImportedFileStatus.MATCHED,
                    ),
                ]
            )
            await session.commit()
            job_id = job.id

        request = MagicMock()
        request.url.path = f"/import/{job_id}/review-partial"
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_review_partial(
                    job_id,
                    request,
                    MagicMock(),
                    session,
                    status=None,
                    page=1,
                    sort=None,
                )

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                split_review = ctx["split_series_review"]
                assert split_review.requires_preferred_destination is True
                assert split_review.items[0].title == "Batman"
                assert split_review.items[0].root_names == ("Main", "Archive")
                assert [root["name"] for root in ctx["managed_library_root_options"]] == [
                    "Main",
                    "Archive",
                ]

    @pytest.mark.asyncio
    async def test_review_partial_with_status_filter(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_review_partial

        await _seed_library_root(_db_factory)
        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=6)

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_review_partial(
                    job_id,
                    request,
                    MagicMock(),
                    session,
                    status="matched",
                    page=1,
                    sort=None,
                )

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                # 6 series: indices 0,2,4 are matched (even index)
                assert ctx["total"] == 3

    @pytest.mark.asyncio
    async def test_review_partial_invalid_status_filter(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Invalid status filter is ignored (not an error)."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_review_partial

        await _seed_library_root(_db_factory)
        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=2)

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_review_partial(
                    job_id,
                    request,
                    MagicMock(),
                    session,
                    status="bogus",
                    page=1,
                    sort=None,
                )

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                # bogus filter ignored → shows all
                assert ctx["total"] == 2

    @pytest.mark.asyncio
    async def test_review_partial_not_found(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from unittest.mock import MagicMock

        from pullbox.core.exceptions import NotFoundError
        from pullbox.ui.routes import import_review_partial

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with pytest.raises(NotFoundError):
                await import_review_partial(
                    999,
                    request,
                    MagicMock(),
                    session,
                    status=None,
                    page=1,
                    sort=None,
                )


class TestImportResultsPartial:
    """Test GET /import/{job_id}/results-partial."""

    @pytest.mark.asyncio
    async def test_results_partial(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_results_partial

        job_id = await _seed_import_job(
            _db_factory, status=ImportJobStatus.COMPLETED, with_series=4
        )

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_results_partial(job_id, request, MagicMock(), session)

                call_args = mock_templates.TemplateResponse.call_args
                assert call_args[0][1] == "partials/import_results.html"
                ctx = call_args[0][2]
                assert ctx["job"].id == job_id
                assert ctx["can_rollback"] is False
                assert "imported_count" in ctx
                assert "failed_count" in ctx
                assert "duplicate_count" in ctx

    def test_results_template_shows_retry_for_file_only_failures(self) -> None:
        from types import SimpleNamespace

        from pullbox.ui.routes import templates

        html = templates.env.get_template("partials/import_results.html").render(
            job=SimpleNamespace(id=26, status=SimpleNamespace(value="completed")),
            can_rollback=False,
            imported_count=18,
            failed_count=0,
            duplicate_count=1,
            no_match_count=0,
            unmatched_queue_count=0,
            failed_series=[],
            files_total=1,
            files_imported=0,
            files_matched=0,
            files_duplicate=0,
            files_already_owned=0,
            files_conflict=0,
            files_no_match=0,
            orphaned_file_no_match_count=0,
            identified_series_file_no_match_count=0,
            catalog_sync_pending_count=0,
            catalog_sync_failed_count=0,
            catalog_sync_attention_count=0,
            catalog_sync_series=[],
            files_failed=1,
            failed_files=[
                SimpleNamespace(
                    file_name="2000AD prog 2481.cbz",
                    error_message="QueuePool timeout",
                )
            ],
            resume_step=5,
            resume_job_id=26,
            resume_progress_snapshot={},
        )

        assert 'data-testid="import-results-retry-action"' in html
        assert "Retry failed" in html
        assert "2000AD prog 2481.cbz" in html

    def test_results_template_surfaces_background_catalog_sync(self) -> None:
        from types import SimpleNamespace

        from pullbox.ui.routes import templates

        html = templates.env.get_template("partials/import_results.html").render(
            job=SimpleNamespace(id=32, status=SimpleNamespace(value="completed")),
            can_rollback=False,
            imported_count=3,
            failed_count=0,
            duplicate_count=0,
            no_match_count=0,
            unmatched_queue_count=0,
            failed_series=[],
            files_total=3,
            files_imported=3,
            files_matched=3,
            files_duplicate=0,
            files_already_owned=0,
            files_conflict=0,
            files_no_match=0,
            orphaned_file_no_match_count=0,
            identified_series_file_no_match_count=0,
            catalog_sync_pending_count=2,
            catalog_sync_failed_count=1,
            catalog_sync_attention_count=3,
            catalog_sync_series=[
                SimpleNamespace(
                    id=10,
                    title="Batman",
                    issue_catalog_state=SimpleNamespace(value="hydrating"),
                    issue_catalog_error=None,
                ),
                SimpleNamespace(
                    id=11,
                    title="Daredevil",
                    issue_catalog_state=SimpleNamespace(value="failed"),
                    issue_catalog_error="ComicVine timed out",
                ),
            ],
            files_failed=0,
            failed_files=[],
            files_safety_blocked=0,
            safety_blocked_files=[],
            resume_step=5,
            resume_job_id=32,
            resume_progress_snapshot={},
        )

        assert 'data-testid="import-results-catalog-sync-note"' in html
        assert "3 series still need ComicVine catalog sync follow-up" in html
        assert "2 syncing in the background" in html
        assert "1 needs metadata retry" in html
        assert "Batman" in html
        assert "Daredevil" in html

    def test_results_template_distinguishes_owned_artifacts_and_rollback_candidates(
        self,
    ) -> None:
        from types import SimpleNamespace

        from pullbox.ui.routes import templates

        html = templates.env.get_template("partials/import_results.html").render(
            job=SimpleNamespace(id=33, status=SimpleNamespace(value="completed")),
            can_rollback=True,
            imported_count=1,
            failed_count=0,
            duplicate_count=0,
            no_match_count=0,
            unmatched_queue_count=0,
            failed_series=[],
            files_total=4,
            files_imported=3,
            files_matched=3,
            files_duplicate=0,
            files_already_owned=1,
            files_conflict=0,
            files_no_match=0,
            orphaned_file_no_match_count=0,
            identified_series_file_no_match_count=0,
            catalog_sync_pending_count=0,
            catalog_sync_failed_count=0,
            catalog_sync_attention_count=0,
            catalog_sync_series=[],
            files_failed=0,
            failed_files=[],
            files_safety_blocked=0,
            safety_blocked_files=[],
            managed_artifacts_created=2,
            referenced_files_registered=1,
            rollback_managed_candidates=2,
            rollback_reference_candidates=1,
            rollback_manual_recovery_count=0,
            resume_step=5,
            resume_job_id=33,
            resume_progress_snapshot={},
        )

        assert 'data-testid="import-results-ownership-summary"' in html
        assert "Managed artifacts" in html
        assert "In-place references" in html
        assert 'data-testid="import-results-rollback-summary"' in html
        assert "2 managed artifacts will be fingerprint-checked" in html
        assert "1 reference will be detached" in html

    def test_results_template_identifies_incomplete_rollback(self) -> None:
        from types import SimpleNamespace

        from pullbox.ui.routes import templates

        html = templates.env.get_template("partials/import_results.html").render(
            job=SimpleNamespace(
                id=34,
                status=SimpleNamespace(value="failed"),
                error_message="Rollback incomplete: 1 action requires manual recovery.",
            ),
            can_rollback=False,
            rollback_incomplete=True,
            rollback_actions_rolled_back=2,
            rollback_manual_recovery_count=1,
            imported_count=1,
            failed_count=0,
            duplicate_count=0,
            no_match_count=0,
            unmatched_queue_count=0,
            failed_series=[],
            files_total=0,
            files_imported=0,
            files_matched=0,
            files_duplicate=0,
            files_already_owned=0,
            files_conflict=0,
            files_no_match=0,
            orphaned_file_no_match_count=0,
            identified_series_file_no_match_count=0,
            files_failed=0,
            failed_files=[],
            files_safety_blocked=0,
            safety_blocked_files=[],
        )

        assert 'data-testid="import-results-rollback-incomplete"' in html
        assert "Rollback is incomplete" in html
        assert "2 actions rolled back safely" in html
        assert "1 requires manual recovery" in html
        assert 'data-testid="import-results-rollback-action"' not in html

    @pytest.mark.asyncio
    async def test_results_partial_rejects_non_result_status(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from unittest.mock import MagicMock

        from pullbox.core.exceptions import ValidationError
        from pullbox.ui.routes import import_results_partial

        job_id = await _seed_import_job(
            _db_factory,
            status=ImportJobStatus.CANCELLED,
            with_series=2,
        )

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with pytest.raises(ValidationError, match="Results are only available"):
                await import_results_partial(job_id, request, MagicMock(), session)

    @pytest.mark.asyncio
    async def test_results_partial_marks_rollback_eligible_started_import(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Completed imports surface rollback eligibility from backend control state."""
        from datetime import UTC, datetime
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_results_partial

        async with _db_factory() as session:
            job = ImportJob(
                source_path="/tmp/comics",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.COMPLETED,
                import_started_at=datetime.now(UTC),
            )
            session.add(job)
            await session.commit()
            job_id = job.id

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_results_partial(job_id, request, MagicMock(), session)

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["can_rollback"] is True

    @pytest.mark.asyncio
    async def test_results_partial_with_failed_series(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Results partial loads failed series detail when failures exist."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_results_partial

        # Seed a job with FAILED series
        async with _db_factory() as session:
            job = ImportJob(
                source_path="/tmp/comics",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.COMPLETED,
            )
            session.add(job)
            await session.flush()

            for i in range(3):
                item = ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name=f"Failed Series {i}",
                    file_count=1,
                    has_files=True,
                    sample_paths=[],
                    status=ImportSeriesStatus.FAILED,
                    error_message=f"Error {i}",
                )
                session.add(item)
            await session.commit()
            job_id = job.id

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_results_partial(job_id, request, MagicMock(), session)

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["failed_count"] == 3
                assert len(ctx["failed_series"]) == 3

    @pytest.mark.asyncio
    async def test_results_partial_uses_persisted_file_totals(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Results partial surfaces final file totals for completed imports."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_results_partial

        async with _db_factory() as session:
            job = ImportJob(
                source_path="/tmp/comics",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.COMPLETED,
                total_files_imported=1,
                total_files_matched=4,
                total_files_no_match=2,
                total_files_conflict=0,
                total_files_failed=0,
            )
            session.add(job)
            await session.flush()

            imported_series = ImportedSeries(
                import_job_id=job.id,
                raw_series_name="Imported Series",
                file_count=1,
                has_files=True,
                sample_paths=[],
                status=ImportSeriesStatus.IMPORTED,
            )
            session.add(imported_series)
            await session.flush()

            session.add(
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=imported_series.id,
                    file_path="/tmp/comics/example.cbz",
                    file_name="example.cbz",
                    file_size=1024,
                    file_format="cbz",
                    status=ImportedFileStatus.IMPORTED,
                )
            )
            await session.commit()
            job_id = job.id

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_results_partial(job_id, request, MagicMock(), session)

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["files_imported"] == 1
                assert ctx["files_matched"] == 4
                assert ctx["files_no_match"] == 2

    @pytest.mark.asyncio
    async def test_results_partial_splits_unmatched_queue_file_counts(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Step 5 distinguishes queue-backed file no-matches from identified-series ones."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_results_partial

        async with _db_factory() as session:
            job = ImportJob(
                source_path="/tmp/comics",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.COMPLETED,
                total_files_no_match=2,
                series_no_match=1,
            )
            session.add(job)
            await session.flush()

            imported_series = ImportedSeries(
                import_job_id=job.id,
                raw_series_name="Imported Series",
                file_count=1,
                has_files=True,
                sample_paths=[],
                status=ImportSeriesStatus.IMPORTED,
            )
            unmatched_series = ImportedSeries(
                import_job_id=job.id,
                raw_series_name="Unmatched Series",
                file_count=1,
                has_files=True,
                sample_paths=[],
                status=ImportSeriesStatus.NO_MATCH,
            )
            session.add_all([imported_series, unmatched_series])
            await session.flush()

            session.add_all(
                [
                    ImportedFile(
                        import_job_id=job.id,
                        import_series_id=imported_series.id,
                        file_path="/tmp/comics/imported-no-match.cbz",
                        file_name="imported-no-match.cbz",
                        file_size=1024,
                        file_format="cbz",
                        status=ImportedFileStatus.NO_MATCH,
                    ),
                    ImportedFile(
                        import_job_id=job.id,
                        import_series_id=unmatched_series.id,
                        file_path="/tmp/comics/unmatched-series-file.cbz",
                        file_name="unmatched-series-file.cbz",
                        file_size=1024,
                        file_format="cbz",
                        status=ImportedFileStatus.NO_MATCH,
                    ),
                ]
            )
            await session.commit()
            job_id = job.id

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_results_partial(job_id, request, MagicMock(), session)

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["files_no_match"] == 2
                assert ctx["orphaned_file_no_match_count"] == 1
                assert ctx["identified_series_file_no_match_count"] == 1
                assert ctx["no_match_count"] == 1
                assert ctx["unmatched_queue_count"] == 2

    @pytest.mark.asyncio
    async def test_results_partial_not_found(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from unittest.mock import MagicMock

        from pullbox.core.exceptions import NotFoundError
        from pullbox.ui.routes import import_results_partial

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with pytest.raises(NotFoundError):
                await import_results_partial(999, request, MagicMock(), session)


class TestImportCvSearch:
    """Test GET /import/{job_id}/series/{series_id}/cv-search."""

    @pytest.mark.asyncio
    async def test_cv_search_no_query(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """CV search with empty query returns empty results."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_cv_search

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=1)
        # Get the series id
        async with _db_factory() as session:
            from pullbox.models.import_job import ImportedSeries

            result = await session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
            )
            series = result.scalars().first()
            series_id = series.id

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_cv_search(job_id, series_id, request, MagicMock(), session, q="")

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["results"] == []
                assert ctx["query"] == ""

    @pytest.mark.asyncio
    async def test_cv_search_not_found(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """CV search with invalid series_id raises NotFoundError."""
        from unittest.mock import MagicMock

        from pullbox.core.exceptions import NotFoundError
        from pullbox.ui.routes import import_cv_search

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW)

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with pytest.raises(NotFoundError):
                await import_cv_search(job_id, 999, request, MagicMock(), session, q="test")

    @pytest.mark.asyncio
    async def test_cv_search_wrong_job_id(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """CV search with series belonging to different job raises NotFoundError."""
        from unittest.mock import MagicMock

        from pullbox.core.exceptions import NotFoundError
        from pullbox.ui.routes import import_cv_search

        job1_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=1)
        job2_id = await _seed_import_job(
            _db_factory, status=ImportJobStatus.REVIEW, source_path="/other"
        )

        # Get series from job1
        async with _db_factory() as session:
            from pullbox.models.import_job import ImportedSeries

            result = await session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job1_id)
            )
            series = result.scalars().first()
            series_id = series.id

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with pytest.raises(NotFoundError):
                # Pass job2's ID but job1's series → mismatch
                await import_cv_search(job2_id, series_id, request, MagicMock(), session, q="test")

    @pytest.mark.asyncio
    async def test_cv_search_no_api_key(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """CV search without API key returns error message."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_cv_search

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=1)
        async with _db_factory() as session:
            from pullbox.models.import_job import ImportedSeries

            result = await session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
            )
            series = result.scalars().first()
            series_id = series.id

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with (
                patch("pullbox.ui.routes.templates") as mock_templates,
                patch(
                    "pullbox.core.comicvine_key.get_comicvine_api_key",
                    return_value=None,
                ),
            ):
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_cv_search(job_id, series_id, request, MagicMock(), session, q="Batman")

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["search_error"] == "No ComicVine API key configured"
                assert ctx["results"] == []

    @pytest.mark.asyncio
    async def test_cv_search_success_includes_comicvine_url(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """CV search carries the provider detail URL for manual inspection."""
        from unittest.mock import AsyncMock, MagicMock

        from pullbox.ui.routes import import_cv_search

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=1)
        async with _db_factory() as session:
            result = await session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
            )
            series_id = result.scalars().first().id

        request = MagicMock()
        request.state.csrf_token = "test"

        mock_result = MagicMock()
        mock_result.provider_id = "130322"
        mock_result.title = "Henchgirl"
        mock_result.year_start = 2020
        mock_result.publisher = "Dark Horse Comics"
        mock_result.issue_count = 1
        mock_result.comicvine_url = "https://comicvine.gamespot.com/henchgirl/4050-130322/"

        async with _db_factory() as session:
            with (
                patch("pullbox.ui.routes.templates") as mock_templates,
                patch(
                    "pullbox.core.comicvine_key.get_comicvine_api_key",
                    new_callable=AsyncMock,
                    return_value="fake-key",
                ),
                patch(
                    "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                    new_callable=AsyncMock,
                    return_value=([mock_result], 1),
                ) as search_mock,
            ):
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_cv_search(
                    job_id, series_id, request, MagicMock(), session, q="Henchgirl"
                )
                search_mock.assert_awaited_once_with(
                    "Henchgirl",
                    max_results=1000,
                    suppress_errors=False,
                )
                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert len(ctx["results"]) == 1
                result = ctx["results"][0]
                assert result["comicvine_id"] == 130322
                assert result["title"] == "Henchgirl"
                assert result["year_start"] == 2020
                assert result["publisher_name"] == "Dark Horse Comics"
                assert result["issue_count"] == 1
                assert result["comicvine_url"] == (
                    "https://comicvine.gamespot.com/henchgirl/4050-130322/"
                )
                assert ctx["results_limit"] == 100

    @pytest.mark.asyncio
    async def test_cv_search_reuses_persistent_cache_between_requests(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Step 3 CV match search reuses cached full candidate sets."""
        from unittest.mock import AsyncMock, MagicMock

        from pullbox.providers.base import SeriesSearchResult
        from pullbox.ui.routes import import_cv_search

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=1)
        async with _db_factory() as session:
            result = await session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
            )
            series_id = result.scalars().first().id

        request = MagicMock()
        request.state.csrf_token = "test"
        request.app.state.db_session_factory = _db_factory

        mock_result = SeriesSearchResult(
            provider_id="130322",
            title="Henchgirl",
            year_start=2020,
            publisher="Dark Horse Comics",
            issue_count=1,
            status=None,
            cover_url=None,
            description="Cached import match result",
            comicvine_url="https://comicvine.gamespot.com/henchgirl/4050-130322/",
        )

        async with _db_factory() as session:
            with (
                patch("pullbox.ui.routes.templates") as mock_templates,
                patch(
                    "pullbox.core.comicvine_key.get_comicvine_api_key",
                    new_callable=AsyncMock,
                    return_value="fake-key",
                ),
                patch(
                    "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                    new_callable=AsyncMock,
                    return_value=([mock_result], 1),
                ) as search_mock,
            ):
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_cv_search(
                    job_id, series_id, request, MagicMock(), session, q="Henchgirl"
                )
                await import_cv_search(
                    job_id, series_id, request, MagicMock(), session, q="henchgirl"
                )

        search_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cv_search_uses_add_series_default_relevance_order(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """CV match search keeps the same default relevance order as add-series search."""
        from unittest.mock import AsyncMock, MagicMock

        from pullbox.ui.routes import import_cv_search

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=1)
        async with _db_factory() as session:
            result = await session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
            )
            series_id = result.scalars().first().id

        def _result(provider_id: str, title: str, year: int | None) -> MagicMock:
            mock_result = MagicMock()
            mock_result.provider_id = provider_id
            mock_result.title = title
            mock_result.year_start = year
            mock_result.publisher = "DC Comics"
            mock_result.issue_count = 1
            mock_result.comicvine_url = None
            return mock_result

        unsorted_results = [
            _result("3", "Batman", 1940),
            _result("2", "Action Comics", 1938),
            _result("1", "Batman", 2016),
            _result("4", "Action Comics", 2016),
        ]

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with (
                patch("pullbox.ui.routes.templates") as mock_templates,
                patch(
                    "pullbox.core.comicvine_key.get_comicvine_api_key",
                    new_callable=AsyncMock,
                    return_value="fake-key",
                ),
                patch(
                    "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                    new_callable=AsyncMock,
                    return_value=(unsorted_results, len(unsorted_results)),
                ) as search_mock,
            ):
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_cv_search(job_id, series_id, request, MagicMock(), session, q="Bat")

                search_mock.assert_awaited_once_with("Bat", max_results=1000, suppress_errors=False)
                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert [(r["title"], r["year_start"]) for r in ctx["results"]] == [
                    ("Batman", 1940),
                    ("Batman", 2016),
                    ("Action Comics", 1938),
                    ("Action Comics", 2016),
                ]

    @pytest.mark.asyncio
    async def test_cv_search_uses_trailing_year_as_start_year_hint(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Step 3 CV match search shares add-series year-hint behavior."""
        from unittest.mock import AsyncMock, MagicMock

        from pullbox.ui.routes import import_cv_search

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=1)
        async with _db_factory() as session:
            result = await session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
            )
            series_id = result.scalars().first().id

        def _result(provider_id: str, title: str, year: int | None) -> MagicMock:
            mock_result = MagicMock()
            mock_result.provider_id = provider_id
            mock_result.title = title
            mock_result.year_start = year
            mock_result.publisher = "Marvel"
            mock_result.issue_count = 1
            mock_result.comicvine_url = None
            return mock_result

        provider_results = [
            _result("170049", "X-Men", 2025),
            _result("160082", "Ultimate X-Men", 2024),
            _result("158814", "X-Men", 2024),
        ]
        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with (
                patch("pullbox.ui.routes.templates") as mock_templates,
                patch(
                    "pullbox.core.comicvine_key.get_comicvine_api_key",
                    new_callable=AsyncMock,
                    return_value="fake-key",
                ),
                patch(
                    "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                    new_callable=AsyncMock,
                    return_value=(provider_results, len(provider_results)),
                ) as search_mock,
            ):
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_cv_search(
                    job_id, series_id, request, MagicMock(), session, q="X-Men 2024"
                )

                search_mock.assert_awaited_once_with(
                    "X-Men",
                    max_results=1000,
                    suppress_errors=False,
                )
                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert [(r["title"], r["year_start"]) for r in ctx["results"]] == [
                    ("X-Men", 2024),
                    ("X-Men", 2025),
                    ("Ultimate X-Men", 2024),
                ]

    @pytest.mark.asyncio
    async def test_cv_search_shows_up_to_100_candidates(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Step 3 CV match search exposes a broader candidate set before clipping."""
        from unittest.mock import AsyncMock, MagicMock

        from pullbox.ui.routes import import_cv_search

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=1)
        async with _db_factory() as session:
            result = await session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
            )
            series_id = result.scalars().first().id

        def _result(index: int) -> MagicMock:
            mock_result = MagicMock()
            mock_result.provider_id = str(10000 + index)
            mock_result.title = f"Long Run {index:03d}"
            mock_result.year_start = 1900 + index
            mock_result.publisher = "Test Publisher"
            mock_result.issue_count = index
            mock_result.comicvine_url = None
            return mock_result

        provider_results = [_result(index) for index in range(125)]
        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with (
                patch("pullbox.ui.routes.templates") as mock_templates,
                patch(
                    "pullbox.core.comicvine_key.get_comicvine_api_key",
                    new_callable=AsyncMock,
                    return_value="fake-key",
                ),
                patch(
                    "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                    new_callable=AsyncMock,
                    return_value=(provider_results, len(provider_results)),
                ),
            ):
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_cv_search(job_id, series_id, request, MagicMock(), session, q="Long")

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert len(ctx["results"]) == 100
                assert ctx["results"][0]["title"] == "Long Run 000"
                assert ctx["results"][-1]["title"] == "Long Run 099"
                assert ctx["results_limit"] == 100

    @pytest.mark.asyncio
    async def test_cv_search_provider_error_surfaces_error_state(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Provider failures should not masquerade as truthful empty search results."""
        from unittest.mock import AsyncMock, MagicMock

        from pullbox.providers.metadata.comicvine import ComicVineError
        from pullbox.ui.routes import import_cv_search

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=1)
        async with _db_factory() as session:
            from pullbox.models.import_job import ImportedSeries

            result = await session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
            )
            series_id = result.scalars().first().id

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with (
                patch("pullbox.ui.routes.templates") as mock_templates,
                patch(
                    "pullbox.core.comicvine_key.get_comicvine_api_key",
                    new_callable=AsyncMock,
                    return_value="fake-key",
                ),
                patch(
                    "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                    new_callable=AsyncMock,
                    side_effect=ComicVineError(0, "Request timed out: /search/"),
                ),
            ):
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_cv_search(
                    job_id,
                    series_id,
                    request,
                    MagicMock(),
                    session,
                    q="Absolute Wonder Woman Annual",
                )

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["results"] == []
                assert ctx["search_error"] == "Request timed out: /search/"

    def test_cv_search_template_separates_inspect_link_from_select_action(self) -> None:
        """Manual search results expose distinct inspect and select controls."""
        from pullbox.ui.routes import templates

        html = templates.env.get_template("partials/import_cv_search_results.html").render(
            job_id=1,
            series_id=2,
            query="Henchgirl",
            results=[
                {
                    "comicvine_id": 130322,
                    "title": "Henchgirl",
                    "year_start": 2020,
                    "publisher_name": "Dark Horse Comics",
                    "issue_count": 1,
                    "description": "A test result",
                    "cover_url": "https://example.test/henchgirl.jpg",
                    "comicvine_url": "https://comicvine.gamespot.com/henchgirl/4050-130322/",
                    "already_added": False,
                    "library_series_id": None,
                }
            ],
            results_limit=100,
            search_error="",
            csrf_token="test",
        )

        assert 'href="https://comicvine.gamespot.com/henchgirl/4050-130322/"' in html
        assert 'target="_blank"' in html
        assert 'class="modal-panel max-w-4xl max-h-[90vh] min-h-0"' in html
        assert '<div class="modal-body min-h-0">' in html
        assert 'data-testid="import-collection-cv-result-130322"' in html
        assert 'data-testid="import-collection-cv-inspect-130322"' in html
        assert 'data-testid="import-collection-cv-select-130322"' in html
        assert '@click="selectResult(130322)"' in html
        assert 'x-show="selecting"' in html
        assert 'x-show="!selecting"' in html
        assert "Applying ComicVine match..." in html
        assert "Updating this review group and preparing the refreshed results." in html
        assert "isSelectingResult" not in html
        old_row_button = (
            '<button type="button"\n'
            '                    data-testid="import-collection-cv-result-130322"'
        )
        assert old_row_button not in html

    def test_cv_search_template_wraps_long_series_names_without_overrunning_actions(self) -> None:
        """Manual search rows keep long titles in a wrapping text column."""
        from pullbox.ui.routes import templates

        long_title = (
            "BATMAN: ARKHAM ASYLUM 15TH ANNIVERSARY EDITION WITH AN EXTREMELY LONG SUBTITLE"
        )
        row_class = (
            'class="group flex w-full min-w-0 items-start gap-3 px-5 py-4 '
            'text-left transition-colors hover:bg-pb-card-hover/45"'
        )
        link_class = (
            'class="flex min-w-0 items-start gap-1.5 text-sm font-medium '
            "text-pb-text transition-colors hover:text-pb-interactive "
            "focus-visible:outline-none focus-visible:ring-2 "
            "focus-visible:ring-pb-focus focus-visible:ring-offset-2 "
            'focus-visible:ring-offset-pb-bg"'
        )

        html = templates.env.get_template("partials/import_cv_search_results.html").render(
            job_id=1,
            series_id=2,
            query="Batman",
            results=[
                {
                    "comicvine_id": 12345,
                    "title": long_title,
                    "year_start": 2010,
                    "publisher_name": "DC Comics",
                    "issue_count": 1,
                    "description": "A test result",
                    "cover_url": None,
                    "comicvine_url": "https://comicvine.gamespot.com/example/4050-12345/",
                    "already_added": False,
                    "library_series_id": None,
                }
            ],
            results_limit=100,
            search_error="",
            csrf_token="test",
        )

        assert row_class not in html
        assert link_class not in html
        assert 'class="add-series-result-card"' in html
        assert 'class="add-series-result-title"' in html
        assert long_title in html
        assert 'data-testid="import-collection-cv-select-12345"' in html
        assert '<span class="truncate">' not in html


class TestImportLogPanel:
    """Test GET /import/{job_id}/log-panel."""

    @pytest.mark.asyncio
    async def test_log_panel(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_log_panel

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.COMPLETED, with_logs=10)

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_log_panel(
                    job_id,
                    request,
                    MagicMock(),
                    session,
                    page=1,
                    page_size=100,
                    level=None,
                )

                call_args = mock_templates.TemplateResponse.call_args
                assert call_args[0][1] == "partials/import_job_log_panel.html"
                ctx = call_args[0][2]
                assert ctx["job"].id == job_id
                assert "log_entries" not in ctx
                assert "total" not in ctx
                assert "total_pages" not in ctx

    @pytest.mark.asyncio
    async def test_log_panel_renders_shared_log_viewer_contract(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from unittest.mock import MagicMock

        import pullbox.ui.routes as ui_routes
        from pullbox.ui.routes import import_log_panel

        real_templates = ui_routes.templates
        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.COMPLETED, with_logs=10)

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_log_panel(
                    job_id,
                    request,
                    MagicMock(),
                    session,
                    page=1,
                    page_size=100,
                    level="WARNING",
                )

                ctx = mock_templates.TemplateResponse.call_args[0][2]

        html = real_templates.env.get_template("partials/import_job_log_panel.html").render(**ctx)
        assert 'data-log-viewer-contract="v1"' in html
        assert "Import log" in html
        assert "Job #" in html
        assert f"jobId: {job_id}" in html
        assert 'data-testid="import-history-log-viewer-' in html
        assert "overflow-x-hidden" in html
        assert "x-bind:title=\"[entry.formatted_timestamp || entry.timestamp || ''" in html

    @pytest.mark.asyncio
    async def test_log_panel_uses_live_toggle_for_active_jobs(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from unittest.mock import MagicMock

        import pullbox.ui.routes as ui_routes
        from pullbox.ui.routes import import_log_panel

        real_templates = ui_routes.templates
        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.MATCHING, with_logs=5)

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_log_panel(
                    job_id,
                    request,
                    MagicMock(),
                    session,
                    page=1,
                    page_size=2,
                    level=None,
                )

                ctx = mock_templates.TemplateResponse.call_args[0][2]

        html = real_templates.env.get_template("partials/import_job_log_panel.html").render(**ctx)
        assert "toggleLive()" in html
        assert "Live" in html

    @pytest.mark.asyncio
    async def test_log_panel_not_found(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from unittest.mock import MagicMock

        from pullbox.core.exceptions import NotFoundError
        from pullbox.ui.routes import import_log_panel

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with pytest.raises(NotFoundError):
                await import_log_panel(
                    999,
                    request,
                    MagicMock(),
                    session,
                    page=1,
                    page_size=100,
                    level=None,
                )


# ── Orphaned Series Page Tests ───────────────────────────────────────


async def _seed_orphan_data(
    factory: async_sessionmaker[AsyncSession],
    *,
    no_match_count: int = 3,
    recovery_pending_count: int = 0,
    skipped_count: int = 0,
    imported_issue_recovery_count: int = 0,
) -> int:
    """Create COMPLETED job with active unmatched and dismissed series. Returns job_id."""
    async with factory() as session:
        job = ImportJob(
            source_path="/tmp/orphans",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.COMPLETED,
        )
        session.add(job)
        await session.flush()

        for i in range(no_match_count):
            session.add(
                ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name=f"Orphan {i}",
                    file_count=5 + i,
                    status=ImportSeriesStatus.NO_MATCH,
                )
            )
        for i in range(recovery_pending_count):
            session.add(
                ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name=f"Identified {i}",
                    file_count=4 + i,
                    status=ImportSeriesStatus.RECOVERY_PENDING,
                    cv_id=7000 + i,
                    cv_title=f"Identified Series {i}",
                )
            )
        for i in range(skipped_count):
            session.add(
                ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name=f"Dismissed {i}",
                    file_count=2,
                    status=ImportSeriesStatus.SKIPPED,
                )
            )

        for i in range(imported_issue_recovery_count):
            imported_series = ImportedSeries(
                import_job_id=job.id,
                raw_series_name=f"Issue Recovery {i}",
                file_count=4,
                files_imported=3,
                files_no_match=1,
                status=ImportSeriesStatus.IMPORTED,
                cv_id=8500 + i,
                cv_title=f"Issue Recovery {i}",
            )
            session.add(imported_series)
            await session.flush()
            session.add(
                ImportedFile(
                    import_job_id=job.id,
                    import_series_id=imported_series.id,
                    file_path=f"/tmp/issue-recovery-{i}.cbz",
                    file_name=f"Issue Recovery {i}.cbz",
                    file_size=1024,
                    file_format="cbz",
                    status=ImportedFileStatus.NO_MATCH,
                )
            )

        await session.commit()
        return job.id


class TestImportUnmatchedTab:
    """Test unmatched-series content under the unified Import page."""

    @pytest.mark.asyncio
    async def test_orphaned_page_full_render(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Full page render with orphaned series data."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_page

        await _seed_orphan_data(
            _db_factory,
            no_match_count=3,
            recovery_pending_count=1,
            skipped_count=1,
            imported_issue_recovery_count=1,
        )

        request = MagicMock()
        request.url.path = "/import"
        request.state.csrf_token = "test"
        request.headers = {}  # No HX-Request → full page

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_page(
                    request,
                    MagicMock(),
                    session,
                    tab="unmatched",
                    view="all",
                    page=1,
                )

                call_args = mock_templates.TemplateResponse.call_args
                assert call_args[0][1] == "pages/import.html"
                ctx = call_args[0][2]
                assert ctx["tab"] == "unmatched"
                assert ctx["total"] == 5
                assert len(ctx["items"]) == 5
                assert ctx["orphaned_count"] == 5
                assert ctx["dismissed_count"] == 1
                assert ctx["view"] == "all"

    def test_orphaned_table_marks_recovery_pending_rows_as_identified(self) -> None:
        """Recovery-pending rows stay in the active queue with guided actions."""
        from pullbox.ui.routes import templates

        html = templates.env.get_template("partials/import_orphaned_table.html").render(
            items=[
                ImportedSeries(
                    id=7,
                    import_job_id=1,
                    raw_series_name="Henchgirl",
                    file_count=1,
                    status=ImportSeriesStatus.RECOVERY_PENDING,
                    cv_id=12345,
                    cv_title="Henchgirl",
                )
            ],
            total=1,
            page=1,
            page_size=25,
            view="all",
            orphaned_count=1,
            dismissed_count=0,
            total_pages=1,
        )

        assert "Series identified" in html
        assert 'data-tip="Continue recovery"' in html
        assert 'data-testid="import-orphaned-recover-7"' in html
        assert 'data-tip="Change ComicVine match"' in html
        assert 'class="downloads-action-btn"' in html

    def test_orphaned_table_routes_imported_issue_recovery_rows_directly_to_file_resolution(
        self,
    ) -> None:
        """Imported rows with leftover file no-matches open recovery directly."""
        from pullbox.ui.routes import templates

        html = templates.env.get_template("partials/import_orphaned_table.html").render(
            items=[
                ImportedSeries(
                    id=11,
                    import_job_id=1,
                    raw_series_name="Absolute Martian Manhunter",
                    file_count=11,
                    files_no_match=1,
                    files_imported=10,
                    status=ImportSeriesStatus.IMPORTED,
                    cv_id=168590,
                    cv_title="Absolute Martian Manhunter",
                )
            ],
            total=1,
            page=1,
            page_size=25,
            view="all",
            orphaned_count=1,
            dismissed_count=0,
            total_pages=1,
        )

        assert "Issue unresolved" in html
        assert 'data-testid="import-orphaned-recover-11"' in html
        assert 'data-tip="Resolve files"' in html
        assert 'aria-label="Resolve unresolved files for Absolute Martian Manhunter"' in html
        assert 'data-testid="import-orphaned-search-11"' not in html

    def test_orphaned_table_routes_known_series_no_match_rows_directly_to_file_resolution(
        self,
    ) -> None:
        """Deferred rows with known ComicVine series do not require another CV search."""
        from pullbox.ui.routes import templates

        html = templates.env.get_template("partials/import_orphaned_table.html").render(
            items=[
                ImportedSeries(
                    id=12,
                    import_job_id=1,
                    raw_series_name="Powers 25",
                    file_count=1,
                    files_no_match=1,
                    status=ImportSeriesStatus.NO_MATCH,
                    cv_id=166903,
                    cv_title="Powers 25",
                    user_selected_cv_id=166903,
                )
            ],
            total=1,
            page=1,
            page_size=25,
            view="all",
            orphaned_count=1,
            dismissed_count=0,
            total_pages=1,
        )

        assert "Issue unresolved" in html
        assert 'data-testid="import-orphaned-recover-12"' in html
        assert 'data-tip="Resolve files"' in html
        assert 'data-testid="import-orphaned-search-12"' not in html

    def test_import_reconcile_modal_uses_import_reconciliation_copy(self) -> None:
        """Step 3 reconciliation modal does not use post-import recovery language."""
        from pullbox.ui.routes import templates

        html = templates.env.get_template("partials/import_reconcile_modal.html").render(
            imported_series=ImportedSeries(
                id=12,
                import_job_id=1,
                raw_series_name="Powers 25",
                file_count=1,
                files_no_match=1,
                status=ImportSeriesStatus.NO_MATCH,
                cv_id=166903,
                cv_title="Powers 25",
            ),
            issue_options=[
                {
                    "issue_cv_id": 1167175,
                    "issue_number": 9.0,
                    "title": "Issue 9",
                    "release_date": None,
                    "already_imported": False,
                }
            ],
            files=[
                {
                    "imported_file_id": 77,
                    "file_name": "Powers 009.cbz",
                    "file_path": "/tmp/Powers 009.cbz",
                    "file_format": "cbz",
                    "parsed_issue_number": 25.0,
                    "parsed_year": 2026,
                    "comicvine_issue_id": None,
                    "status": ImportedFileStatus.NO_MATCH,
                    "error_message": None,
                    "matched_issue_cv_id": None,
                    "suggested_issue_cv_id": 1167175,
                    "suggested_issue_label": "#9 - Issue 9",
                    "decision_locked": False,
                    "diagnostics": {},
                }
            ],
            files_remaining=1,
            files_completed=0,
            csrf_token="test-csrf",
        )

        assert "Reconcile for Import" in html
        assert "Save reconciliation" in html
        assert "Recover files" not in html
        assert 'data-testid="import-reconcile-modal"' in html
        assert 'data-testid="import-reconcile-save"' in html
        assert 'data-testid="import-reconcile-skip-77"' in html
        assert 'data-dropdown-wrap-options="true"' in html
        assert "import-reconcile-issue-dropdown" in html
        assert "dropdown-select-panel-wrap" in html
        assert "wrapOptions: true" in html
        assert '@click="toggleSkip(77)"' in html
        assert "Will skip" in html
        assert "Restore issue decision" in html
        assert 'text-pb-text-inverse" x-text=' in html
        assert ">Save reconciliation</span>" in html
        assert "+ &#34;Powers 009.cbz&#34;" in html
        assert '+ "Powers 009.cbz"' not in html

    def test_import_reconcile_modal_offers_provisional_issue_action(self) -> None:
        """Missing provider issue targets can be explicitly created for this import."""
        from pullbox.ui.routes import templates

        html = templates.env.get_template("partials/import_reconcile_modal.html").render(
            imported_series=ImportedSeries(
                id=12,
                import_job_id=1,
                raw_series_name="King Dracula",
                raw_year=2026,
                file_count=1,
                files_no_match=1,
                status=ImportSeriesStatus.NO_MATCH,
                cv_id=169964,
                cv_title="King Dracula",
                cv_issue_count=3,
            ),
            issue_options=[
                {
                    "issue_cv_id": 1100001,
                    "issue_number": 1.0,
                    "title": "Issue 1",
                    "release_date": None,
                    "already_imported": False,
                },
                {
                    "issue_cv_id": 1100002,
                    "issue_number": 2.0,
                    "title": "Issue 2",
                    "release_date": None,
                    "already_imported": False,
                },
                {
                    "issue_cv_id": 1100003,
                    "issue_number": 3.0,
                    "title": "Issue 3",
                    "release_date": None,
                    "already_imported": False,
                },
            ],
            files=[
                {
                    "imported_file_id": 77,
                    "file_name": "King Dracula 04 (of 04) (2026).cbr",
                    "file_path": "/tmp/King Dracula 04 (of 04) (2026).cbr",
                    "file_format": "cbr",
                    "parsed_issue_number": 4.0,
                    "archive_entry_issue_number": 4.0,
                    "parsed_year": 2026,
                    "comicvine_issue_id": None,
                    "status": ImportedFileStatus.NO_MATCH,
                    "error_message": None,
                    "matched_issue_cv_id": None,
                    "suggested_issue_cv_id": None,
                    "suggested_issue_label": None,
                    "can_create_provisional_issue": True,
                    "provisional_issue_number": 4.0,
                    "provisional_issue_label": "#4",
                    "decision_locked": False,
                    "diagnostics": {},
                }
            ],
            files_remaining=1,
            files_completed=0,
            csrf_token="test-csrf",
        )

        assert "Create provisional #4" in html
        assert "data-reconcile-provisional-button" in html
        assert 'data-provisional-issue-number="4"' in html
        assert 'data-testid="import-reconcile-provisional-77"' in html
        assert "Will create issue" in html

    def test_import_reconcile_skip_controller_updates_state_even_if_control_lookup_misses(
        self,
    ) -> None:
        """Skip toggles should still update visible state if a dropdown node is absent."""
        source = Path("src/pullbox/ui/static/js/pullbox.js").read_text()
        start = source.index("function importReconcileModal")
        end = source.index("function orphanedRecoveryModal")
        reconcile_controller = source[start:end]

        assert "if (!row || !select || !trigger)" not in reconcile_controller
        assert "if (row) {" in reconcile_controller
        assert "if (select) {" in reconcile_controller
        assert "if (trigger) {" in reconcile_controller
        assert "this.skippedFileIds = next;" in reconcile_controller
        assert "provisionalFileIds" in reconcile_controller
        assert 'action: "provisional"' in reconcile_controller

    def test_dropdown_wrap_mode_uses_trigger_width_for_panel(self) -> None:
        """Wrapping dropdowns should not expand the panel to fit one long label."""
        source = Path("src/pullbox/ui/static/js/pullbox.js").read_text()
        start = source.index("function dropdownSelectData")
        end = source.index("function _readSearchHistory")
        dropdown_controller = source[start:end]

        assert "wrapOptions: Boolean(cfg.wrapOptions)" in dropdown_controller
        assert "this.wrapOptions" in dropdown_controller
        assert "? Math.ceil(triggerRect.width)" in dropdown_controller

    def test_orphaned_table_uses_centered_status_pills_and_icon_actions(self) -> None:
        """Unmatched rows use centered pills and standard icon action buttons."""
        from pullbox.ui.routes import templates

        html = templates.env.get_template("partials/import_orphaned_table.html").render(
            items=[
                ImportedSeries(
                    id=8,
                    import_job_id=1,
                    raw_series_name="Persephone",
                    file_count=2,
                    status=ImportSeriesStatus.NO_MATCH,
                ),
                ImportedSeries(
                    id=9,
                    import_job_id=1,
                    raw_series_name="Dead Space",
                    file_count=1,
                    status=ImportSeriesStatus.SKIPPED,
                ),
            ],
            total=2,
            page=1,
            page_size=25,
            view="all",
            orphaned_count=1,
            dismissed_count=1,
            total_pages=1,
        )

        assert (
            "inline-flex min-h-8 items-center justify-center whitespace-normal "
            "text-center leading-tight" in html
        )
        assert 'data-tip="Search ComicVine"' in html
        assert 'data-tip="Dismiss"' in html
        assert 'aria-label="Search ComicVine for Persephone"' in html
        assert 'aria-label="Dismiss unmatched series Persephone"' in html
        assert "Search CV" not in html

    def test_orphaned_table_uses_app_confirm_modal_for_dismiss(self) -> None:
        """Dismiss actions rely on the shared app confirm modal instead of hx-confirm."""
        from pullbox.ui.routes import templates

        html = templates.env.get_template("partials/import_orphaned_table.html").render(
            items=[
                ImportedSeries(
                    id=10,
                    import_job_id=1,
                    raw_series_name="Wasted Space",
                    file_count=1,
                    status=ImportSeriesStatus.NO_MATCH,
                )
            ],
            total=1,
            page=1,
            page_size=25,
            view="all",
            orphaned_count=1,
            dismissed_count=0,
            total_pages=1,
        )

        assert "hx-confirm" not in html
        assert "@click='dismissOrphan(" in html
        assert ':disabled="isDismissingOrphan(10)"' in html

    def test_orphaned_recovery_modal_uses_contract_controls(self) -> None:
        """Recovery modal uses contract dropdowns and a row action instead of native controls."""
        from pullbox.ui.routes import templates

        html = templates.env.get_template("partials/import_orphaned_recovery.html").render(
            csrf_token="test-token",
            imported_series=ImportedSeries(
                id=12,
                import_job_id=1,
                raw_series_name="Persephone",
                cv_title="Persephone",
                cv_publisher="Image",
                status=ImportSeriesStatus.RECOVERY_PENDING,
            ),
            issue_options=[
                {
                    "issue_cv_id": 501,
                    "issue_number": 1.0,
                    "title": "Awakening",
                    "release_date": None,
                    "already_imported": False,
                }
            ],
            files=[
                {
                    "imported_file_id": 17,
                    "file_name": "Persephone 001.pdf",
                    "file_path": "/imports/Persephone 001.pdf",
                    "file_format": "pdf",
                    "parsed_issue_number": 1.0,
                    "parsed_year": 2022,
                    "comicvine_issue_id": None,
                    "status": ImportedFileStatus.NO_MATCH,
                    "error_message": None,
                    "matched_issue_cv_id": None,
                    "suggested_issue_cv_id": 501,
                    "suggested_issue_label": "#1 - Awakening",
                    "decision_locked": False,
                    "diagnostics": {},
                }
            ],
            requires_library_root=True,
            selected_library_root_id=9,
            available_library_roots=[{"id": 9, "name": "Main", "path": "/library"}],
            files_remaining=1,
            files_completed=0,
            recovery_progress=None,
        )

        assert 'data-testid="orphaned-recovery-root"' not in html
        assert ">Library root<" not in html
        assert 'data-dropdown-select-contract="v1"' in html
        assert "data-dropdown-select-panel" in html
        assert "@click.outside=" in html
        assert 'data-testid="orphaned-recovery-skip-17"' in html
        assert 'data-testid="orphaned-recovery-progress-bar"' in html
        assert 'data-testid="orphaned-recovery-progress-value"' in html
        assert 'role="progressbar"' in html
        assert "app-progress-track" in html
        assert "app-progress-value--interactive" in html
        assert "min-w-[11.5rem]" in html
        assert "whitespace-nowrap" in html
        assert "shrink-0" in html
        assert "+ &#34;Persephone 001.pdf&#34;" in html
        assert '+ "Persephone 001.pdf"' not in html
        assert "Skip this file for now" in html
        assert "Restore issue decision" in html
        assert "data-recovery-skip-toggle" not in html
        assert 'type="checkbox"' not in html

    @pytest.mark.asyncio
    async def test_orphaned_page_dismissed_tab(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Dismissed tab shows SKIPPED series."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_page

        await _seed_orphan_data(_db_factory, no_match_count=2, skipped_count=3)

        request = MagicMock()
        request.url.path = "/import"
        request.state.csrf_token = "test"
        request.headers = {}

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_page(
                    request,
                    MagicMock(),
                    session,
                    tab="unmatched",
                    view="dismissed",
                    page=1,
                )

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["tab"] == "unmatched"
                assert ctx["total"] == 3
                assert ctx["orphaned_count"] == 2
                assert ctx["dismissed_count"] == 3
                assert ctx["view"] == "dismissed"

    @pytest.mark.asyncio
    async def test_orphaned_page_htmx_partial(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """HTMX request returns the unmatched content bundle."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_page

        await _seed_orphan_data(_db_factory, no_match_count=2)

        request = MagicMock()
        request.url.path = "/import"
        request.state.csrf_token = "test"
        request.headers = {"HX-Request": "true", "HX-Target": "import-orphaned-results"}

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_page(
                    request,
                    MagicMock(),
                    session,
                    tab="unmatched",
                    view="all",
                    page=1,
                )

                call_args = mock_templates.TemplateResponse.call_args
                assert call_args[0][1] == "partials/import_orphaned_content_bundle.html"

    @pytest.mark.asyncio
    async def test_orphaned_page_empty_state(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Empty state when no orphaned series exist."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_page

        request = MagicMock()
        request.url.path = "/import"
        request.state.csrf_token = "test"
        request.headers = {}

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_page(
                    request,
                    MagicMock(),
                    session,
                    tab="unmatched",
                    view="all",
                    page=1,
                )

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["tab"] == "unmatched"
                assert ctx["total"] == 0
                assert ctx["items"] == []
                assert ctx["orphaned_count"] == 0


class TestImportOrphanedCvSearch:
    """Test GET /import/orphaned/{id}/cv-search."""

    def test_orphaned_cv_search_template_wraps_long_series_names_without_overrunning_actions(
        self,
    ) -> None:
        """Orphaned search rows keep long titles wrapped away from action controls."""
        from pullbox.ui.routes import templates

        long_title = (
            "BATMAN: ARKHAM ASYLUM 15TH ANNIVERSARY EDITION WITH AN EXTREMELY LONG SUBTITLE"
        )
        row_class = (
            'class="group flex w-full min-w-0 items-start gap-3 px-5 py-4 '
            'text-left transition-colors hover:bg-pb-card-hover/45"'
        )
        link_class = (
            'class="flex min-w-0 items-start gap-1.5 text-sm font-medium '
            "text-pb-text transition-colors hover:text-pb-interactive "
            "focus-visible:outline-none focus-visible:ring-2 "
            "focus-visible:ring-pb-focus focus-visible:ring-offset-2 "
            'focus-visible:ring-offset-pb-bg"'
        )

        html = templates.env.get_template("partials/import_orphaned_cv_search.html").render(
            imported_series_id=1,
            query="Batman",
            results=[
                {
                    "cv_id": 12345,
                    "name": long_title,
                    "start_year": 2010,
                    "publisher": "DC Comics",
                    "issue_count": 1,
                    "cv_url": "https://comicvine.gamespot.com/example/4050-12345/",
                }
            ],
            search_error="",
            csrf_token="test",
        )

        assert row_class in html
        assert 'class="min-w-0 flex-1"' in html
        assert link_class in html
        assert '<span class="min-w-0 break-words whitespace-normal">' in html
        assert '<div class="flex shrink-0 items-center gap-2 self-start">' in html
        assert '<span class="truncate">' not in html

    @pytest.mark.asyncio
    async def test_cv_search_empty_query(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """CV search with empty query returns empty results."""
        from unittest.mock import MagicMock

        from pullbox.ui.routes import import_orphaned_cv_search

        job_id = await _seed_orphan_data(_db_factory, no_match_count=1)
        async with _db_factory() as session:
            result = await session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
            )
            series_id = result.scalars().first().id

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with patch("pullbox.ui.routes.templates") as mock_templates:
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_orphaned_cv_search(series_id, request, MagicMock(), session, q="")

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["results"] == []

    @pytest.mark.asyncio
    async def test_cv_search_not_found(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """CV search raises NotFoundError for missing series."""
        from unittest.mock import MagicMock

        from pullbox.core.exceptions import NotFoundError
        from pullbox.ui.routes import import_orphaned_cv_search

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with pytest.raises(NotFoundError):
                await import_orphaned_cv_search(99999, request, MagicMock(), session, q="test")

    @pytest.mark.asyncio
    async def test_cv_search_no_api_key(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """CV search with query but no API key returns error."""
        from unittest.mock import AsyncMock, MagicMock

        from pullbox.ui.routes import import_orphaned_cv_search

        job_id = await _seed_orphan_data(_db_factory, no_match_count=1)
        async with _db_factory() as session:
            result = await session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
            )
            series_id = result.scalars().first().id

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with (
                patch("pullbox.ui.routes.templates") as mock_templates,
                patch(
                    "pullbox.ui.routes.import_orphaned_cv_search.__wrapped__",
                    side_effect=None,
                )
                if False
                else patch(
                    "pullbox.core.comicvine_key.get_comicvine_api_key",
                    new_callable=AsyncMock,
                    return_value=None,
                ),
            ):
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_orphaned_cv_search(
                    series_id, request, MagicMock(), session, q="Batman"
                )
                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["search_error"] == "No ComicVine API key configured"
                assert ctx["results"] == []

    @pytest.mark.asyncio
    async def test_cv_search_success(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """CV search with valid query and API key returns results."""
        from unittest.mock import AsyncMock, MagicMock

        from pullbox.ui.routes import import_orphaned_cv_search

        job_id = await _seed_orphan_data(_db_factory, no_match_count=1)
        async with _db_factory() as session:
            result = await session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
            )
            series_id = result.scalars().first().id

        request = MagicMock()
        request.state.csrf_token = "test"

        mock_result = MagicMock()
        mock_result.provider_id = "12345"
        mock_result.title = "Batman"
        mock_result.year_start = 2016
        mock_result.publisher = "DC Comics"
        mock_result.issue_count = 85

        async with _db_factory() as session:
            with (
                patch("pullbox.ui.routes.templates") as mock_templates,
                patch(
                    "pullbox.core.comicvine_key.get_comicvine_api_key",
                    new_callable=AsyncMock,
                    return_value="fake-key",
                ),
                patch(
                    "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                    new_callable=AsyncMock,
                    return_value=([mock_result], 1),
                ) as search_mock,
            ):
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_orphaned_cv_search(
                    series_id, request, MagicMock(), session, q="Batman"
                )
                search_mock.assert_awaited_once_with("Batman", max_results=1000)
                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert len(ctx["results"]) == 1
                assert ctx["results"][0]["cv_id"] == 12345
                assert ctx["results"][0]["name"] == "Batman"

    @pytest.mark.asyncio
    async def test_cv_search_reuses_persistent_cache_between_requests(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Deferred unresolved CV search reuses cached full candidate sets."""
        from unittest.mock import AsyncMock, MagicMock

        from pullbox.providers.base import SeriesSearchResult
        from pullbox.ui.routes import import_orphaned_cv_search

        job_id = await _seed_orphan_data(_db_factory, no_match_count=1)
        async with _db_factory() as session:
            result = await session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
            )
            series_id = result.scalars().first().id

        request = MagicMock()
        request.state.csrf_token = "test"
        request.app.state.db_session_factory = _db_factory

        mock_result = SeriesSearchResult(
            provider_id="12345",
            title="Batman",
            year_start=2016,
            publisher="DC Comics",
            issue_count=85,
            status=None,
            cover_url=None,
            description="Cached orphaned search result",
        )

        async with _db_factory() as session:
            with (
                patch("pullbox.ui.routes.templates") as mock_templates,
                patch(
                    "pullbox.core.comicvine_key.get_comicvine_api_key",
                    new_callable=AsyncMock,
                    return_value="fake-key",
                ),
                patch(
                    "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                    new_callable=AsyncMock,
                    return_value=([mock_result], 1),
                ) as search_mock,
            ):
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_orphaned_cv_search(
                    series_id, request, MagicMock(), session, q="Batman"
                )
                await import_orphaned_cv_search(
                    series_id, request, MagicMock(), session, q="batman"
                )

        search_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cv_search_uses_trailing_year_as_start_year_hint(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Deferred unresolved search shares add-series year-hint behavior."""
        from unittest.mock import AsyncMock, MagicMock

        from pullbox.ui.routes import import_orphaned_cv_search

        job_id = await _seed_orphan_data(_db_factory, no_match_count=1)
        async with _db_factory() as session:
            result = await session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
            )
            series_id = result.scalars().first().id

        def _result(provider_id: str, title: str, year: int | None) -> MagicMock:
            mock_result = MagicMock()
            mock_result.provider_id = provider_id
            mock_result.title = title
            mock_result.year_start = year
            mock_result.publisher = "Marvel"
            mock_result.issue_count = 1
            mock_result.comicvine_url = None
            return mock_result

        provider_results = [
            _result("170049", "X-Men", 2025),
            _result("158814", "X-Men", 2024),
        ]
        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with (
                patch("pullbox.ui.routes.templates") as mock_templates,
                patch(
                    "pullbox.core.comicvine_key.get_comicvine_api_key",
                    new_callable=AsyncMock,
                    return_value="fake-key",
                ),
                patch(
                    "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                    new_callable=AsyncMock,
                    return_value=(provider_results, len(provider_results)),
                ) as search_mock,
            ):
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_orphaned_cv_search(
                    series_id, request, MagicMock(), session, q="X-Men (2024)"
                )

                search_mock.assert_awaited_once_with("X-Men", max_results=1000)
                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert [(r["name"], r["start_year"]) for r in ctx["results"]] == [
                    ("X-Men", 2024),
                    ("X-Men", 2025),
                ]

    @pytest.mark.asyncio
    async def test_cv_search_provider_error(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """CV search captures provider errors gracefully."""
        from unittest.mock import AsyncMock, MagicMock

        from pullbox.ui.routes import import_orphaned_cv_search

        job_id = await _seed_orphan_data(_db_factory, no_match_count=1)
        async with _db_factory() as session:
            result = await session.execute(
                select(ImportedSeries).where(ImportedSeries.import_job_id == job_id)
            )
            series_id = result.scalars().first().id

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with (
                patch("pullbox.ui.routes.templates") as mock_templates,
                patch(
                    "pullbox.core.comicvine_key.get_comicvine_api_key",
                    new_callable=AsyncMock,
                    return_value="fake-key",
                ),
                patch(
                    "pullbox.providers.metadata.comicvine.ComicVineProvider.search_series_globally",
                    new_callable=AsyncMock,
                    side_effect=Exception("API rate limited"),
                ),
            ):
                mock_templates.TemplateResponse.return_value = MagicMock()
                await import_orphaned_cv_search(
                    series_id, request, MagicMock(), session, q="Batman"
                )
                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["search_error"] == "API rate limited"
                assert ctx["results"] == []


class TestImportOrphanedRecovery:
    """Test GET /import/orphaned/{id}/recovery."""

    @pytest.mark.asyncio
    async def test_import_reconcile_route_renders_modal_context(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Step 3 reconcile route renders issue decisions for the active import."""
        from unittest.mock import AsyncMock, MagicMock

        from pullbox.ui.routes import import_series_reconcile

        imported_series = ImportedSeries(
            id=7,
            import_job_id=3,
            raw_series_name="Powers 25",
            raw_year=2025,
            raw_publisher="Dark Horse",
            file_count=1,
            status=ImportSeriesStatus.NO_MATCH,
            cv_id=166903,
            cv_title="Powers 25",
            cv_publisher="Dark Horse",
        )

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with (
                patch("pullbox.ui.routes.templates") as mock_templates,
                patch(
                    "pullbox.composition.services.build_import_service",
                    new_callable=AsyncMock,
                ) as mock_build_service,
            ):
                mock_templates.TemplateResponse.return_value = MagicMock()
                mock_service = AsyncMock()
                mock_service.get_import_reconcile_context.return_value = {
                    "imported_series": imported_series,
                    "issue_options": [
                        {
                            "issue_cv_id": 1167175,
                            "issue_number": 9.0,
                            "title": "Issue 9",
                            "release_date": None,
                            "already_imported": False,
                        }
                    ],
                    "files": [
                        {
                            "imported_file_id": 77,
                            "file_name": "Powers 009.cbz",
                            "file_path": "/tmp/Powers 009.cbz",
                            "file_format": "cbz",
                            "parsed_issue_number": 25.0,
                            "parsed_year": 2026,
                            "comicvine_issue_id": None,
                            "status": ImportedFileStatus.NO_MATCH,
                            "error_message": None,
                            "matched_issue_cv_id": None,
                            "suggested_issue_cv_id": 1167175,
                            "suggested_issue_label": "#9 - Issue 9",
                            "decision_locked": False,
                            "diagnostics": {},
                        }
                    ],
                    "files_remaining": 1,
                    "files_completed": 0,
                }
                mock_build_service.return_value = mock_service

                await import_series_reconcile(3, 7, request, MagicMock(), session)

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["imported_series"].cv_id == 166903
                assert ctx["files_remaining"] == 1
                assert ctx["issue_options"][0]["issue_cv_id"] == 1167175
                mock_service.get_import_reconcile_context.assert_awaited_once_with(
                    session,
                    3,
                    7,
                )

    @pytest.mark.asyncio
    async def test_orphaned_recovery_route_renders_modal_context(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Recovery route renders the guided recovery modal payload."""
        from unittest.mock import AsyncMock, MagicMock

        from pullbox.ui.routes import import_orphaned_recovery

        imported_series = ImportedSeries(
            id=7,
            import_job_id=3,
            raw_series_name="Henchgirl",
            raw_year=2018,
            raw_publisher="Scout",
            file_count=2,
            status=ImportSeriesStatus.RECOVERY_PENDING,
            cv_id=12345,
            cv_title="Henchgirl",
            cv_publisher="Scout",
        )

        request = MagicMock()
        request.state.csrf_token = "test"

        async with _db_factory() as session:
            with (
                patch("pullbox.ui.routes.templates") as mock_templates,
                patch(
                    "pullbox.composition.services.build_import_service",
                    new_callable=AsyncMock,
                ) as mock_build_service,
                patch(
                    "pullbox.tasks.import_orphan_recovery_task.get_orphan_recovery_progress_state"
                ) as mock_progress_state,
            ):
                mock_templates.TemplateResponse.return_value = MagicMock()
                mock_service = AsyncMock()
                mock_service.get_orphan_recovery_context.return_value = {
                    "imported_series": imported_series,
                    "issue_options": [
                        {
                            "issue_cv_id": 501,
                            "issue_number": 1.0,
                            "title": "Issue One",
                            "release_date": "2018-01-01",
                            "already_imported": False,
                        }
                    ],
                    "files": [
                        {
                            "imported_file_id": 11,
                            "file_name": "Henchgirl 001.cbz",
                            "file_path": "/tmp/Henchgirl 001.cbz",
                            "file_format": "cbz",
                            "parsed_issue_number": 1.0,
                            "parsed_year": 2018,
                            "comicvine_issue_id": None,
                            "status": ImportedFileStatus.PENDING,
                            "error_message": None,
                            "matched_issue_cv_id": None,
                            "suggested_issue_cv_id": 501,
                            "suggested_issue_label": "#1 - Issue One",
                            "decision_locked": False,
                            "diagnostics": {},
                        }
                    ],
                    "requires_library_root": True,
                    "selected_library_root_id": None,
                    "available_library_roots": [{"id": 9, "name": "Main", "path": "/library/main"}],
                    "files_remaining": 1,
                    "files_completed": 0,
                }
                mock_build_service.return_value = mock_service
                mock_progress_state.return_value = MagicMock(
                    state="running",
                    model_dump=MagicMock(
                        return_value={
                            "imported_series_id": 7,
                            "state": "running",
                            "message": "Recovering file 1 of 1",
                            "current_file_name": "Henchgirl 001.cbz",
                            "current_file_stage": "transferring",
                            "current_file_progress_current": 1,
                            "current_file_progress_total": 2,
                            "current_file_progress_pct": 75,
                            "current_file_progress_unit": "steps",
                            "file_index": 1,
                            "total_files": 1,
                        }
                    ),
                )

                await import_orphaned_recovery(7, request, MagicMock(), session)

                ctx = mock_templates.TemplateResponse.call_args[0][2]
                assert ctx["imported_series"].status == ImportSeriesStatus.RECOVERY_PENDING
                assert ctx["requires_library_root"] is True
                assert ctx["files_remaining"] == 1
                assert ctx["issue_options"][0]["issue_cv_id"] == 501
                assert ctx["recovery_progress"]["state"] == "running"
