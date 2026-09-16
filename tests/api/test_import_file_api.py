"""Tests for file-level Import API endpoints — Phase R-6, Task R-6.1.

Covers:
- GET /api/v1/import/{job_id}/series/{series_id}/files — paginated file list
- GET /api/v1/import/{job_id}/conflicts — conflict groups
- PUT /api/v1/import/{job_id}/files/{file_id}/match — override file match
- PUT /api/v1/import/{job_id}/conflicts/{group_id}/resolve — resolve conflict
- POST /api/v1/import/{job_id}/conflicts/resolve-bulk — resolve many conflicts
- POST /api/v1/import/{job_id}/conflicts/reset — reset resolved conflicts
- Auth required on all new endpoints
- Edge cases: 404s, 400s, empty results, pagination, status filters

Run:
    pytest tests/api/test_import_file_api.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.import_job import (
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobStatus,
    ImportSeriesStatus,
    ImportSourceType,
)
from pullbox.models.issue import Issue
from pullbox.models.series import Series, SeriesStatus
from pullbox.models.user import APIKey, User
from pullbox.services.auth_service import AuthService

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-import-files")

# Global counter to avoid UNIQUE constraint violations on comicvine_id
_cv_id_counter = 50000


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
    raw_key = "pb_k1_" + "c" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="fileapiuser",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="file-api-test"))
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


# ── Seed Helpers ─────────────────────────────────────────────────────────


async def _seed_job_with_files(
    factory: async_sessionmaker[AsyncSession],
    *,
    job_status: ImportJobStatus = ImportJobStatus.REVIEW,
    series_count: int = 1,
    files_per_series: int = 3,
    conflict_files: int = 0,
    no_match_files: int = 0,
) -> tuple[int, list[int], list[int], list[int]]:
    """Create job, series, and files.

    Returns (job_id, series_ids, file_ids, issue_ids).
    Creates real Series + Issue records to satisfy FK constraints on matched_issue_id.
    """
    global _cv_id_counter
    async with factory() as session:
        job = ImportJob(
            source_path="/tmp/comics",
            source_type=ImportSourceType.FILESYSTEM,
            status=job_status,
        )
        session.add(job)
        await session.flush()

        series_ids: list[int] = []
        file_ids: list[int] = []
        all_issue_ids: list[int] = []
        conflict_group_counter = 1

        for s in range(series_count):
            _cv_id_counter += 1
            cv_id = _cv_id_counter

            # Create a real Series + Issues for FK satisfaction
            real_series = Series(
                title=f"Test Series {cv_id}",
                sort_title=f"test series {cv_id}",
                year_start=2020 + s,
                comicvine_id=cv_id,
                status=SeriesStatus.CONTINUING,
                issue_count=0,
            )
            session.add(real_series)
            await session.flush()

            # Create enough Issue records for matched files + conflict files
            issue_ids: list[int] = []
            needed_issues = files_per_series + (conflict_files // 2) + conflict_files % 2
            for i in range(needed_issues):
                issue = Issue(
                    series_id=real_series.id,
                    issue_number=float(i + 1),
                    title=f"Issue #{i + 1}",
                )
                session.add(issue)
                await session.flush()
                issue_ids.append(issue.id)

            # Also create a spare issue for override tests
            spare_issue = Issue(
                series_id=real_series.id,
                issue_number=float(needed_issues + 1),
                title=f"Spare Issue #{needed_issues + 1}",
            )
            session.add(spare_issue)
            await session.flush()
            issue_ids.append(spare_issue.id)

            all_issue_ids.extend(issue_ids)

            item = ImportedSeries(
                import_job_id=job.id,
                raw_series_name=f"Test Series {cv_id}",
                raw_year=2020 + s,
                file_count=files_per_series + conflict_files + no_match_files,
                has_files=True,
                sample_paths=[f"/tmp/comics/series_{s}/issue_1.cbz"],
                status=ImportSeriesStatus.MATCHED,
                cv_id=cv_id,
                cv_title=f"CV Series {cv_id}",
                cv_match_score=0.95,
            )
            session.add(item)
            await session.flush()
            series_ids.append(item.id)

            # Regular matched files
            for f in range(files_per_series):
                imp_file = ImportedFile(
                    import_job_id=job.id,
                    import_series_id=item.id,
                    file_path=f"/tmp/comics/series_{s}/issue_{f + 1}.cbz",
                    file_name=f"issue_{f + 1}.cbz",
                    file_size=1024 * (f + 1),
                    file_format="cbz",
                    parsed_series=f"Test Series {s}",
                    parsed_issue_number=float(f + 1),
                    status=ImportedFileStatus.MATCHED,
                    match_confidence="high",
                    match_method="issue_number",
                    matched_issue_id=issue_ids[f] if f < len(issue_ids) else None,
                )
                session.add(imp_file)
                await session.flush()
                file_ids.append(imp_file.id)

            # Conflict files (pairs sharing matched_issue_id)
            conflict_issue_idx = files_per_series
            for c in range(0, conflict_files, 2):
                shared_issue_id = (
                    issue_ids[conflict_issue_idx] if conflict_issue_idx < len(issue_ids) else None
                )
                conflict_issue_idx += 1
                for dup_idx in range(2):
                    is_pref = dup_idx == 0
                    cf = ImportedFile(
                        import_job_id=job.id,
                        import_series_id=item.id,
                        file_path=f"/tmp/comics/series_{s}/conflict_{c}_{dup_idx}.cbz",
                        file_name=f"conflict_{c}_{dup_idx}.cbz",
                        file_size=2048 if is_pref else 1024,
                        file_format="cbz",
                        parsed_series=f"Test Series {s}",
                        parsed_issue_number=float(100 + c),
                        status=ImportedFileStatus.CONFLICT,
                        match_confidence="high" if is_pref else "medium",
                        match_method="issue_number",
                        matched_issue_id=shared_issue_id,
                        conflict_group_id=conflict_group_counter,
                        is_preferred=is_pref,
                    )
                    session.add(cf)
                    await session.flush()
                    file_ids.append(cf.id)
                conflict_group_counter += 1

            # No-match files
            for n in range(no_match_files):
                nmf = ImportedFile(
                    import_job_id=job.id,
                    import_series_id=item.id,
                    file_path=f"/tmp/comics/series_{s}/nomatch_{n}.cbz",
                    file_name=f"nomatch_{n}.cbz",
                    file_size=512,
                    file_format="cbz",
                    parsed_series=f"Test Series {s}",
                    status=ImportedFileStatus.NO_MATCH,
                )
                session.add(nmf)
                await session.flush()
                file_ids.append(nmf.id)

        await session.commit()
        return job.id, series_ids, file_ids, all_issue_ids


# ── Tests: GET /import/{job_id}/series/{series_id}/files ──────────────


class TestListImportFiles:
    """Test GET /api/v1/import/{job_id}/series/{series_id}/files."""

    @pytest.mark.asyncio
    async def test_returns_paginated_files(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Returns ImportedFilesResponse with all files for the series."""
        job_id, series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=5
        )
        resp = await client.get(f"/api/v1/import/{job_id}/series/{series_ids[0]}/files")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 5
        assert data["page"] == 1

    @pytest.mark.asyncio
    async def test_pagination(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Pagination returns correct page of files."""
        job_id, series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=5
        )
        resp = await client.get(
            f"/api/v1/import/{job_id}/series/{series_ids[0]}/files?page=2&page_size=2"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert data["page"] == 2
        assert data["page_size"] == 2
        assert len(data["items"]) == 2

    @pytest.mark.asyncio
    async def test_status_filter(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Filter by file status returns only matching files."""
        job_id, series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=3, no_match_files=2
        )
        resp = await client.get(
            f"/api/v1/import/{job_id}/series/{series_ids[0]}/files?status=no_match"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        for item in data["items"]:
            assert item["status"] == "no_match"

    @pytest.mark.asyncio
    async def test_job_not_found(
        self,
        client: AsyncClient,
    ) -> None:
        """Returns 404 when job doesn't exist."""
        resp = await client.get("/api/v1/import/99999/series/1/files")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_series_not_found(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Returns 404 when series doesn't exist within the job."""
        job_id, _series_ids, _file_ids, _iids = await _seed_job_with_files(_db_factory)
        resp = await client.get(f"/api/v1/import/{job_id}/series/99999/files")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_series_from_different_job(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Returns 404 when series belongs to a different job."""
        _job1, series1, _f1, _iids = await _seed_job_with_files(_db_factory)
        job2, _series2, _f2, _iids = await _seed_job_with_files(_db_factory)
        resp = await client.get(f"/api/v1/import/{job2}/series/{series1[0]}/files")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_empty_series_no_files(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Series with zero files returns empty list."""
        job_id, series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=0
        )
        resp = await client.get(f"/api/v1/import/{job_id}/series/{series_ids[0]}/files")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    @pytest.mark.asyncio
    async def test_files_include_conflict_and_nomatch(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """All file statuses returned when no filter applied."""
        job_id, series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=2, conflict_files=2, no_match_files=1
        )
        resp = await client.get(f"/api/v1/import/{job_id}/series/{series_ids[0]}/files")
        assert resp.status_code == 200
        data = resp.json()
        # 2 matched + 2 conflict + 1 no_match = 5
        assert data["total"] == 5

    @pytest.mark.asyncio
    async def test_filter_conflict_status(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Filter by conflict status returns only conflict files."""
        job_id, series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=2, conflict_files=4
        )
        resp = await client.get(
            f"/api/v1/import/{job_id}/series/{series_ids[0]}/files?status=conflict"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 4
        for item in data["items"]:
            assert item["status"] == "conflict"

    @pytest.mark.asyncio
    async def test_invalid_status_filter_returns_all(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Invalid status filter is ignored and returns all files."""
        job_id, series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=3
        )
        resp = await client.get(
            f"/api/v1/import/{job_id}/series/{series_ids[0]}/files?status=bogus"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3

    @pytest.mark.asyncio
    async def test_file_fields_populated(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """File response contains expected fields."""
        job_id, series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=1
        )
        resp = await client.get(f"/api/v1/import/{job_id}/series/{series_ids[0]}/files")
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert "id" in item
        assert "file_path" in item
        assert "file_name" in item
        assert "file_size" in item
        assert "status" in item
        assert "match_confidence" in item
        assert "matched_issue_id" in item
        assert "conflict_group_id" in item
        assert "is_preferred" in item


# ── Tests: GET /import/{job_id}/conflicts ─────────────────────────────


class TestListConflicts:
    """Test GET /api/v1/import/{job_id}/conflicts."""

    @pytest.mark.asyncio
    async def test_returns_conflict_groups(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Returns ConflictGroupsResponse with grouped conflict files."""
        job_id, _series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=2, conflict_files=4
        )
        resp = await client.get(f"/api/v1/import/{job_id}/conflicts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == job_id
        # 4 conflict files = 2 groups of 2
        assert data["total"] == 2
        assert len(data["groups"]) == 2
        for group in data["groups"]:
            assert len(group["files"]) == 2
            assert "conflict_group_id" in group
            assert "matched_issue_id" in group

    @pytest.mark.asyncio
    async def test_no_conflicts_returns_empty(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Job with no conflicts returns empty groups list."""
        job_id, _series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=3, conflict_files=0
        )
        resp = await client.get(f"/api/v1/import/{job_id}/conflicts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["groups"] == []

    @pytest.mark.asyncio
    async def test_conflicts_endpoint_returns_one_bounded_server_page(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        job_id, _series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory,
            files_per_series=0,
            conflict_files=6,
        )

        response = await client.get(
            f"/api/v1/import/{job_id}/conflicts",
            params={"page": 2, "page_size": 1},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 3
        assert payload["page"] == 2
        assert payload["page_size"] == 1
        assert len(payload["groups"]) == 1

    @pytest.mark.asyncio
    async def test_conflicts_endpoint_represents_series_candidate_conflicts(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        job_id, series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory,
            files_per_series=1,
            conflict_files=0,
        )
        async with _db_factory() as session:
            imported_series = await session.get(ImportedSeries, series_ids[0])
            assert imported_series is not None
            imported_series.status = ImportSeriesStatus.NO_MATCH
            imported_series.diagnostics = {
                "kind": "series_conflict",
                "reason": "ambiguous_match",
            }
            await session.commit()

        response = await client.get(f"/api/v1/import/{job_id}/conflicts")

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["groups"][0]["kind"] == "series_conflict"
        assert payload["groups"][0]["conflict_group_id"] == f"series-{series_ids[0]}"
        assert payload["groups"][0]["series_id"] == series_ids[0]
        assert payload["groups"][0]["matched_issue_id"] is None

    @pytest.mark.asyncio
    async def test_job_not_found(
        self,
        client: AsyncClient,
    ) -> None:
        """Returns 404 when job doesn't exist."""
        resp = await client.get("/api/v1/import/99999/conflicts")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_conflicts_across_multiple_series(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Conflicts from multiple series are all returned."""
        job_id, _series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, series_count=2, files_per_series=1, conflict_files=2
        )
        resp = await client.get(f"/api/v1/import/{job_id}/conflicts")
        assert resp.status_code == 200
        data = resp.json()
        # Each series has 1 conflict group (2 conflict files)
        assert data["total"] == 2

    @pytest.mark.asyncio
    async def test_conflict_group_has_preferred_file(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Each conflict group has exactly one preferred file."""
        job_id, _series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=0, conflict_files=2
        )
        resp = await client.get(f"/api/v1/import/{job_id}/conflicts")
        assert resp.status_code == 200
        for group in resp.json()["groups"]:
            preferred = [f for f in group["files"] if f["is_preferred"]]
            assert len(preferred) == 1

    @pytest.mark.asyncio
    async def test_conflict_files_share_matched_issue_id(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """All files in a group share the same matched_issue_id."""
        job_id, _series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=0, conflict_files=2
        )
        resp = await client.get(f"/api/v1/import/{job_id}/conflicts")
        assert resp.status_code == 200
        for group in resp.json()["groups"]:
            issue_ids = {f["matched_issue_id"] for f in group["files"]}
            assert len(issue_ids) == 1
            assert group["matched_issue_id"] in issue_ids


# ── Tests: PUT /import/{job_id}/files/{file_id}/match ─────────────────


class TestOverrideFileMatch:
    """Test PUT /api/v1/import/{job_id}/files/{file_id}/match."""

    @pytest.mark.asyncio
    async def test_updates_file_match(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """PUT updates matched_issue_id and returns updated file."""
        job_id, _series_ids, file_ids, iids = await _seed_job_with_files(
            _db_factory, files_per_series=1
        )
        # Use the spare issue (last in iids) as the override target
        spare_issue_id = iids[-1]
        resp = await client.put(
            f"/api/v1/import/{job_id}/files/{file_ids[0]}/match",
            json={"issue_id": spare_issue_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched_issue_id"] == spare_issue_id
        assert data["match_method"] == "manual_override"
        assert data["status"] == "matched"

    @pytest.mark.asyncio
    async def test_job_not_found(
        self,
        client: AsyncClient,
    ) -> None:
        """Returns 404 when job doesn't exist."""
        resp = await client.put(
            "/api/v1/import/99999/files/1/match",
            json={"issue_id": 7777},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_file_not_found(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Returns 404 when file doesn't exist within the job."""
        job_id, _series_ids, _file_ids, _iids = await _seed_job_with_files(_db_factory)
        resp = await client.put(
            f"/api/v1/import/{job_id}/files/99999/match",
            json={"issue_id": 7777},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_file_from_different_job(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Returns 404 when file belongs to a different job."""
        _job1, _s1, file_ids1, iids1 = await _seed_job_with_files(_db_factory)
        job2, _s2, _f2, _iids2 = await _seed_job_with_files(_db_factory)
        resp = await client.put(
            f"/api/v1/import/{job2}/files/{file_ids1[0]}/match",
            json={"issue_id": iids1[-1]},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_invalid_issue_id(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Returns 422 when issue_id is invalid (zero or negative)."""
        job_id, _series_ids, file_ids, _iids = await _seed_job_with_files(_db_factory)
        resp = await client.put(
            f"/api/v1/import/{job_id}/files/{file_ids[0]}/match",
            json={"issue_id": 0},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_issue_id(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Returns 422 when issue_id is missing from body."""
        job_id, _series_ids, file_ids, _iids = await _seed_job_with_files(_db_factory)
        resp = await client.put(
            f"/api/v1/import/{job_id}/files/{file_ids[0]}/match",
            json={},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_override_nomatch_file(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Overriding a NO_MATCH file sets it to MATCHED with manual_override method."""
        job_id, _series_ids, file_ids, iids = await _seed_job_with_files(
            _db_factory, files_per_series=0, no_match_files=1
        )
        spare_issue_id = iids[-1]
        resp = await client.put(
            f"/api/v1/import/{job_id}/files/{file_ids[0]}/match",
            json={"issue_id": spare_issue_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched_issue_id"] == spare_issue_id
        assert data["status"] == "matched"
        assert data["match_method"] == "manual_override"

    @pytest.mark.asyncio
    async def test_override_conflict_file(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Overriding a CONFLICT file re-assigns it to a new issue."""
        job_id, _series_ids, file_ids, iids = await _seed_job_with_files(
            _db_factory, files_per_series=0, conflict_files=2
        )
        spare_issue_id = iids[-1]
        resp = await client.put(
            f"/api/v1/import/{job_id}/files/{file_ids[0]}/match",
            json={"issue_id": spare_issue_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched_issue_id"] == spare_issue_id
        assert data["status"] == "matched"

    @pytest.mark.asyncio
    async def test_job_not_in_review_state(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Returns 409 when job is not in REVIEW state."""
        job_id, _series_ids, file_ids, _iids = await _seed_job_with_files(
            _db_factory, job_status=ImportJobStatus.IMPORTING
        )
        resp = await client.put(
            f"/api/v1/import/{job_id}/files/{file_ids[0]}/match",
            json={"issue_id": 7777},
        )
        assert resp.status_code == 409


# ── Tests: PUT /import/{job_id}/conflicts/{group_id}/resolve ─────────


class TestResolveConflict:
    """Test PUT /api/v1/import/{job_id}/conflicts/{group_id}/resolve."""

    @pytest.mark.asyncio
    async def test_resolves_conflict(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """PUT resolves conflict: chosen file → CONFIRMED, others → SKIPPED."""
        job_id, _series_ids, file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=0, conflict_files=2
        )
        # Conflict files are file_ids[0] and file_ids[1]
        chosen_id = file_ids[0]
        resp = await client.put(
            f"/api/v1/import/{job_id}/conflicts/1/resolve",
            json={"chosen_file_id": chosen_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Response should show the resolved files
        confirmed = [f for f in data["files"] if f["status"] == "confirmed"]
        skipped = [f for f in data["files"] if f["status"] == "skipped"]
        assert len(confirmed) == 1
        assert confirmed[0]["id"] == chosen_id
        assert len(skipped) == 1

    @pytest.mark.asyncio
    async def test_job_not_found(
        self,
        client: AsyncClient,
    ) -> None:
        """Returns 404 when job doesn't exist."""
        resp = await client.put(
            "/api/v1/import/99999/conflicts/1/resolve",
            json={"chosen_file_id": 1},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_conflict_group_not_found(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Returns 404 when conflict group doesn't exist."""
        job_id, _series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=3, conflict_files=0
        )
        resp = await client.put(
            f"/api/v1/import/{job_id}/conflicts/999/resolve",
            json={"chosen_file_id": 1},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_chosen_file_not_in_group(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Returns 400 when chosen_file_id is not part of the conflict group."""
        job_id, _series_ids, file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=1, conflict_files=2
        )
        # file_ids[0] is the matched file, not in conflict group 1
        resp = await client.put(
            f"/api/v1/import/{job_id}/conflicts/1/resolve",
            json={"chosen_file_id": file_ids[0]},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_chosen_file_id(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Returns 422 when chosen_file_id is invalid."""
        job_id, _series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, conflict_files=2
        )
        resp = await client.put(
            f"/api/v1/import/{job_id}/conflicts/1/resolve",
            json={"chosen_file_id": 0},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_chosen_file_id(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Returns 422 when body is missing chosen_file_id."""
        job_id, _series_ids, _file_ids, _iids = await _seed_job_with_files(
            _db_factory, conflict_files=2
        )
        resp = await client.put(
            f"/api/v1/import/{job_id}/conflicts/1/resolve",
            json={},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_job_not_in_review_state(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Returns 409 when job is not in REVIEW state."""
        job_id, _series_ids, file_ids, _iids = await _seed_job_with_files(
            _db_factory, job_status=ImportJobStatus.IMPORTING, conflict_files=2
        )
        # Conflict file IDs start after matched files (3 matched + conflict files)
        conflict_file_ids = [fid for fid in file_ids]
        resp = await client.put(
            f"/api/v1/import/{job_id}/conflicts/1/resolve",
            json={"chosen_file_id": conflict_file_ids[-1]},
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_resolve_already_resolved(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Resolving an already-resolved conflict re-applies the resolution."""
        job_id, _series_ids, file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=0, conflict_files=2
        )
        # Resolve with first file
        resp1 = await client.put(
            f"/api/v1/import/{job_id}/conflicts/1/resolve",
            json={"chosen_file_id": file_ids[0]},
        )
        assert resp1.status_code == 200

        # Re-resolve with second file
        resp2 = await client.put(
            f"/api/v1/import/{job_id}/conflicts/1/resolve",
            json={"chosen_file_id": file_ids[1]},
        )
        assert resp2.status_code == 200
        confirmed = [f for f in resp2.json()["files"] if f["status"] == "confirmed"]
        assert len(confirmed) == 1
        assert confirmed[0]["id"] == file_ids[1]


class TestResolveConflictBulk:
    """Test POST /api/v1/import/{job_id}/conflicts/resolve-bulk."""

    @pytest.mark.asyncio
    async def test_resolves_multiple_conflicts(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        job_id, _series_ids, file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=0, conflict_files=4
        )
        resp = await client.post(
            f"/api/v1/import/{job_id}/conflicts/resolve-bulk",
            json={
                "resolutions": [
                    {"conflict_group_id": 1, "chosen_file_id": file_ids[0]},
                    {"conflict_group_id": 2, "chosen_file_id": file_ids[2]},
                ]
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["resolved_group_ids"] == [1, 2]
        assert data["resolved_count"] == 2
        assert len(data["resolved_series_ids"]) == 1

        conflicts = await client.get(f"/api/v1/import/{job_id}/conflicts")
        assert conflicts.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_rejects_duplicate_group_resolution(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        job_id, _series_ids, file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=0, conflict_files=2
        )
        resp = await client.post(
            f"/api/v1/import/{job_id}/conflicts/resolve-bulk",
            json={
                "resolutions": [
                    {"conflict_group_id": 1, "chosen_file_id": file_ids[0]},
                    {"conflict_group_id": 1, "chosen_file_id": file_ids[1]},
                ]
            },
        )

        assert resp.status_code == 400


class TestResetConflicts:
    """Test POST /api/v1/import/{job_id}/conflicts/reset."""

    @pytest.mark.asyncio
    async def test_resets_resolved_conflicts(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        job_id, _series_ids, file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=0, conflict_files=2
        )
        chosen_id = file_ids[0]
        resolve = await client.put(
            f"/api/v1/import/{job_id}/conflicts/1/resolve",
            json={"chosen_file_id": chosen_id},
        )
        assert resolve.status_code == 200

        reset = await client.post(
            f"/api/v1/import/{job_id}/conflicts/reset",
            json={"group_ids": [1]},
        )
        assert reset.status_code == 200
        assert reset.json()["reset_group_ids"] == [1]
        assert len(reset.json()["reset_series_ids"]) == 1

        conflicts = await client.get(f"/api/v1/import/{job_id}/conflicts")
        assert conflicts.status_code == 200
        groups = conflicts.json()["groups"]
        assert len(groups) == 1
        assert {item["status"] for item in groups[0]["files"]} == {"conflict"}

    @pytest.mark.asyncio
    async def test_resets_all_resolved_conflicts_when_no_group_ids_supplied(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        job_id, _series_ids, file_ids, _iids = await _seed_job_with_files(
            _db_factory, files_per_series=0, conflict_files=2
        )
        resolve = await client.put(
            f"/api/v1/import/{job_id}/conflicts/1/resolve",
            json={"chosen_file_id": file_ids[0]},
        )
        assert resolve.status_code == 200

        reset = await client.post(
            f"/api/v1/import/{job_id}/conflicts/reset",
            json={},
        )
        assert reset.status_code == 200
        assert reset.json()["reset_group_ids"] == [1]

    # ── Tests: Auth Required ─────────────────────────────────────────────


class TestFileApiAuthRequired:
    """All new file-level endpoints return 401 without valid session."""

    @pytest.mark.asyncio
    async def test_requires_auth(
        self,
        unauth_client: AsyncClient,
    ) -> None:
        """All file-level endpoints return 401 without authentication."""
        endpoints = [
            ("GET", "/api/v1/import/1/series/1/files"),
            ("GET", "/api/v1/import/1/conflicts"),
            ("PUT", "/api/v1/import/1/files/1/match"),
            ("PUT", "/api/v1/import/1/conflicts/1/resolve"),
            ("POST", "/api/v1/import/1/conflicts/resolve-bulk"),
            ("POST", "/api/v1/import/1/conflicts/reset"),
        ]
        for method, url in endpoints:
            resp = await unauth_client.request(method, url)
            assert resp.status_code == 401, f"{method} {url} should require auth"
