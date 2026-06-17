"""Tests for Import Jobs REST API — Phase CI-5, Task CI-5.1.

Covers:
- POST /api/v1/import — create job + trigger scan
- GET /api/v1/import/{id} — job detail
- GET /api/v1/import/{id}/preview — paginated series review
- POST /api/v1/import/{id}/confirm — confirm series for import
- POST /api/v1/import/{id}/series/{sid}/override — set user CV ID
- DELETE /api/v1/import/{id} — discard pre-review or delete history row
- POST /api/v1/import/{id}/cancel — request active-job cancellation
- POST /api/v1/import/{id}/retry — create a fresh retry job from history
- GET /api/v1/import/{id}/stream — SSE progress stream
- GET /api/v1/import/{id}/logs — paginated job logs
- GET /api/v1/import/{id}/logs/download — text/plain log download
- Auth required on all endpoints

Run:
    pytest tests/api/test_import_api.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Imported for direct handler tests
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models import Base
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
from pullbox.models.user import APIKey, User
from pullbox.schemas.import_job import ImportJobCreate, OrphanRecoveryProgressResponse
from pullbox.services.auth_service import AuthService

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-import")


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
async def _api_key_header(
    _db_factory: async_sessionmaker[AsyncSession],
) -> str:
    """Create a test user + API key, return the raw key string."""
    raw_key = "pb_k1_" + "b" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="importuser",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="import-test"))
        await session.commit()
    return raw_key


@pytest.fixture
async def client(
    _db_factory: async_sessionmaker[AsyncSession],
    _api_key_header: str,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client authenticated via API key."""
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()
    app.state.db_session_factory = _db_factory

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with _db_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_dep] = _override_db
    reset_setup_cache()

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-Api-Key": _api_key_header},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


@pytest.fixture
async def unauth_client(
    _db_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client with NO authentication."""
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()
    app.state.db_session_factory = _db_factory

    async def _override_db() -> AsyncGenerator[AsyncSession, None]:
        async with _db_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db_dep] = _override_db
    reset_setup_cache()

    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    reset_setup_cache()


async def _seed_orphan_data(
    factory: async_sessionmaker[AsyncSession],
    *,
    orphan_count: int = 3,
    recovery_pending_count: int = 0,
    failed_count: int = 0,
    imported_count: int = 0,
    imported_issue_recovery_count: int = 0,
) -> int:
    """Create a COMPLETED job with active unmatched, FAILED, and IMPORTED series."""
    async with factory() as session:
        job = ImportJob(
            source_path="/tmp/orphan-test",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.COMPLETED,
            series_imported=imported_count,
            series_failed=failed_count,
        )
        session.add(job)
        await session.flush()

        for i in range(orphan_count):
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
        for i in range(failed_count):
            session.add(
                ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name=f"Failed {i}",
                    file_count=3,
                    status=ImportSeriesStatus.FAILED,
                    cv_id=9000 + i,
                )
            )
        for i in range(imported_count):
            from pullbox.models.series import Series, SeriesStatus

            series = Series(
                title=f"Imported {i}",
                sort_title=f"Imported {i}",
                year_start=2020,
                comicvine_id=8000 + i,
                status=SeriesStatus.CONTINUING,
                issue_count=0,
            )
            session.add(series)
            await session.flush()
            session.add(
                ImportedSeries(
                    import_job_id=job.id,
                    raw_series_name=f"Imported {i}",
                    file_count=10,
                    status=ImportSeriesStatus.IMPORTED,
                    cv_id=8000 + i,
                    series_id=series.id,
                )
            )

        for i in range(imported_issue_recovery_count):
            from pullbox.models.series import Series, SeriesStatus

            series = Series(
                title=f"Issue Recovery {i}",
                sort_title=f"Issue Recovery {i}",
                year_start=2020,
                comicvine_id=8500 + i,
                status=SeriesStatus.CONTINUING,
                issue_count=1,
            )
            session.add(series)
            await session.flush()
            imported_series = ImportedSeries(
                import_job_id=job.id,
                raw_series_name=f"Issue Recovery {i}",
                file_count=3,
                status=ImportSeriesStatus.IMPORTED,
                cv_id=8500 + i,
                series_id=series.id,
                files_imported=2,
                files_no_match=1,
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
                cv_match_score=0.95 if series_status == ImportSeriesStatus.MATCHED else None,
            )
            session.add(item)

        for i in range(with_logs):
            level = "INFO" if i % 3 != 0 else "WARNING"
            log = ImportJobLog(
                import_job_id=job.id,
                level=level,
                event=f"test_event_{i}",
                message=f"Test message {i}",
                data={"index": i},
            )
            session.add(log)

        await session.commit()
        return job.id


async def _seed_duplicate_review_selection_job(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int, int]:
    """Create a review job with a duplicate-series file ready for selection tests."""
    async with factory() as session:
        job = ImportJob(
            source_path="/tmp/duplicate-review",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
        )
        session.add(job)
        await session.flush()

        existing_series = Series(
            title="Batman",
            sort_title="batman",
            year_start=2016,
            comicvine_id=97508,
        )
        session.add(existing_series)
        await session.flush()

        duplicate_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="Batman",
            raw_year=2016,
            status=ImportSeriesStatus.DUPLICATE,
            series_id=existing_series.id,
            diagnostics={
                "duplicate_reason": "cv_id",
                "actionable_duplicate_merge": True,
            },
        )
        session.add(duplicate_series)
        await session.flush()

        imp_file = ImportedFile(
            import_job_id=job.id,
            import_series_id=duplicate_series.id,
            file_path="/tmp/Batman 001.cbz",
            file_name="Batman 001.cbz",
            file_size=1024,
            file_format="cbz",
            status=ImportedFileStatus.MATCHED,
            include_in_import=True,
        )
        session.add(imp_file)
        await session.commit()
        return job.id, duplicate_series.id, imp_file.id


async def _seed_matched_review_selection_job(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[int, list[int]]:
    """Create a review job with matched series ready for server-side selection tests."""
    async with factory() as session:
        job = ImportJob(
            source_path="/tmp/matched-review",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
        )
        session.add(job)
        await session.flush()

        series_ids: list[int] = []
        for idx in range(2):
            imported_series = ImportedSeries(
                import_job_id=job.id,
                raw_series_name=f"Matched {idx}",
                raw_year=2020 + idx,
                status=ImportSeriesStatus.MATCHED,
                file_count=3,
                files_total=3,
                files_matched=3,
                selected_for_import=idx == 0,
            )
            session.add(imported_series)
            await session.flush()
            series_ids.append(imported_series.id)

        await session.commit()
        return job.id, series_ids


async def _seed_mixed_review_selection_job(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[int, list[int], int]:
    """Create a review job with matched, conflicted, duplicate, and no-match rows."""
    async with factory() as session:
        job = ImportJob(
            source_path="/tmp/mixed-review",
            source_type=ImportSourceType.FILESYSTEM,
            status=ImportJobStatus.REVIEW,
        )
        session.add(job)
        await session.flush()

        matched_ids: list[int] = []
        for idx in range(3):
            imported_series = ImportedSeries(
                import_job_id=job.id,
                raw_series_name=f"Matched {idx}",
                raw_year=2020 + idx,
                status=ImportSeriesStatus.MATCHED,
                file_count=1,
                files_total=1,
                files_matched=1,
                files_conflict=0 if idx < 2 else 1,
                selected_for_import=idx == 0,
            )
            session.add(imported_series)
            await session.flush()
            matched_ids.append(imported_series.id)

        existing_series = Series(
            title="In Library",
            sort_title="in library",
            year_start=2024,
            comicvine_id=123456,
        )
        session.add(existing_series)
        await session.flush()

        duplicate_series = ImportedSeries(
            import_job_id=job.id,
            raw_series_name="In Library",
            raw_year=2024,
            status=ImportSeriesStatus.DUPLICATE,
            series_id=existing_series.id,
            files_total=1,
            files_matched=1,
            diagnostics={
                "duplicate_reason": "cv_id",
                "actionable_duplicate_merge": True,
            },
        )
        session.add(duplicate_series)
        await session.flush()
        session.add(
            ImportedFile(
                import_job_id=job.id,
                import_series_id=duplicate_series.id,
                file_path="/tmp/In Library 001.cbz",
                file_name="In Library 001.cbz",
                file_size=1024,
                file_format="cbz",
                status=ImportedFileStatus.MATCHED,
                include_in_import=True,
            )
        )

        session.add(
            ImportedSeries(
                import_job_id=job.id,
                raw_series_name="Needs Work",
                raw_year=2024,
                status=ImportSeriesStatus.NO_MATCH,
                file_count=1,
                files_total=1,
                files_no_match=1,
            )
        )
        await session.commit()
        return job.id, matched_ids, duplicate_series.id


# ── Tests: POST /import ──────────────────────────────────────────────


class TestCreateImportJob:
    """Test POST /api/v1/import."""

    @pytest.mark.asyncio
    async def test_create_job(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
        tmp_path: object,
    ) -> None:
        """POST /import creates job and returns 201."""
        with patch("pullbox.api.v1.import_jobs.trigger_import_scan") as mock_trigger:
            resp = await client.post(
                "/api/v1/import",
                json={
                    "source_path": str(tmp_path),
                    "source_type": "filesystem",
                },
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "pending"
        assert data["source_type"] == "filesystem"
        mock_trigger.assert_called_once_with(data["id"])

    @pytest.mark.asyncio
    async def test_create_job_invalid_path(
        self,
        client: AsyncClient,
    ) -> None:
        """POST /import with non-existent path returns 422."""
        resp = await client.post(
            "/api/v1/import",
            json={
                "source_path": "/nonexistent/path/that/should/not/exist",
                "source_type": "filesystem",
            },
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_job_invalid_source_type(
        self,
        client: AsyncClient,
        tmp_path: object,
    ) -> None:
        """POST /import with invalid source_type returns 422."""
        resp = await client.post(
            "/api/v1/import",
            json={
                "source_path": str(tmp_path),
                "source_type": "invalid_type",
            },
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_job_rejects_existing_active_import(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
        tmp_path: object,
    ) -> None:
        """POST /import returns 409 when another import is already active."""
        await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW)

        resp = await client.post(
            "/api/v1/import",
            json={
                "source_path": str(tmp_path),
                "source_type": "filesystem",
            },
        )

        assert resp.status_code == 409
        assert "Only one import can be active" in resp.json()["detail"]


# ── Tests: GET /import/{id} ─────────────────────────────────────────


class TestGetImportJob:
    """Test GET /api/v1/import/{id}."""

    @pytest.mark.asyncio
    async def test_get_job(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /import/{id} returns ImportJobRead."""
        job_id = await _seed_import_job(_db_factory)
        resp = await client.get(f"/api/v1/import/{job_id}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == job_id
        assert data["status"] == "pending"
        assert data["source_path"] == "/tmp/comics"

    @pytest.mark.asyncio
    async def test_get_job_not_found(
        self,
        client: AsyncClient,
    ) -> None:
        """GET /import/{id} returns 404 for unknown id."""
        resp = await client.get("/api/v1/import/99999")
        assert resp.status_code == 404


# ── Tests: GET /import/{id}/preview ──────────────────────────────────


class TestGetPreview:
    """Test GET /api/v1/import/{id}/preview."""

    @pytest.mark.asyncio
    async def test_preview_returns_series(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /import/{id}/preview returns ImportPreviewResponse."""
        job_id = await _seed_import_job(
            _db_factory,
            status=ImportJobStatus.REVIEW,
            with_series=6,
        )
        resp = await client.get(f"/api/v1/import/{job_id}/preview")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 6
        assert len(data["items"]) == 6
        assert data["job"]["id"] == job_id

    @pytest.mark.asyncio
    async def test_preview_status_filter(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /import/{id}/preview?status=matched filters correctly."""
        job_id = await _seed_import_job(
            _db_factory,
            status=ImportJobStatus.REVIEW,
            with_series=6,
        )
        resp = await client.get(f"/api/v1/import/{job_id}/preview?status=matched")

        assert resp.status_code == 200
        data = resp.json()
        # Seeding alternates matched/no_match, so 3 matched out of 6
        assert data["total"] == 3

    @pytest.mark.asyncio
    async def test_preview_pagination(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /import/{id}/preview?page=2&page_size=2 paginates."""
        job_id = await _seed_import_job(
            _db_factory,
            status=ImportJobStatus.REVIEW,
            with_series=6,
        )
        resp = await client.get(f"/api/v1/import/{job_id}/preview?page=2&page_size=2")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 6
        assert data["page"] == 2
        assert data["page_size"] == 2
        assert len(data["items"]) == 2


# ── Tests: POST /import/{id}/confirm ─────────────────────────────────


class TestConfirmImport:
    """Test POST /api/v1/import/{id}/confirm."""

    @pytest.mark.asyncio
    async def test_confirm_succeeds(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST /import/{id}/confirm confirms series and triggers execute."""
        job_id = await _seed_import_job(
            _db_factory,
            status=ImportJobStatus.REVIEW,
            with_series=4,
        )

        async with _db_factory() as session:
            from sqlalchemy import select

            matched_series = list(
                (
                    await session.execute(
                        select(ImportedSeries).where(
                            ImportedSeries.import_job_id == job_id,
                            ImportedSeries.status == ImportSeriesStatus.MATCHED,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for imported_series in matched_series:
                imported_series.selected_for_import = True
            await session.commit()
            matched_ids = [imported_series.id for imported_series in matched_series]

        with patch("pullbox.api.v1.import_jobs.trigger_import_execute") as mock_trigger:
            resp = await client.post(
                f"/api/v1/import/{job_id}/confirm",
                json={"series_ids": matched_ids},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "importing"
        mock_trigger.assert_called_once_with(job_id)

    @pytest.mark.asyncio
    async def test_confirm_ignores_stale_client_ids_when_server_selection_exists(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST /confirm should use durable selection instead of stale browser ids."""
        job_id = await _seed_import_job(
            _db_factory,
            status=ImportJobStatus.REVIEW,
            with_series=2,
        )

        async with _db_factory() as session:
            from sqlalchemy import select

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

        with patch("pullbox.api.v1.import_jobs.trigger_import_execute") as mock_trigger:
            resp = await client.post(
                f"/api/v1/import/{job_id}/confirm",
                json={"series_ids": [99999]},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "importing"
        mock_trigger.assert_called_once_with(job_id)

    @pytest.mark.asyncio
    async def test_confirm_wrong_state(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST /import/{id}/confirm on non-REVIEW job returns 409."""
        job_id = await _seed_import_job(
            _db_factory,
            status=ImportJobStatus.SCANNING,
        )

        resp = await client.post(
            f"/api/v1/import/{job_id}/confirm",
            json={"series_ids": [1]},
        )
        assert resp.status_code == 409


class TestSeriesSelection:
    """Test server-owned matched-series review selection endpoints."""

    @pytest.mark.asyncio
    async def test_update_series_selection(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        job_id, series_ids = await _seed_matched_review_selection_job(_db_factory)

        resp = await client.put(
            f"/api/v1/import/{job_id}/series/{series_ids[1]}/selection",
            json={"include_in_import": True},
        )

        assert resp.status_code == 200
        assert resp.json()["selected_for_import"] is True

    @pytest.mark.asyncio
    async def test_bulk_update_series_selection(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        job_id, series_ids = await _seed_matched_review_selection_job(_db_factory)

        resp = await client.post(
            f"/api/v1/import/{job_id}/series/selection-bulk",
            json={"include_in_import": True, "imported_series_ids": series_ids},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["updated"] == 2
        assert data["include_in_import"] is True
        assert data["selection_state"]["matched_series_selected"] == 2
        assert data["selection_state"]["selected_item_count"] == 2

    @pytest.mark.asyncio
    async def test_selection_state_counts_importable_items_across_review_types(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Selection state is the canonical item count for Step 3."""
        job_id, matched_ids, duplicate_series_id = await _seed_mixed_review_selection_job(
            _db_factory
        )

        resp = await client.get(f"/api/v1/import/{job_id}/selection-state")

        assert resp.status_code == 200
        data = resp.json()
        assert data["matched_series_importable"] == 2
        assert data["matched_series_selected"] == 1
        assert data["duplicate_series_importable"] == 1
        assert data["duplicate_series_selected"] == 1
        assert data["importable_item_count"] == 3
        assert data["selected_item_count"] == 2
        assert data["selected_series_ids"] == [matched_ids[0]]
        assert data["selected_duplicate_series_ids"] == [duplicate_series_id]
        assert data["duplicate_selected_file_counts"][str(duplicate_series_id)] == 1

    @pytest.mark.asyncio
    async def test_bulk_select_series_selection_selects_all_importable_rows(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        job_id, series_ids = await _seed_matched_review_selection_job(_db_factory)

        resp = await client.post(
            f"/api/v1/import/{job_id}/series/selection-bulk",
            json={"include_in_import": True, "imported_series_ids": []},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["updated"] == 2
        assert data["include_in_import"] is True
        assert data["selection_state"]["importable_item_count"] == 2
        assert data["selection_state"]["selected_item_count"] == 2

        async with _db_factory() as session:
            from sqlalchemy import select

            selected_ids = (
                (
                    await session.execute(
                        select(ImportedSeries.id).where(
                            ImportedSeries.import_job_id == job_id,
                            ImportedSeries.selected_for_import.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert set(selected_ids) == set(series_ids)

    @pytest.mark.asyncio
    async def test_bulk_clear_series_selection(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        job_id, _series_ids = await _seed_matched_review_selection_job(_db_factory)

        resp = await client.post(
            f"/api/v1/import/{job_id}/series/selection-bulk",
            json={"include_in_import": False, "imported_series_ids": []},
        )

        assert resp.status_code == 200
        assert resp.json()["include_in_import"] is False
        assert resp.json()["selection_state"]["selected_item_count"] == 0

        async with _db_factory() as session:
            from sqlalchemy import select

            selected_ids = (
                (
                    await session.execute(
                        select(ImportedSeries.id).where(
                            ImportedSeries.import_job_id == job_id,
                            ImportedSeries.selected_for_import.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert selected_ids == []


# ── Tests: POST /import/{id}/series/{sid}/override ───────────────────


class TestOverrideCvId:
    """Test POST /api/v1/import/{id}/series/{sid}/override."""

    @pytest.mark.asyncio
    async def test_override_sets_cv_id(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST override sets user CV ID and returns updated series."""
        job_id = await _seed_import_job(
            _db_factory,
            status=ImportJobStatus.REVIEW,
            with_series=2,
        )

        # Get a series ID
        async with _db_factory() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(ImportedSeries.id).where(
                    ImportedSeries.import_job_id == job_id,
                )
            )
            series_id = result.scalars().first()

        # Mock the service's override method to avoid real CV calls
        mock_item = MagicMock()
        mock_item.id = series_id
        mock_item.status = ImportSeriesStatus.MATCHED
        mock_item.raw_series_name = "Test Series"
        mock_item.raw_year = 2020
        mock_item.raw_publisher = None
        mock_item.file_count = 5
        mock_item.has_files = True
        mock_item.sample_paths = []
        mock_item.source_folder = None
        mock_item.cv_id = 12345
        mock_item.cv_title = "CV Series"
        mock_item.cv_year = 2020
        mock_item.cv_publisher = "DC"
        mock_item.cv_issue_count = 50
        mock_item.cv_url = "https://comicvine.gamespot.com/test"
        mock_item.cv_match_score = 1.0
        mock_item.cv_match_method = "user_override"
        mock_item.user_selected_cv_id = 12345
        mock_item.files_total = 5
        mock_item.files_matched = 5
        mock_item.files_duplicate = 0
        mock_item.files_already_owned = 0
        mock_item.files_conflict = 0
        mock_item.files_no_match = 0
        mock_item.files_imported = 0
        mock_item.files_failed = 0
        mock_item.series_id = None
        mock_item.error_message = None
        mock_item.diagnostics = {}

        with (
            patch(
                "pullbox.api.v1.import_jobs._build_interactive_import_service",
                new_callable=AsyncMock,
            ) as mock_build,
            patch("pullbox.api.v1.import_jobs.trigger_import_series_rematch") as mock_trigger,
        ):
            mock_service = AsyncMock()
            mock_service.override_cv_id.return_value = mock_item
            mock_build.return_value = mock_service

            resp = await client.post(
                f"/api/v1/import/{job_id}/series/{series_id}/override",
                json={"cv_id": 12345},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["cv_id"] == 12345
        assert data["files_conflict"] == 0
        mock_service.override_cv_id.assert_awaited_once_with(
            ANY,
            series_id,
            12345,
            rematch_files=False,
        )
        mock_trigger.assert_called_once_with(job_id, series_id)


# ── Tests: POST /import/{id}/series/{sid}/reconcile ───────────────────


class TestReconcileImportSeries:
    """Test POST /api/v1/import/{id}/series/{sid}/reconcile."""

    @pytest.mark.asyncio
    async def test_reconcile_import_series_assigns_review_decisions(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST reconcile saves Step 3 decisions without starting file import."""
        job_id = await _seed_import_job(
            _db_factory,
            status=ImportJobStatus.REVIEW,
            with_series=1,
        )
        async with _db_factory() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(ImportedSeries.id).where(ImportedSeries.import_job_id == job_id)
            )
            series_id = result.scalar_one()

        mock_item = MagicMock()
        mock_item.id = series_id
        mock_item.status = ImportSeriesStatus.MATCHED
        mock_item.raw_series_name = "Powers 25"
        mock_item.raw_year = 2025
        mock_item.raw_publisher = "Dark Horse"
        mock_item.file_count = 1
        mock_item.has_files = True
        mock_item.sample_paths = []
        mock_item.source_folder = None
        mock_item.cv_id = 166903
        mock_item.cv_title = "Powers 25"
        mock_item.cv_year = 2025
        mock_item.cv_publisher = "Dark Horse"
        mock_item.cv_issue_count = 9
        mock_item.cv_url = "https://comicvine.gamespot.com/powers-25/4050-166903/"
        mock_item.cv_match_score = 1.0
        mock_item.cv_match_method = "user_override"
        mock_item.user_selected_cv_id = 166903
        mock_item.selected_for_import = False
        mock_item.files_total = 1
        mock_item.files_matched = 1
        mock_item.files_duplicate = 0
        mock_item.files_already_owned = 0
        mock_item.files_conflict = 0
        mock_item.files_no_match = 0
        mock_item.files_imported = 0
        mock_item.files_failed = 0
        mock_item.series_id = None
        mock_item.error_message = None
        mock_item.diagnostics = {}

        with patch(
            "pullbox.api.v1.import_jobs._build_interactive_import_service",
            new_callable=AsyncMock,
        ) as mock_build:
            mock_service = AsyncMock()
            mock_service.reconcile_import_series.return_value = mock_item
            mock_build.return_value = mock_service

            resp = await client.post(
                f"/api/v1/import/{job_id}/series/{series_id}/reconcile",
                json={
                    "decisions": [
                        {
                            "imported_file_id": 77,
                            "action": "assign",
                            "issue_cv_id": 1167175,
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "matched"
        assert data["selected_for_import"] is False
        mock_service.reconcile_import_series.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reconcile_import_series_accepts_provisional_issue_decision(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST reconcile can save a manual local issue when ComicVine is stale."""
        job_id = await _seed_import_job(
            _db_factory,
            status=ImportJobStatus.REVIEW,
            with_series=1,
        )
        async with _db_factory() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(ImportedSeries.id).where(ImportedSeries.import_job_id == job_id)
            )
            series_id = result.scalar_one()

        mock_item = MagicMock()
        mock_item.id = series_id
        mock_item.status = ImportSeriesStatus.MATCHED
        mock_item.raw_series_name = "King Dracula"
        mock_item.raw_year = 2026
        mock_item.raw_publisher = "Zenescope"
        mock_item.file_count = 1
        mock_item.has_files = True
        mock_item.sample_paths = []
        mock_item.source_folder = None
        mock_item.cv_id = 169964
        mock_item.cv_title = "King Dracula"
        mock_item.cv_year = 2025
        mock_item.cv_publisher = "Zenescope"
        mock_item.cv_issue_count = 3
        mock_item.cv_url = "https://comicvine.gamespot.com/king-dracula/4050-169964/"
        mock_item.cv_match_score = 1.0
        mock_item.cv_match_method = "user_override"
        mock_item.user_selected_cv_id = 169964
        mock_item.selected_for_import = False
        mock_item.files_total = 1
        mock_item.files_matched = 1
        mock_item.files_duplicate = 0
        mock_item.files_already_owned = 0
        mock_item.files_conflict = 0
        mock_item.files_no_match = 0
        mock_item.files_imported = 0
        mock_item.files_failed = 0
        mock_item.series_id = None
        mock_item.error_message = None
        mock_item.diagnostics = {}

        with patch(
            "pullbox.api.v1.import_jobs._build_interactive_import_service",
            new_callable=AsyncMock,
        ) as mock_build:
            mock_service = AsyncMock()
            mock_service.reconcile_import_series.return_value = mock_item
            mock_build.return_value = mock_service

            resp = await client.post(
                f"/api/v1/import/{job_id}/series/{series_id}/reconcile",
                json={
                    "decisions": [
                        {
                            "imported_file_id": 77,
                            "action": "provisional",
                            "provisional_issue_number": 4,
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "matched"
        request = mock_service.reconcile_import_series.await_args.args[3]
        assert request.decisions[0].action == "provisional"
        assert request.decisions[0].provisional_issue_number == 4.0


# ── Tests: DELETE /import/{id} ───────────────────────────────────────


class TestCancelImportJob:
    """Test DELETE /api/v1/import/{id}."""

    @pytest.mark.asyncio
    async def test_cancel_pending_job(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """DELETE /import/{id} cancels a PENDING job."""
        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.PENDING)

        resp = await client.delete(f"/api/v1/import/{job_id}")
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_review_job_discards_scan_results(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """DELETE /import/{id} discards a review job and its scan results."""
        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW)

        resp = await client.delete(f"/api/v1/import/{job_id}")
        assert resp.status_code == 204

        async with _db_factory() as session:
            job = await session.get(ImportJob, job_id)
            assert job is None

    @pytest.mark.asyncio
    async def test_cancel_importing_job_returns_409(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """DELETE /import/{id} on IMPORTING job returns 409."""
        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.IMPORTING)

        resp = await client.delete(f"/api/v1/import/{job_id}")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            ImportJobStatus.FAILED,
            ImportJobStatus.COMPLETED,
            ImportJobStatus.CANCELLED,
        ],
    )
    async def test_delete_terminal_job_history_row(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
        status: ImportJobStatus,
    ) -> None:
        """DELETE /import/{id} removes finished jobs from history."""
        job_id = await _seed_import_job(_db_factory, status=status)

        resp = await client.delete(f"/api/v1/import/{job_id}")
        assert resp.status_code == 204

        async with _db_factory() as session:
            job = await session.get(ImportJob, job_id)
            assert job is None

    @pytest.mark.asyncio
    async def test_cancel_scanning_job_marks_cancelled_and_clears_progress_queue(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """DELETE /import/{id} rejects active scan jobs in favor of explicit controls."""
        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.SCANNING)

        resp = await client.delete(f"/api/v1/import/{job_id}")
        assert resp.status_code == 409

        async with _db_factory() as session:
            job = await session.get(ImportJob, job_id)
            assert job is not None
            assert job.status == ImportJobStatus.SCANNING

    @pytest.mark.asyncio
    async def test_clear_import_history_deletes_only_terminal_jobs(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """DELETE /import/history removes terminal jobs and preserves active ones."""
        completed_id = await _seed_import_job(_db_factory, status=ImportJobStatus.COMPLETED)
        failed_id = await _seed_import_job(_db_factory, status=ImportJobStatus.FAILED)
        cancelled_id = await _seed_import_job(_db_factory, status=ImportJobStatus.CANCELLED)
        review_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW)
        scanning_id = await _seed_import_job(_db_factory, status=ImportJobStatus.SCANNING)

        resp = await client.delete("/api/v1/import/history")
        assert resp.status_code == 200
        assert resp.json() == {"deleted": 3}

        async with _db_factory() as session:
            assert await session.get(ImportJob, completed_id) is None
            assert await session.get(ImportJob, failed_id) is None
            assert await session.get(ImportJob, cancelled_id) is None
            assert await session.get(ImportJob, review_id) is not None
            assert await session.get(ImportJob, scanning_id) is not None


# ── Tests: GET /import/{id}/stream ───────────────────────────────────


class TestSSEStream:
    """Test GET /api/v1/import/{id}/stream."""

    def test_stream_routes_do_not_hold_request_scoped_db_sessions(self) -> None:
        """Import SSE routes authenticate without keeping DbSession open for the stream."""
        from pullbox.api.deps import get_db_dep, require_stream_auth
        from pullbox.app import create_app
        from tests.route_contracts import RouteContract, iter_api_route_contracts

        def iter_dependency_calls(route: RouteContract) -> set[object]:
            calls: set[object] = set()
            stack = list(route.dependant.dependencies)
            while stack:
                dependency = stack.pop()
                calls.add(dependency.call)
                stack.extend(dependency.dependencies)
            return calls

        app = create_app()
        stream_paths = {
            "/api/v1/import/{job_id}/stream",
            "/api/v1/import/{job_id}/logs/stream",
        }

        matched_paths: set[str] = set()
        for route in iter_api_route_contracts(app.routes):
            if route.path not in stream_paths:
                continue
            matched_paths.add(route.path)
            dependency_calls = iter_dependency_calls(route)
            assert require_stream_auth in dependency_calls
            assert get_db_dep not in dependency_calls

        assert matched_paths == stream_paths

    @pytest.mark.asyncio
    async def test_stream_returns_event_stream(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /import/{id}/stream returns text/event-stream content type."""
        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.SCANNING)
        async with _db_factory() as session:
            job = await session.get(ImportJob, job_id)
            assert job is not None
            job.progress_snapshot = {
                "snapshot_version": 2,
                "job_id": job_id,
                "status": ImportJobStatus.COMPLETED.value,
                "mode": "import",
                "phase": "done",
                "progress": 100,
                "message": "Done",
                "progress_revision": 5,
            }
            job.status = ImportJobStatus.COMPLETED
            job.progress_revision = 5
            await session.commit()

        resp = await client.get(f"/api/v1/import/{job_id}/stream")

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        assert "data:" in resp.text


# ── Tests: GET /import/{id}/logs ─────────────────────────────────────


class TestJobLogs:
    """Test GET /api/v1/import/{id}/logs."""

    @pytest.mark.asyncio
    async def test_logs_returns_entries(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /import/{id}/logs returns ImportJobLogsResponse with entries."""
        job_id = await _seed_import_job(_db_factory, with_logs=5)

        resp = await client.get(f"/api/v1/import/{job_id}/logs")

        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == job_id
        assert data["total"] == 5
        assert len(data["items"]) == 5

    @pytest.mark.asyncio
    async def test_logs_filter_by_level(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /import/{id}/logs?level=WARNING filters to WARNING+ only."""
        job_id = await _seed_import_job(_db_factory, with_logs=6)

        resp = await client.get(f"/api/v1/import/{job_id}/logs?level=WARNING")

        assert resp.status_code == 200
        data = resp.json()
        # Seeding: every 3rd log (i=0,3) is WARNING, rest are INFO
        # With level=WARNING filter, only WARNING+ entries should return
        assert data["total"] == 2
        for item in data["items"]:
            assert item["level"] in ("WARNING", "ERROR")

    @pytest.mark.asyncio
    async def test_logs_pagination(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /import/{id}/logs?page=2&page_size=2 paginates."""
        job_id = await _seed_import_job(_db_factory, with_logs=5)

        resp = await client.get(f"/api/v1/import/{job_id}/logs?page=2&page_size=2")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert data["page"] == 2
        assert data["page_size"] == 2
        assert len(data["items"]) == 2

    @pytest.mark.asyncio
    async def test_logs_descending_order(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /import/{id}/logs?order=desc returns newest entries first."""
        job_id = await _seed_import_job(_db_factory, with_logs=4)

        resp = await client.get(f"/api/v1/import/{job_id}/logs?order=desc")

        assert resp.status_code == 200
        data = resp.json()
        assert [item["id"] for item in data["items"]] == sorted(
            [item["id"] for item in data["items"]],
            reverse=True,
        )

    @pytest.mark.asyncio
    async def test_logs_empty_job(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /import/{id}/logs on job with no log entries returns empty list."""
        job_id = await _seed_import_job(_db_factory, with_logs=0)

        resp = await client.get(f"/api/v1/import/{job_id}/logs")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []


# ── Tests: GET /import/{id}/logs/download ────────────────────────────


class TestLogDownload:
    """Test GET /api/v1/import/{id}/logs/download."""

    @pytest.mark.asyncio
    async def test_download_returns_text(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /import/{id}/logs/download returns text/plain attachment."""
        job_id = await _seed_import_job(_db_factory, with_logs=3)

        resp = await client.get(f"/api/v1/import/{job_id}/logs/download")

        assert resp.status_code == 200
        assert "text/plain" in resp.headers.get("content-type", "")
        cd = resp.headers.get("content-disposition", "")
        assert cd.startswith("attachment; filename=")
        assert f"import_job_{job_id}_" in cd
        assert cd.endswith('.log"')
        # Should have 3 lines of log entries
        lines = [line for line in resp.text.strip().split("\n") if line]
        assert len(lines) == 3

    @pytest.mark.asyncio
    async def test_download_empty(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /import/{id}/logs/download with no logs returns comment line."""
        job_id = await _seed_import_job(_db_factory, with_logs=0)

        resp = await client.get(f"/api/v1/import/{job_id}/logs/download")

        assert resp.status_code == 200
        assert resp.text.startswith("#")


# ── Tests: GET /import/orphaned ────────────────────────────────────────


class TestOrphanedEndpoints:
    """Test orphaned series API endpoints."""

    @pytest.mark.asyncio
    async def test_list_orphaned(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /import/orphaned returns OrphanedSeriesResponse."""
        await _seed_orphan_data(
            _db_factory,
            orphan_count=2,
            recovery_pending_count=1,
            imported_issue_recovery_count=1,
        )

        resp = await client.get("/api/v1/import/orphaned")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 4
        assert len(data["items"]) == 4
        assert data["page"] == 1
        assert {item["status"] for item in data["items"]} == {
            "no_match",
            "recovery_pending",
            "imported",
        }

    @pytest.mark.asyncio
    async def test_list_orphaned_pagination(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /import/orphaned?page=1&page_size=2 paginates."""
        await _seed_orphan_data(_db_factory, orphan_count=5)

        resp = await client.get("/api/v1/import/orphaned?page=1&page_size=2")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

    @pytest.mark.asyncio
    async def test_orphaned_count(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /import/orphaned/count returns count."""
        await _seed_orphan_data(
            _db_factory,
            orphan_count=4,
            recovery_pending_count=2,
            imported_issue_recovery_count=1,
        )

        resp = await client.get("/api/v1/import/orphaned/count")

        assert resp.status_code == 200
        assert resp.json()["count"] == 7

    @pytest.mark.asyncio
    async def test_assign_orphan_success(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST /import/orphaned/{id}/assign returns AssignOrphanResponse."""
        job_id = await _seed_orphan_data(_db_factory, orphan_count=1)

        # Get the orphan ID
        async with _db_factory() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(ImportedSeries.id).where(
                    ImportedSeries.import_job_id == job_id,
                    ImportedSeries.status == ImportSeriesStatus.NO_MATCH,
                )
            )
            orphan_id = result.scalars().first()

        mock_item = MagicMock()
        mock_item.id = orphan_id
        mock_item.status = ImportSeriesStatus.RECOVERY_PENDING
        mock_item.cv_title = "Batman (2020)"
        mock_item.raw_series_name = "Batman"
        mock_item.files_total = 5

        with patch(
            "pullbox.api.v1.import_job_orphaned_routes._build_orphan_import_service",
            new_callable=AsyncMock,
        ) as mock_build:
            mock_svc = AsyncMock()
            mock_svc.assign_cv_to_orphan.return_value = mock_item
            mock_build.return_value = mock_svc

            resp = await client.post(
                f"/api/v1/import/orphaned/{orphan_id}/assign",
                json={"cv_id": 12345},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["imported_series_id"] == orphan_id
        assert data["status"] == "recovery_pending"
        assert data["cv_title"] == "Batman (2020)"
        assert data["recovery_required"] is True
        assert data["files_remaining"] == 5

    @pytest.mark.asyncio
    async def test_assign_orphan_not_found(
        self,
        client: AsyncClient,
    ) -> None:
        """POST /import/orphaned/99999/assign returns 404."""
        with patch(
            "pullbox.api.v1.import_job_orphaned_routes._build_orphan_import_service",
            new_callable=AsyncMock,
        ) as mock_build:
            mock_svc = AsyncMock()
            mock_svc.assign_cv_to_orphan.side_effect = NotFoundError("ImportedSeries", 99999)
            mock_build.return_value = mock_svc

            resp = await client.post(
                "/api/v1/import/orphaned/99999/assign",
                json={"cv_id": 12345},
            )

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_assign_orphan_provider_error(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST /import/orphaned/{id}/assign returns 422 on ProviderError."""
        from pullbox.core.exceptions import ProviderError

        job_id = await _seed_orphan_data(_db_factory, orphan_count=1)

        async with _db_factory() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(ImportedSeries.id).where(
                    ImportedSeries.import_job_id == job_id,
                    ImportedSeries.status == ImportSeriesStatus.NO_MATCH,
                )
            )
            orphan_id = result.scalars().first()

        with patch(
            "pullbox.api.v1.import_job_orphaned_routes._build_orphan_import_service",
            new_callable=AsyncMock,
        ) as mock_build:
            mock_svc = AsyncMock()
            mock_svc.assign_cv_to_orphan.side_effect = ProviderError("comicvine", "CV ID not found")
            mock_build.return_value = mock_svc

            resp = await client.post(
                f"/api/v1/import/orphaned/{orphan_id}/assign",
                json={"cv_id": 99999},
            )

        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_get_orphan_recovery_payload(
        self,
        client: AsyncClient,
    ) -> None:
        """GET /import/orphaned/{id}/recovery returns the guided recovery payload."""
        imported_series = ImportedSeries(
            id=7,
            import_job_id=3,
            raw_series_name="Henchgirl",
            raw_year=2016,
            raw_publisher="Scout",
            file_count=2,
            has_files=True,
            sample_paths=["/tmp/henchgirl.cbz"],
            source_folder="/tmp",
            status=ImportSeriesStatus.RECOVERY_PENDING,
            cv_id=12345,
            cv_title="Henchgirl",
            cv_year=2016,
            cv_publisher="Scout",
            cv_issue_count=4,
            cv_url="https://comicvine.gamespot.com/henchgirl/4050-12345/",
            user_selected_cv_id=12345,
            selected_for_import=False,
            files_total=2,
            files_matched=0,
            files_duplicate=0,
            files_already_owned=0,
            files_conflict=0,
            files_no_match=2,
            files_imported=0,
            files_failed=0,
            diagnostics={},
        )

        with patch(
            "pullbox.api.v1.import_job_orphaned_routes._build_orphan_import_service",
            new_callable=AsyncMock,
        ) as mock_build:
            mock_svc = AsyncMock()
            mock_svc.get_orphan_recovery_context.return_value = {
                "imported_series": imported_series,
                "issue_options": [
                    {
                        "issue_cv_id": 501,
                        "issue_number": 1.0,
                        "title": "Issue One",
                        "release_date": "2016-01-01",
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
                        "parsed_year": 2016,
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
                "available_library_roots": [{"id": 9, "name": "Main", "path": "/library"}],
                "files_remaining": 1,
                "files_completed": 0,
            }
            mock_build.return_value = mock_svc

            resp = await client.get("/api/v1/import/orphaned/7/recovery")

        assert resp.status_code == 200
        data = resp.json()
        assert data["imported_series"]["status"] == "recovery_pending"
        assert data["issue_options"][0]["issue_cv_id"] == 501
        assert data["files"][0]["suggested_issue_cv_id"] == 501
        assert data["requires_library_root"] is True

    @pytest.mark.asyncio
    async def test_recover_orphan_success(
        self,
        client: AsyncClient,
    ) -> None:
        """POST /import/orphaned/{id}/recover returns recovery summary data."""
        with patch(
            "pullbox.api.v1.import_job_orphaned_routes._build_orphan_import_service",
            new_callable=AsyncMock,
        ) as mock_build:
            mock_svc = AsyncMock()
            mock_svc.recover_orphan.return_value = {
                "imported_series_id": 7,
                "status": ImportSeriesStatus.IMPORTED,
                "series_id": 42,
                "imported_count": 1,
                "skipped_count": 1,
                "failed_count": 0,
                "files_remaining": 0,
            }
            mock_build.return_value = mock_svc

            resp = await client.post(
                "/api/v1/import/orphaned/7/recover",
                json={
                    "target_library_root_id": 9,
                    "decisions": [
                        {"imported_file_id": 11, "action": "assign", "issue_cv_id": 501},
                        {"imported_file_id": 12, "action": "skip"},
                    ],
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "imported"
        assert data["series_id"] == 42
        assert data["imported_count"] == 1
        assert data["skipped_count"] == 1

    @pytest.mark.asyncio
    async def test_start_orphan_recovery_run(
        self,
        client: AsyncClient,
    ) -> None:
        """POST /import/orphaned/{id}/recover/start returns initial live progress state."""
        with patch(
            "pullbox.api.v1.import_job_orphaned_routes.start_orphan_recovery_run",
            new_callable=AsyncMock,
        ) as mock_start:
            mock_start.return_value = OrphanRecoveryProgressResponse(
                imported_series_id=7,
                state="running",
                message="Preparing recovery import...",
                current_file_name=None,
                current_file_stage=None,
                current_file_progress_current=None,
                current_file_progress_total=None,
                current_file_progress_pct=None,
                current_file_progress_unit=None,
                file_index=None,
                total_files=1,
                result_status=None,
                imported_count=0,
                skipped_count=1,
                failed_count=0,
                files_remaining=None,
                error_message=None,
            )

            resp = await client.post(
                "/api/v1/import/orphaned/7/recover/start",
                json={
                    "target_library_root_id": 9,
                    "decisions": [
                        {"imported_file_id": 11, "action": "assign", "issue_cv_id": 501},
                        {"imported_file_id": 12, "action": "skip"},
                    ],
                },
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["state"] == "running"
        assert data["total_files"] == 1
        assert data["skipped_count"] == 1

    @pytest.mark.asyncio
    async def test_get_orphan_recovery_progress(
        self,
        client: AsyncClient,
    ) -> None:
        """GET /import/orphaned/{id}/recover/progress returns the current run snapshot."""
        with patch(
            "pullbox.api.v1.import_job_orphaned_routes.get_orphan_recovery_progress_state"
        ) as mock_get:
            mock_get.return_value = OrphanRecoveryProgressResponse(
                imported_series_id=7,
                state="running",
                message="Recovering file 1 of 1",
                current_file_name="Henchgirl 001.cbz",
                current_file_stage="transferring",
                current_file_progress_current=1,
                current_file_progress_total=2,
                current_file_progress_pct=75,
                current_file_progress_unit="steps",
                file_index=1,
                total_files=1,
                result_status=None,
                imported_count=0,
                skipped_count=0,
                failed_count=0,
                files_remaining=0,
                error_message=None,
            )

            resp = await client.get("/api/v1/import/orphaned/7/recover/progress")

        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "running"
        assert data["current_file_name"] == "Henchgirl 001.cbz"
        assert data["current_file_progress_pct"] == 75


class TestFileRepairEndpoints:
    """Repair and override endpoints for import review files."""

    @pytest.mark.asyncio
    async def test_repair_file_metadata_success(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW)
        async with _db_factory() as session:
            imported_series = ImportedSeries(
                import_job_id=job_id,
                raw_series_name="Chicken Devil",
                raw_year=2021,
                file_count=1,
                status=ImportSeriesStatus.MATCHED,
            )
            session.add(imported_series)
            await session.flush()
            imp_file = ImportedFile(
                import_job_id=job_id,
                import_series_id=imported_series.id,
                file_path="/tmp/chicken-devil.cb7",
                file_name="chicken-devil.cb7",
                file_size=1024,
                file_format="cb7",
                status=ImportedFileStatus.MATCHED,
            )
            session.add(imp_file)
            await session.commit()
            file_id = imp_file.id

        repaired = ImportedFile(
            id=file_id,
            import_job_id=job_id,
            import_series_id=1,
            file_path="/tmp/chicken-devil [Pullbox Repaired].cbz",
            file_name="chicken-devil [Pullbox Repaired].cbz",
            file_size=2048,
            file_format="cbz",
            parsed_series="Chicken Devil",
            parsed_issue_number=4.0,
            parsed_year=2022,
            has_comicinfo=True,
            comicvine_issue_id=905404,
            issue_number_raw="4",
            status=ImportedFileStatus.MATCHED,
            matched_issue_id=905404,
            match_confidence="high",
            match_method="manual_override",
            conflict_group_id=None,
            duplicate_group_id=None,
            duplicate_of_file_id=None,
            is_preferred=False,
            include_in_import=False,
            content_hash=None,
            library_file_id=None,
            error_message=None,
            diagnostics={},
            created_at=datetime.now(UTC),
        )

        with patch("pullbox.api.v1.import_jobs._make_import_service") as make_service:
            mock_service = MagicMock()
            mock_service.repair_file_metadata = AsyncMock(return_value=repaired)
            make_service.return_value = mock_service

            resp = await client.post(
                f"/api/v1/import/{job_id}/files/{file_id}/repair-metadata",
                json={"issue_id": 905404},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["file_format"] == "cbz"
        assert data["file_name"] == "chicken-devil [Pullbox Repaired].cbz"
        mock_service.repair_file_metadata.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dismiss_orphan(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST /import/orphaned/{id}/dismiss returns 204."""
        job_id = await _seed_orphan_data(_db_factory, orphan_count=1)

        async with _db_factory() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(ImportedSeries.id).where(
                    ImportedSeries.import_job_id == job_id,
                    ImportedSeries.status == ImportSeriesStatus.NO_MATCH,
                )
            )
            orphan_id = result.scalars().first()

        resp = await client.post(f"/api/v1/import/orphaned/{orphan_id}/dismiss")

        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_dismiss_not_found(
        self,
        client: AsyncClient,
    ) -> None:
        """POST /import/orphaned/99999/dismiss returns 404."""
        resp = await client.post("/api/v1/import/orphaned/99999/dismiss")

        assert resp.status_code == 404


# ── Tests: POST /import/{id}/retry-failed ────────────────────────────


class TestRetryFailedEndpoint:
    """Test retry-failed API endpoint."""

    @pytest.mark.asyncio
    async def test_retry_failed_success(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST /import/{id}/retry-failed returns 202 RetryFailedResponse."""
        job_id = await _seed_orphan_data(
            _db_factory, orphan_count=0, failed_count=2, imported_count=3
        )

        with patch("pullbox.api.v1.import_jobs.trigger_import_execute") as mock_trigger:
            resp = await client.post(f"/api/v1/import/{job_id}/retry-failed")

        assert resp.status_code == 202
        data = resp.json()
        assert data["job_id"] == job_id
        assert data["retrying_count"] == 2
        mock_trigger.assert_called_once_with(job_id)

    @pytest.mark.asyncio
    async def test_retry_failed_not_found(
        self,
        client: AsyncClient,
    ) -> None:
        """POST /import/99999/retry-failed returns 404."""
        with patch("pullbox.api.v1.import_jobs.trigger_import_execute"):
            resp = await client.post("/api/v1/import/99999/retry-failed")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_retry_failed_wrong_state(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST /import/{id}/retry-failed on REVIEW job returns 409."""
        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW)

        with patch("pullbox.api.v1.import_jobs.trigger_import_execute"):
            resp = await client.post(f"/api/v1/import/{job_id}/retry-failed")

        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_retry_failed_no_failures(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST /import/{id}/retry-failed with 0 FAILED rows returns 409."""
        job_id = await _seed_orphan_data(
            _db_factory, orphan_count=0, failed_count=0, imported_count=3
        )

        with patch("pullbox.api.v1.import_jobs.trigger_import_execute"):
            resp = await client.post(f"/api/v1/import/{job_id}/retry-failed")

        assert resp.status_code == 409


# ── Tests: POST /import/{id}/retry ───────────────────────────────────


class TestRetryImportEndpoint:
    """Test fresh retry API endpoint."""

    @pytest.mark.asyncio
    async def test_retry_import_creates_new_job_and_redirect(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "retry-source"
        source_dir.mkdir()

        async with _db_factory() as session:
            job = ImportJob(
                source_path=str(source_dir),
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.CANCELLED,
                monitored=True,
                search_on_add=True,
                cv_match_threshold=0.84,
                min_files_per_series=2,
                file_formats="cbz, pdf",
            )
            session.add(job)
            await session.commit()
            job_id = job.id

        with patch("pullbox.api.v1.import_jobs.trigger_import_scan") as mock_trigger:
            resp = await client.post(f"/api/v1/import/{job_id}/retry")

        assert resp.status_code == 202
        data = resp.json()
        assert data["job_id"] != job_id
        assert data["redirect_url"].endswith(
            f"/import?tab=collection&resume_job_id={data['job_id']}&resume_step=2"
        )
        mock_trigger.assert_called_once_with(data["job_id"])

        async with _db_factory() as session:
            new_job = await session.get(ImportJob, data["job_id"])
            assert new_job is not None
            assert new_job.status == ImportJobStatus.PENDING
            assert new_job.source_path == str(source_dir.resolve())
            assert new_job.monitored is True
            assert new_job.search_on_add is True
            assert new_job.cv_match_threshold == pytest.approx(0.84)

    @pytest.mark.asyncio
    async def test_retry_import_rejects_non_retryable_status(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        source_dir = tmp_path / "retry-source-completed"
        source_dir.mkdir()

        job_id = await _seed_import_job(
            _db_factory,
            status=ImportJobStatus.COMPLETED,
            source_path=str(source_dir),
        )

        with patch("pullbox.api.v1.import_jobs.trigger_import_scan"):
            resp = await client.post(f"/api/v1/import/{job_id}/retry")

        assert resp.status_code == 409
        assert "Only cancelled or rolled-back jobs" in resp.text


# ── Tests: Auth Required ─────────────────────────────────────────────


class TestAuthRequired:
    """All endpoints return 401 without valid session."""

    @pytest.mark.asyncio
    async def test_requires_auth(
        self,
        unauth_client: AsyncClient,
    ) -> None:
        """All endpoints return 401 without authentication."""
        endpoints = [
            ("POST", "/api/v1/import"),
            ("GET", "/api/v1/import/1"),
            ("GET", "/api/v1/import/1/preview"),
            ("POST", "/api/v1/import/1/confirm"),
            ("POST", "/api/v1/import/1/series/1/override"),
            ("DELETE", "/api/v1/import/1"),
            ("GET", "/api/v1/import/1/stream"),
            ("GET", "/api/v1/import/1/logs"),
            ("GET", "/api/v1/import/1/logs/download"),
            ("GET", "/api/v1/import/orphaned"),
            ("GET", "/api/v1/import/orphaned/count"),
            ("POST", "/api/v1/import/orphaned/1/assign"),
            ("POST", "/api/v1/import/orphaned/1/dismiss"),
            ("POST", "/api/v1/import/1/retry-failed"),
            ("POST", "/api/v1/import/1/retry"),
            # File-level endpoints (R-6)
            ("GET", "/api/v1/import/1/series/1/files"),
            ("GET", "/api/v1/import/1/conflicts"),
            ("GET", "/api/v1/import/1/selection-state"),
            ("PUT", "/api/v1/import/1/files/1/match"),
            ("POST", "/api/v1/import/1/files/1/repair-metadata"),
            ("PUT", "/api/v1/import/1/files/1/selection"),
            ("POST", "/api/v1/import/1/files/selection-bulk"),
            ("PUT", "/api/v1/import/1/conflicts/1/resolve"),
            ("POST", "/api/v1/import/1/conflicts/resolve-bulk"),
            ("POST", "/api/v1/import/1/conflicts/reset"),
        ]
        for method, url in endpoints:
            resp = await unauth_client.request(method, url)
            assert resp.status_code == 401, f"{method} {url} should require auth"


# ── Direct handler tests (coverage supplement) ──────────────────────────


class TestHandlersDirect:
    """Call route functions directly for coverage of handler bodies."""

    @pytest.mark.asyncio
    async def test_create_job_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
        tmp_path: object,
    ) -> None:
        """create_import_job called directly."""
        from pullbox.api.v1.import_jobs import create_import_job

        async with _db_factory() as session:
            mock_user = MagicMock()
            body = ImportJobCreate(
                source_path=str(tmp_path),
                source_type=ImportSourceType.FILESYSTEM,
            )

            with patch("pullbox.api.v1.import_jobs.trigger_import_scan"):
                result = await create_import_job(_user=mock_user, session=session, body=body)
                await session.commit()

            assert result.status == ImportJobStatus.PENDING

    @pytest.mark.asyncio
    async def test_get_job_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """get_import_job called directly."""
        from pullbox.api.v1.import_jobs import get_import_job

        job_id = await _seed_import_job(_db_factory)

        async with _db_factory() as session:
            result = await get_import_job(job_id=job_id, _user=MagicMock(), session=session)
            assert result.id == job_id

    @pytest.mark.asyncio
    async def test_get_job_not_found_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """get_import_job raises NotFoundError for missing ID."""
        from pullbox.api.v1.import_jobs import get_import_job

        async with _db_factory() as session:
            with pytest.raises(NotFoundError):
                await get_import_job(job_id=99999, _user=MagicMock(), session=session)

    @pytest.mark.asyncio
    async def test_get_preview_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """get_import_preview called directly."""
        from pullbox.api.v1.import_jobs import get_import_preview

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=4)

        async with _db_factory() as session:
            result = await get_import_preview(
                job_id=job_id,
                _user=MagicMock(),
                session=session,
                status=None,
                page=1,
                page_size=50,
            )
            assert result.total == 4

    @pytest.mark.asyncio
    async def test_get_preview_with_status_filter_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """get_import_preview with status filter."""
        from pullbox.api.v1.import_jobs import get_import_preview

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=4)

        async with _db_factory() as session:
            result = await get_import_preview(
                job_id=job_id,
                _user=MagicMock(),
                session=session,
                status="matched",
                page=1,
                page_size=50,
            )
            assert result.total == 2

    @pytest.mark.asyncio
    async def test_confirm_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """confirm_import called directly."""
        from pullbox.api.v1.import_jobs import confirm_import
        from pullbox.schemas.import_job import ConfirmImportRequest

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=4)

        async with _db_factory() as session:
            from sqlalchemy import select

            res = await session.execute(
                select(ImportedSeries.id).where(
                    ImportedSeries.import_job_id == job_id,
                    ImportedSeries.status == ImportSeriesStatus.MATCHED,
                )
            )
            matched_ids = list(res.scalars().all())

            body = ConfirmImportRequest(series_ids=matched_ids)

            with patch("pullbox.api.v1.import_jobs.trigger_import_execute"):
                result = await confirm_import(
                    job_id=job_id,
                    _user=MagicMock(),
                    session=session,
                    body=body,
                )
                await session.commit()

            assert result.status == ImportJobStatus.IMPORTING

    @pytest.mark.asyncio
    async def test_update_file_selection_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """update_file_selection persists duplicate-series include/exclude state."""
        from pullbox.api.v1.import_jobs import update_file_selection
        from pullbox.schemas.import_job import FileSelectionUpdateRequest

        job_id, _series_id, file_id = await _seed_duplicate_review_selection_job(_db_factory)

        async with _db_factory() as session:
            result = await update_file_selection(
                job_id=job_id,
                file_id=file_id,
                _user=MagicMock(),
                session=session,
                body=FileSelectionUpdateRequest(include_in_import=False),
            )
            await session.commit()

        assert result.include_in_import is False

    @pytest.mark.asyncio
    async def test_unmatch_duplicate_series_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """unmatch_duplicate_series moves a wrong in-library match back to series review."""
        from pullbox.api.v1.import_jobs import unmatch_duplicate_series

        job_id, series_id, _file_id = await _seed_duplicate_review_selection_job(_db_factory)

        async with _db_factory() as session:
            result = await unmatch_duplicate_series(
                job_id=job_id,
                imported_series_id=series_id,
                _user=MagicMock(),
                session=session,
            )
            await session.commit()

        assert result.status == ImportSeriesStatus.NO_MATCH
        assert result.series_id is None
        assert result.cv_id is None

    @pytest.mark.asyncio
    async def test_unmatch_series_match_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """unmatch_series_match clears a normal ComicVine auto-match."""
        from pullbox.api.v1.import_jobs import unmatch_series_match

        async with _db_factory() as session:
            job = ImportJob(
                source_path="/tmp/matched-review",
                source_type=ImportSourceType.FILESYSTEM,
                status=ImportJobStatus.REVIEW,
            )
            session.add(job)
            await session.flush()
            imported_series = ImportedSeries(
                import_job_id=job.id,
                raw_series_name="Giant-Sized Savage Tales",
                raw_year=2026,
                status=ImportSeriesStatus.MATCHED,
                cv_id=143970,
                cv_title="Savage Tales One-Shot",
                cv_year=2022,
                cv_match_score=0.84,
                cv_match_method="alternate_release_candidate",
                files_total=1,
                files_matched=1,
                selected_for_import=True,
            )
            session.add(imported_series)
            await session.flush()
            imp_file = ImportedFile(
                import_job_id=job.id,
                import_series_id=imported_series.id,
                file_path="/tmp/Giant-Sized Savage Tales.cbz",
                file_name="Giant-Sized Savage Tales.cbz",
                file_size=1024,
                file_format="cbz",
                status=ImportedFileStatus.MATCHED,
                matched_issue_cv_id=935006,
                match_confidence="medium",
                match_method="issue_number",
                include_in_import=True,
            )
            session.add(imp_file)
            await session.commit()
            job_id = job.id
            series_id = imported_series.id

        async with _db_factory() as session:
            result = await unmatch_series_match(
                job_id=job_id,
                imported_series_id=series_id,
                _user=MagicMock(),
                session=session,
            )
            await session.commit()

        assert result.status == ImportSeriesStatus.NO_MATCH
        assert result.cv_id is None
        assert result.cv_title is None
        assert result.selected_for_import is False

    @pytest.mark.asyncio
    async def test_bulk_update_file_selection_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """bulk_update_file_selection updates duplicate-series matches in scope."""
        from pullbox.api.v1.import_jobs import bulk_update_file_selection
        from pullbox.schemas.import_job import FileSelectionBulkUpdateRequest

        job_id, series_id, _file_id = await _seed_duplicate_review_selection_job(_db_factory)

        async with _db_factory() as session:
            result = await bulk_update_file_selection(
                job_id=job_id,
                _user=MagicMock(),
                session=session,
                body=FileSelectionBulkUpdateRequest(
                    include_in_import=False,
                    imported_series_id=series_id,
                ),
            )
            await session.commit()

        assert result.updated == 1
        assert result.selection_state.selected_item_count == 0

    @pytest.mark.asyncio
    async def test_confirm_wrong_state_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """confirm_import raises HTTPException on wrong state."""
        from fastapi import HTTPException

        from pullbox.api.v1.import_jobs import confirm_import
        from pullbox.schemas.import_job import ConfirmImportRequest

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.SCANNING)

        async with _db_factory() as session:
            body = ConfirmImportRequest(series_ids=[1])
            with pytest.raises(HTTPException) as exc_info:
                await confirm_import(
                    job_id=job_id,
                    _user=MagicMock(),
                    session=session,
                    body=body,
                )
            assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_cancel_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """cancel_import_job called directly deletes discardable jobs."""
        from pullbox.api.v1.import_jobs import cancel_import_job
        from pullbox.schemas.import_job import ImportProgressEvent
        from pullbox.tasks.import_task import (
            get_latest_progress_event,
            get_progress_queue,
            remove_progress_queue,
            set_latest_progress_event,
        )

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW)
        queue = get_progress_queue(job_id)
        set_latest_progress_event(
            ImportProgressEvent(
                job_id=job_id,
                status=ImportJobStatus.REVIEW,
                phase="review",
                progress=100,
                message="Ready for review",
            )
        )

        async with _db_factory() as session:
            await cancel_import_job(job_id=job_id, _user=MagicMock(), session=session)
            await session.commit()

        async with _db_factory() as session:
            job = await session.get(ImportJob, job_id)
            assert job is None
        assert get_latest_progress_event(job_id) is None
        replacement_queue = get_progress_queue(job_id)
        assert replacement_queue is not queue
        assert replacement_queue.empty()
        remove_progress_queue(job_id)

    @pytest.mark.asyncio
    async def test_request_cancel_scanning_direct_sets_cancel_request(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Active-scan cancel stores a cooperative cancel request durably."""
        from pullbox.api.v1.import_jobs import request_cancel_import_job
        from pullbox.models.import_job import ImportControlRequest

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.SCANNING)

        async with _db_factory() as session:
            result = await request_cancel_import_job(
                job_id=job_id,
                _user=MagicMock(),
                session=session,
            )
            await session.commit()
            assert result is not None
            assert result.status == ImportJobStatus.SCANNING
            assert result.control_request == ImportControlRequest.CANCEL

        async with _db_factory() as session:
            job = await session.get(ImportJob, job_id)
            assert job is not None
            assert job.status == ImportJobStatus.SCANNING
            assert job.control_request == ImportControlRequest.CANCEL
            assert job.error_message == "Import cancelled by user."

    @pytest.mark.asyncio
    async def test_request_cancel_paused_preimport_clears_runtime_state(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Pre-import paused cancel deletes the job and purges runtime queue state."""
        from pullbox.api.v1.import_jobs import request_cancel_import_job
        from pullbox.schemas.import_job import ImportProgressEvent
        from pullbox.tasks.import_task import (
            get_latest_progress_event,
            get_progress_queue,
            remove_progress_queue,
            set_latest_progress_event,
        )

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.PAUSED)
        queue = get_progress_queue(job_id)
        set_latest_progress_event(
            ImportProgressEvent(
                job_id=job_id,
                status=ImportJobStatus.PAUSED,
                phase="scanning",
                progress=42,
                message="Paused",
            )
        )

        async with _db_factory() as session:
            result = await request_cancel_import_job(
                job_id=job_id,
                _user=MagicMock(),
                session=session,
            )
            await session.commit()
            assert result is None

        async with _db_factory() as session:
            job = await session.get(ImportJob, job_id)
            assert job is None

        assert get_latest_progress_event(job_id) is None
        replacement_queue = get_progress_queue(job_id)
        assert replacement_queue is not queue
        assert replacement_queue.empty()
        remove_progress_queue(job_id)

    @pytest.mark.asyncio
    async def test_cancel_wrong_state_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """cancel_import_job rejects active states that need explicit controls."""
        from fastapi import HTTPException

        from pullbox.api.v1.import_jobs import cancel_import_job

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.IMPORTING)

        async with _db_factory() as session:
            with pytest.raises(HTTPException) as exc_info:
                await cancel_import_job(job_id=job_id, _user=MagicMock(), session=session)
            assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_request_cancel_endpoint_sets_cancel_request(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST /import/{id}/cancel stores cooperative cancellation for active jobs."""
        from pullbox.models.import_job import ImportControlRequest

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.SCANNING)

        resp = await client.post(f"/api/v1/import/{job_id}/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == ImportJobStatus.SCANNING.value
        assert data["control_request"] == ImportControlRequest.CANCEL.value

        async with _db_factory() as session:
            job = await session.get(ImportJob, job_id)
            assert job is not None
            assert job.status == ImportJobStatus.SCANNING
            assert job.control_request == ImportControlRequest.CANCEL

    @pytest.mark.asyncio
    async def test_pause_endpoint_marks_import_paused_immediately(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST /import/{id}/pause should return a real paused import state."""
        from pullbox.models.import_job import ImportControlRequest

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.IMPORTING)
        async with _db_factory() as session:
            job = await session.get(ImportJob, job_id)
            assert job is not None
            job.import_started_at = datetime.now(UTC)
            job.progress_snapshot = {
                "status": ImportJobStatus.IMPORTING.value,
                "mode": "import",
                "phase": "importing",
                "progress": 64,
                "message": "Processing file 2/5",
                "current_file_name": "Fearscape Vol 02.pdf",
                "current_file_stage": "rendering",
                "current_file_progress_pct": 42,
            }
            await session.commit()

        resp = await client.post(f"/api/v1/import/{job_id}/pause")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == ImportJobStatus.PAUSED.value
        assert data["control_request"] == ImportControlRequest.NONE.value
        assert data["progress_snapshot"]["status"] == ImportJobStatus.PAUSED.value
        assert data["progress_snapshot"]["phase"] == "importing"
        assert data["progress_snapshot"]["progress"] == 64
        assert data["progress_snapshot"]["message"] == "Import is paused."
        assert data["progress_snapshot"]["control_state"]["can_resume"] is True
        assert data["progress_snapshot"]["control_state"]["can_cancel"] is True

        async with _db_factory() as session:
            job = await session.get(ImportJob, job_id)
            assert job is not None
            assert job.status == ImportJobStatus.PAUSED
            assert job.control_request == ImportControlRequest.NONE

    @pytest.mark.asyncio
    async def test_request_cancel_paused_import_enters_rollback(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Paused imports cancel by entering rollback instead of reviving review."""
        from pullbox.api.v1.import_jobs import request_cancel_import_job
        from pullbox.models.import_job import ImportControlRequest

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.PAUSED)
        async with _db_factory() as session:
            job = await session.get(ImportJob, job_id)
            assert job is not None
            job.import_started_at = datetime.now(UTC)
            await session.commit()

        async with _db_factory() as session:
            result = await request_cancel_import_job(
                job_id=job_id,
                _user=MagicMock(),
                session=session,
            )
            await session.commit()
            assert result is not None
            assert result.status == ImportJobStatus.ROLLING_BACK
            assert result.control_request == ImportControlRequest.CANCEL

    @pytest.mark.asyncio
    async def test_override_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """override_series_cv_id called directly."""
        from pullbox.api.v1.import_jobs import override_series_cv_id

        job_id = await _seed_import_job(_db_factory, status=ImportJobStatus.REVIEW, with_series=2)

        async with _db_factory() as session:
            from sqlalchemy import select

            res = await session.execute(
                select(ImportedSeries.id).where(
                    ImportedSeries.import_job_id == job_id,
                )
            )
            series_id = res.scalars().first()

            mock_item = MagicMock()
            mock_item.id = series_id
            mock_item.status = ImportSeriesStatus.MATCHED
            mock_item.raw_series_name = "Test"
            mock_item.raw_year = 2020
            mock_item.raw_publisher = None
            mock_item.file_count = 5
            mock_item.has_files = True
            mock_item.sample_paths = []
            mock_item.source_folder = None
            mock_item.cv_id = 99999
            mock_item.cv_title = "Overridden"
            mock_item.cv_year = 2020
            mock_item.cv_publisher = "DC"
            mock_item.cv_issue_count = 10
            mock_item.cv_url = "https://comicvine.gamespot.com/test"
            mock_item.cv_match_score = 1.0
            mock_item.cv_match_method = "user_override"
            mock_item.user_selected_cv_id = 99999
            mock_item.files_total = 5
            mock_item.files_matched = 5
            mock_item.files_duplicate = 0
            mock_item.files_already_owned = 0
            mock_item.files_conflict = 0
            mock_item.files_no_match = 0
            mock_item.files_imported = 0
            mock_item.files_failed = 0
            mock_item.series_id = None
            mock_item.error_message = None
            mock_item.diagnostics = {}

            with (
                patch(
                    "pullbox.api.v1.import_jobs._build_interactive_import_service",
                    new_callable=AsyncMock,
                ) as mock_build,
                patch("pullbox.api.v1.import_jobs.trigger_import_series_rematch") as mock_trigger,
            ):
                mock_service = AsyncMock()
                mock_service.override_cv_id.return_value = mock_item
                mock_build.return_value = mock_service

                result = await override_series_cv_id(
                    job_id=job_id,
                    imported_series_id=series_id,
                    _user=MagicMock(),
                    session=session,
                    body={"cv_id": 99999},
                )

            assert result.cv_id == 99999
            mock_service.override_cv_id.assert_awaited_once_with(
                session,
                series_id,
                99999,
                rematch_files=False,
            )
            mock_trigger.assert_called_once_with(job_id, series_id)

    @pytest.mark.asyncio
    async def test_override_invalid_cv_id_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """override_series_cv_id raises ValidationError for bad cv_id."""
        from pullbox.api.v1.import_jobs import override_series_cv_id

        async with _db_factory() as session:
            with pytest.raises(ValidationError):
                await override_series_cv_id(
                    job_id=1,
                    imported_series_id=1,
                    _user=MagicMock(),
                    session=session,
                    body={"cv_id": -1},
                )

    @pytest.mark.asyncio
    async def test_stream_direct(self) -> None:
        """stream_import_progress returns StreamingResponse."""
        from pullbox.api.v1.import_jobs import stream_import_progress

        with patch(
            "pullbox.api.v1.import_jobs._load_initial_import_progress_sse",
            new=AsyncMock(return_value=(None, False)),
        ):
            resp = await stream_import_progress(
                job_id=12345,
                request=MagicMock(),
                _user=MagicMock(),
            )
        assert resp.media_type == "text/event-stream"

    @pytest.mark.asyncio
    async def test_log_stream_direct(self) -> None:
        """stream_import_logs returns StreamingResponse."""
        from pullbox.api.v1.import_jobs import stream_import_logs

        with patch(
            "pullbox.api.v1.import_jobs._ensure_import_job_exists_for_stream",
            new=AsyncMock(),
        ):
            resp = await stream_import_logs(
                job_id=12345,
                request=MagicMock(),
                _user=MagicMock(),
            )
        assert resp.media_type == "text/event-stream"

    @pytest.mark.asyncio
    async def test_logs_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """get_import_job_logs called directly."""
        from pullbox.api.v1.import_jobs import get_import_job_logs

        job_id = await _seed_import_job(_db_factory, with_logs=3)

        async with _db_factory() as session:
            result = await get_import_job_logs(
                job_id=job_id,
                _user=MagicMock(),
                session=session,
                page=1,
                page_size=100,
                level=None,
            )
            assert result.total == 3
            assert len(result.items) == 3

    @pytest.mark.asyncio
    async def test_logs_with_level_filter_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """get_import_job_logs with level filter."""
        from pullbox.api.v1.import_jobs import get_import_job_logs

        job_id = await _seed_import_job(_db_factory, with_logs=6)

        async with _db_factory() as session:
            result = await get_import_job_logs(
                job_id=job_id,
                _user=MagicMock(),
                session=session,
                page=1,
                page_size=100,
                level="WARNING",
                order="asc",
            )
            assert result.total == 2

    @pytest.mark.asyncio
    async def test_logs_descending_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """get_import_job_logs can return latest-first entries."""
        from pullbox.api.v1.import_jobs import get_import_job_logs

        job_id = await _seed_import_job(_db_factory, with_logs=4)

        async with _db_factory() as session:
            result = await get_import_job_logs(
                job_id=job_id,
                _user=MagicMock(),
                session=session,
                page=1,
                page_size=100,
                level=None,
                order="desc",
            )

        assert len(result.items) == 4
        assert result.items[0].id > result.items[-1].id

    @pytest.mark.asyncio
    async def test_logs_not_found_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """get_import_job_logs raises NotFoundError."""
        from pullbox.api.v1.import_jobs import get_import_job_logs

        async with _db_factory() as session:
            with pytest.raises(NotFoundError):
                await get_import_job_logs(
                    job_id=99999,
                    _user=MagicMock(),
                    session=session,
                    page=1,
                    page_size=100,
                    level=None,
                    order="asc",
                )

    @pytest.mark.asyncio
    async def test_download_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """download_import_job_logs called directly."""
        from pullbox.api.v1.import_jobs import download_import_job_logs

        job_id = await _seed_import_job(_db_factory, with_logs=3)

        async with _db_factory() as session:
            result = await download_import_job_logs(
                job_id=job_id, _user=MagicMock(), session=session
            )
            assert "text/plain" in result.media_type
            assert len(result.body.decode().strip().split("\n")) == 3

    @pytest.mark.asyncio
    async def test_download_empty_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """download_import_job_logs with no entries."""
        from pullbox.api.v1.import_jobs import download_import_job_logs

        job_id = await _seed_import_job(_db_factory, with_logs=0)

        async with _db_factory() as session:
            result = await download_import_job_logs(
                job_id=job_id, _user=MagicMock(), session=session
            )
            assert result.body.decode().startswith("#")

    @pytest.mark.asyncio
    async def test_download_not_found_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """download_import_job_logs raises NotFoundError."""
        from pullbox.api.v1.import_jobs import download_import_job_logs

        async with _db_factory() as session:
            with pytest.raises(NotFoundError):
                await download_import_job_logs(job_id=99999, _user=MagicMock(), session=session)


class TestBuildImportService:
    """Test _build_import_service helper."""

    @pytest.mark.asyncio
    async def test_builds_service(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """_build_import_service creates an ImportService with dependencies."""
        from pullbox.api.v1.import_jobs import _build_import_service
        from pullbox.services.import_service import ImportService

        mock_settings = MagicMock()
        mock_settings.comicvine_rate_limit = 1.0
        mock_settings.covers_dir = "/tmp"
        mock_settings.metadata_refresh_days = 30

        async with _db_factory() as session:
            with (
                patch(
                    "pullbox.composition.services.get_comicvine_api_key",
                    new_callable=AsyncMock,
                    return_value="test-key",
                ),
                patch(
                    "pullbox.composition.services.get_settings",
                    return_value=mock_settings,
                ),
            ):
                svc = await _build_import_service(session)
                assert isinstance(svc, ImportService)


class TestMakeImportService:
    """Test _make_import_service helper."""

    def test_creates_service(self) -> None:
        from pullbox.api.v1.import_jobs import _make_import_service
        from pullbox.services.import_service import ImportService

        svc = _make_import_service()
        assert isinstance(svc, ImportService)


class TestSSEHeartbeat:
    """Test SSE heartbeat on timeout."""

    @pytest.mark.asyncio
    async def test_heartbeat_on_timeout(self) -> None:
        """Event generator yields heartbeat when queue.get() times out."""
        import asyncio

        from pullbox.schemas.import_job import ImportProgressEvent
        from pullbox.tasks.import_task import get_progress_queue, remove_progress_queue

        job_id = 77777
        queue = get_progress_queue(job_id)
        collected: list[str] = []

        # Reproduce the SSE generator logic inline with a short timeout
        # to trigger the heartbeat path
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.01)
            collected.append(f"data: {event.model_dump_json()}\n\n")
        except TimeoutError:
            collected.append('data: {"heartbeat": true}\n\n')

        # Then consume a real event
        await queue.put(
            ImportProgressEvent(
                job_id=job_id,
                status=ImportJobStatus.COMPLETED,
                phase="done",
                progress=100,
                message="Done",
            )
        )
        event = await queue.get()
        collected.append(f"data: {event.model_dump_json()}\n\n")

        remove_progress_queue(job_id)

        assert any('"heartbeat"' in c for c in collected)
        assert any('"phase"' in c for c in collected)


class TestLogLevelHelper:
    """Test _log_levels_at_or_above helper."""

    def test_warning_returns_warning_and_error(self) -> None:
        from pullbox.api.v1.import_jobs import _log_levels_at_or_above

        levels = _log_levels_at_or_above("WARNING")
        assert "WARNING" in levels
        assert "ERROR" in levels
        assert "INFO" not in levels
        assert "DEBUG" not in levels

    def test_debug_returns_all(self) -> None:
        from pullbox.api.v1.import_jobs import _log_levels_at_or_above

        levels = _log_levels_at_or_above("DEBUG")
        assert len(levels) == 4

    def test_unknown_level_returns_all(self) -> None:
        from pullbox.api.v1.import_jobs import _log_levels_at_or_above

        levels = _log_levels_at_or_above("BOGUS")
        assert len(levels) == 4
