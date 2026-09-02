"""Tests for Intervention Queue API endpoints — ESM Phase 2, Task 2.4.

Covers:
- GET /api/v1/intervention — paginated list with status/series/confidence filters
- GET /api/v1/intervention/count — pending count
- GET /api/v1/intervention/{id} — single detail
- POST /api/v1/intervention/{id}/approve — approve and trigger download
- POST /api/v1/intervention/{id}/reject — reject with optional reason
- Authentication requirement on all endpoints

Run:
    pytest tests/api/test_intervention_api.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.direct_acquisition import DirectAcquisitionAttempt, DirectAcquisitionState
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import APIKey, User
from pullbox.services.auth_service import AuthService
from pullbox.services.intervention_service import InterventionService

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-intervention")


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
    raw_key = "pb_k1_" + "a" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="interventionuser",
            password_hash=AuthService.hash_password("Test@1234"),
        )
        session.add(user)
        await session.flush()
        session.add(APIKey(user_id=user.id, key_hash=key_hash, name="intervention-test"))
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


async def _seed_pending_matches(
    factory: async_sessionmaker[AsyncSession],
    *,
    count: int = 3,
    statuses: list[PendingMatchStatus] | None = None,
) -> list[int]:
    """Seed a series, issue, and pending matches. Returns list of pending match IDs."""
    if statuses is None:
        statuses = [PendingMatchStatus.PENDING] * count

    async with factory() as session:
        series = Series(
            comicvine_id=99900,
            title="Batman",
            sort_title="Batman",
            year_start=2016,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=1,
        )
        session.add(series)
        await session.flush()

        issue = Issue(
            series_id=series.id,
            comicvine_id=50001,
            issue_number=1.0,
            title="Issue #1",
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        session.add(issue)
        await session.flush()

        pm_ids: list[int] = []
        for i, status in enumerate(statuses):
            pm = PendingMatch(
                issue_id=issue.id,
                release_title=f"Batman 001 (2016) [Digital] Release{i}.cbz",
                download_url=f"https://indexer.example.com/dl/test{i}",
                is_torrent=False,
                file_size=100_000_000 + i * 1_000_000,
                confidence="medium",
                match_details={
                    "parsed_series": "Batman",
                    "parsed_issue": 1.0,
                    "parsed_year": 2016,
                    "series_similarity": 0.95,
                    "issue_match": True,
                    "year_match": True,
                    "type_match": True,
                    "indexer_name": "NZBgeek",
                    "age_days": 2,
                },
                status=status,
            )
            session.add(pm)
            await session.flush()
            pm_ids.append(pm.id)

        await session.commit()
        return pm_ids


# ── TestInterventionAPI ────────────────────────────────────────────────


class TestInterventionAPI:
    """Tests for the intervention queue API endpoints."""

    @pytest.mark.asyncio
    async def test_list_pending(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /intervention returns paginated pending matches."""
        await _seed_pending_matches(_db_factory, count=3)

        resp = await client.get("/api/v1/intervention")

        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        assert "page" in data
        assert "pages" in data
        assert data["total"] == 3
        assert len(data["items"]) == 3

    @pytest.mark.asyncio
    async def test_list_filter_by_status(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """status query param filters results."""
        await _seed_pending_matches(
            _db_factory,
            statuses=[
                PendingMatchStatus.PENDING,
                PendingMatchStatus.APPROVED,
                PendingMatchStatus.REJECTED,
            ],
        )

        resp = await client.get("/api/v1/intervention?status=approved")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1

    @pytest.mark.asyncio
    async def test_count_pending(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /intervention/count returns correct pending count."""
        await _seed_pending_matches(
            _db_factory,
            statuses=[
                PendingMatchStatus.PENDING,
                PendingMatchStatus.PENDING,
                PendingMatchStatus.REJECTED,
            ],
        )

        resp = await client.get("/api/v1/intervention/count")

        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2

    @pytest.mark.asyncio
    async def test_get_detail(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """GET /intervention/{id} returns single pending match detail."""
        pm_ids = await _seed_pending_matches(_db_factory, count=1)

        resp = await client.get(f"/api/v1/intervention/{pm_ids[0]}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == pm_ids[0]
        assert data["release_title"].startswith("Batman")
        assert data["confidence"] == "medium"

    @pytest.mark.asyncio
    async def test_approve_sends_to_client(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST /intervention/{id}/approve triggers download and returns 201."""
        pm_ids = await _seed_pending_matches(_db_factory, count=1)

        mock_download = MagicMock()
        mock_download.id = 42
        mock_download.issue_id = 1
        mock_download.title = "Batman 001 (2016) [Digital] Release0.cbz"
        mock_download.state = "sent"

        mock_registry = MagicMock()

        with (
            patch(
                "pullbox.composition.providers.build_registry",
                new_callable=AsyncMock,
                return_value=(mock_registry, {}),
            ),
            patch(
                "pullbox.api.v1.intervention.InterventionService.approve_match",
                new_callable=AsyncMock,
                return_value=mock_download,
            ),
        ):
            resp = await client.post(f"/api/v1/intervention/{pm_ids[0]}/approve")

        assert resp.status_code == 201
        data = resp.json()
        assert data["download_id"] == 42
        assert data["status"] == "sent"

    @pytest.mark.asyncio
    async def test_approve_dc_does_not_require_legacy_download_client(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        pm_ids = await _seed_pending_matches(_db_factory, count=1)
        async with _db_factory() as session:
            pending = await session.get(PendingMatch, pm_ids[0])
            pending.match_details = {"source_kind": "dc"}
            await session.commit()
        history = MagicMock(spec=DownloadHistory)
        history.id = 12
        history.issue_id = 1
        history.title = "Batman 001"
        history.state = DownloadState.SENT
        with (
            patch(
                "pullbox.composition.services.build_domain_download_service",
                new_callable=AsyncMock,
                return_value=None,
            ) as build,
            patch.object(InterventionService, "approve_match", AsyncMock(return_value=history)),
        ):
            response = await client.post(f"/api/v1/intervention/{pm_ids[0]}/approve")
        assert response.status_code == 201
        assert response.json()["source_kind"] == "dc"
        build.assert_not_awaited()

    async def test_approve_direct_does_not_require_download_client(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Direct intervention approval bypasses legacy download-client composition."""
        pm_ids = await _seed_pending_matches(_db_factory, count=1)
        async with _db_factory() as session:
            pending = await session.get(PendingMatch, pm_ids[0])
            assert pending is not None
            pending.download_url = "pullbox-direct://attempt/12"
            pending.match_details = {
                "source_kind": "direct",
                "direct_attempt_id": 12,
                "provider_name": "GetComics",
            }
            await session.commit()

        attempt = MagicMock(spec=DirectAcquisitionAttempt)
        attempt.id = 12
        attempt.issue_id = 1
        attempt.state = DirectAcquisitionState.PLANNED
        attempt.candidate_snapshot = {"display_title": "Batman 001 (2016)"}

        with (
            patch(
                "pullbox.composition.services.build_domain_download_service",
                new_callable=AsyncMock,
            ) as build_download,
            patch(
                "pullbox.api.v1.intervention.InterventionService.approve_match",
                new_callable=AsyncMock,
                return_value=attempt,
            ),
        ):
            response = await client.post(f"/api/v1/intervention/{pm_ids[0]}/approve")

        assert response.status_code == 201
        assert response.json() == {
            "download_id": None,
            "acquisition_id": 12,
            "issue_id": 1,
            "title": "Batman 001 (2016)",
            "status": "planned",
            "source_kind": "direct",
        }
        build_download.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reject_updates_status(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST /intervention/{id}/reject sets REJECTED status."""
        pm_ids = await _seed_pending_matches(_db_factory, count=1)

        resp = await client.post(
            f"/api/v1/intervention/{pm_ids[0]}/reject",
            json={"reason": "Wrong volume"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "Match rejected."

        # Verify status updated in DB
        async with _db_factory() as session:
            pm = await session.get(PendingMatch, pm_ids[0])
            assert pm is not None
            assert pm.status == PendingMatchStatus.REJECTED

    @pytest.mark.asyncio
    async def test_approve_nonexistent_returns_404(
        self,
        client: AsyncClient,
    ) -> None:
        """POST /intervention/99999/approve returns 404."""
        resp = await client.post("/api/v1/intervention/99999/approve")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_reject_nonexistent_returns_404(
        self,
        client: AsyncClient,
    ) -> None:
        """POST /intervention/99999/reject returns 404."""
        resp = await client.post("/api/v1/intervention/99999/reject")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_requires_auth(
        self,
        unauth_client: AsyncClient,
    ) -> None:
        """All endpoints return 401 without authentication."""
        endpoints = [
            ("GET", "/api/v1/intervention"),
            ("GET", "/api/v1/intervention/count"),
            ("GET", "/api/v1/intervention/1"),
            ("POST", "/api/v1/intervention/1/approve"),
            ("POST", "/api/v1/intervention/1/reject"),
        ]
        for method, url in endpoints:
            resp = await unauth_client.request(method, url)
            assert resp.status_code == 401, f"{method} {url} should require auth"


# ── Direct handler unit tests (coverage supplement) ────────────────────


class TestResolveStatus:
    """Tests for _resolve_status helper."""

    def test_valid_status_parsed(self) -> None:
        from pullbox.api.v1.intervention import _resolve_status

        assert _resolve_status("approved") == PendingMatchStatus.APPROVED
        assert _resolve_status("rejected") == PendingMatchStatus.REJECTED
        assert _resolve_status("pending") == PendingMatchStatus.PENDING

    def test_invalid_status_defaults_to_pending(self) -> None:
        from pullbox.api.v1.intervention import _resolve_status

        assert _resolve_status("bogus") == PendingMatchStatus.PENDING

    def test_none_defaults_to_pending(self) -> None:
        from pullbox.api.v1.intervention import _resolve_status

        assert _resolve_status(None) == PendingMatchStatus.PENDING


class TestToResponse:
    """Tests for _to_response helper."""

    def test_converts_pending_match(self) -> None:
        from pullbox.api.v1.intervention import _to_response

        issue = MagicMock()
        issue.id = 10
        issue.issue_number = 5.0
        issue.issue_type = IssueType.ISSUE
        issue.series = MagicMock()
        issue.series.title = "Batman"

        pm = MagicMock()
        pm.id = 1
        pm.issue = issue
        pm.release_title = "Batman 005 (2016).cbz"
        pm.download_url = "https://example.com/dl"
        pm.file_size = 50_000_000
        pm.confidence = "high"
        pm.match_details = {"parsed_series": "Batman"}
        pm.is_torrent = False
        pm.status = "pending"
        pm.created_at = "2026-03-01T00:00:00"
        pm.resolved_at = None
        pm.resolved_by = None

        resp = _to_response(pm)

        assert resp.id == 1
        assert resp.issue.id == 10
        assert resp.issue.series_title == "Batman"
        assert resp.issue.issue_number == 5.0
        assert resp.release_title == "Batman 005 (2016).cbz"
        assert resp.confidence == "high"

    def test_handles_no_series(self) -> None:
        from pullbox.api.v1.intervention import _to_response

        issue = MagicMock()
        issue.id = 10
        issue.issue_number = 1.0
        issue.issue_type = None
        issue.series = None

        pm = MagicMock()
        pm.id = 2
        pm.issue = issue
        pm.release_title = "Release.cbz"
        pm.download_url = "https://example.com/dl"
        pm.file_size = None
        pm.confidence = "low"
        pm.match_details = {}
        pm.is_torrent = False
        pm.status = "pending"
        pm.created_at = "2026-03-01T00:00:00"
        pm.resolved_at = None
        pm.resolved_by = None

        resp = _to_response(pm)

        assert resp.issue.series_title is None
        assert resp.issue.issue_type is None


class TestHandlersDirect:
    """Direct handler tests for branches not covered by ASGI transport."""

    @pytest.mark.asyncio
    async def test_list_with_series_filter(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """list_pending_matches with series_id filter exercises filter branch."""
        from pullbox.api.v1.intervention import list_pending_matches

        pm_ids = await _seed_pending_matches(_db_factory, count=2)

        async with _db_factory() as session:
            # Get the series_id from the seeded data
            pm = await session.get(PendingMatch, pm_ids[0])
            assert pm is not None
            issue = await session.get(Issue, pm.issue_id)
            assert issue is not None
            series_id = issue.series_id

            # Mock user
            mock_user = MagicMock()

            result = await list_pending_matches(
                user=mock_user,
                session=session,
                series_id=series_id,
            )
            assert result.total == 2
            assert len(result.items) == 2

    @pytest.mark.asyncio
    async def test_list_with_confidence_filter(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """list_pending_matches with confidence filter exercises filter branch."""
        from pullbox.api.v1.intervention import list_pending_matches

        await _seed_pending_matches(_db_factory, count=2)

        async with _db_factory() as session:
            mock_user = MagicMock()

            result = await list_pending_matches(
                user=mock_user,
                session=session,
                confidence="medium",
            )
            assert result.total == 2

            result_none = await list_pending_matches(
                user=mock_user,
                session=session,
                confidence="high",
            )
            assert result_none.total == 0

    @pytest.mark.asyncio
    async def test_count_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """pending_match_count called directly."""
        from pullbox.api.v1.intervention import pending_match_count

        await _seed_pending_matches(_db_factory, count=2)

        async with _db_factory() as session:
            mock_user = MagicMock()
            result = await pending_match_count(user=mock_user, session=session)
            assert result.count == 2

    @pytest.mark.asyncio
    async def test_detail_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """get_pending_match called directly."""
        from pullbox.api.v1.intervention import get_pending_match

        pm_ids = await _seed_pending_matches(_db_factory, count=1)

        async with _db_factory() as session:
            mock_user = MagicMock()
            result = await get_pending_match(pending_id=pm_ids[0], user=mock_user, session=session)
            assert result.id == pm_ids[0]
            assert result.release_title.startswith("Batman")

    @pytest.mark.asyncio
    async def test_detail_not_found(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """get_pending_match raises NotFoundError for missing ID."""
        from pullbox.api.v1.intervention import get_pending_match
        from pullbox.core.exceptions import NotFoundError

        async with _db_factory() as session:
            mock_user = MagicMock()
            with pytest.raises(NotFoundError):
                await get_pending_match(pending_id=99999, user=mock_user, session=session)

    @pytest.mark.asyncio
    async def test_reject_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """reject_pending_match called directly."""
        from pullbox.api.v1.intervention import reject_pending_match

        pm_ids = await _seed_pending_matches(_db_factory, count=1)

        async with _db_factory() as session:
            mock_user = MagicMock()
            mock_user.username = "testuser"
            result = await reject_pending_match(
                pending_id=pm_ids[0], user=mock_user, session=session, body=None
            )
            assert result["message"] == "Match rejected."
            await session.commit()

        # Verify DB state
        async with _db_factory() as session:
            pm = await session.get(PendingMatch, pm_ids[0])
            assert pm is not None
            assert pm.status == PendingMatchStatus.REJECTED

    @pytest.mark.asyncio
    async def test_approve_direct(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """approve_pending_match called directly with mocked registry."""
        from pullbox.api.v1.intervention import approve_pending_match

        pm_ids = await _seed_pending_matches(_db_factory, count=1)

        mock_download = MagicMock()
        mock_download.id = 99
        mock_download.issue_id = 1
        mock_download.title = "Batman 001 (2016) [Digital] Release0.cbz"
        mock_download.state = "sent"

        mock_registry = MagicMock()

        async with _db_factory() as session:
            mock_user = MagicMock()
            mock_user.username = "testuser"

            with (
                patch(
                    "pullbox.composition.providers.build_registry",
                    new_callable=AsyncMock,
                    return_value=(mock_registry, {}),
                ),
                patch.object(
                    InterventionService,
                    "approve_match",
                    new_callable=AsyncMock,
                    return_value=mock_download,
                ),
            ):
                result = await approve_pending_match(
                    pending_id=pm_ids[0], user=mock_user, session=session
                )

            assert result.download_id == 99
            assert result.status == "sent"


class TestBulkActions:
    """Tests for bulk approve/reject API endpoints."""

    @pytest.mark.asyncio
    async def test_bulk_approve_success(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST /intervention/bulk-approve approves all 3 pending matches."""
        pm_ids = await _seed_pending_matches(_db_factory, count=3)

        mock_download = MagicMock()
        mock_download.id = 42
        mock_download.issue_id = 1
        mock_download.title = "Batman 001 (2016) [Digital] Release0.cbz"
        mock_download.state = "sent"

        with (
            patch(
                "pullbox.composition.providers.build_registry",
                new_callable=AsyncMock,
                return_value=(MagicMock(), {}),
            ),
            patch(
                "pullbox.services.intervention_service.InterventionService.approve_match",
                new_callable=AsyncMock,
                return_value=mock_download,
            ),
        ):
            resp = await client.post(
                "/api/v1/intervention/bulk-approve",
                json={"ids": pm_ids},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["processed"] == 3
        assert data["succeeded"] == 3
        assert data["failed"] == 0

    @pytest.mark.asyncio
    async def test_bulk_reject_success(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """POST /intervention/bulk-reject rejects all 3 pending matches."""
        pm_ids = await _seed_pending_matches(_db_factory, count=3)

        resp = await client.post(
            "/api/v1/intervention/bulk-reject",
            json={"ids": pm_ids},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["processed"] == 3
        assert data["succeeded"] == 3
        assert data["failed"] == 0

    @pytest.mark.asyncio
    async def test_bulk_approve_partial_failure(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Bulk approve with 2 pending + 1 rejected yields 2 success + 1 failure."""
        pm_ids = await _seed_pending_matches(
            _db_factory,
            statuses=[
                PendingMatchStatus.PENDING,
                PendingMatchStatus.PENDING,
                PendingMatchStatus.REJECTED,
            ],
        )

        mock_download = MagicMock()
        mock_download.id = 42
        mock_download.issue_id = 1
        mock_download.title = "Batman 001 (2016) [Digital] Release0.cbz"
        mock_download.state = "sent"

        with (
            patch(
                "pullbox.composition.providers.build_registry",
                new_callable=AsyncMock,
                return_value=(MagicMock(), {}),
            ),
            patch(
                "pullbox.services.intervention_service.InterventionService.approve_match",
                new_callable=AsyncMock,
                side_effect=[mock_download, mock_download, ValueError("not pending")],
            ),
        ):
            resp = await client.post(
                "/api/v1/intervention/bulk-approve",
                json={"ids": pm_ids},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["processed"] == 3
        assert data["succeeded"] == 2
        assert data["failed"] == 1

    @pytest.mark.asyncio
    async def test_bulk_empty_ids_returns_422(
        self,
        client: AsyncClient,
    ) -> None:
        """Empty ids list returns 422 validation error."""
        resp = await client.post(
            "/api/v1/intervention/bulk-approve",
            json={"ids": []},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_bulk_reject_with_reason(
        self,
        client: AsyncClient,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Bulk reject passes reason through to service."""
        pm_ids = await _seed_pending_matches(_db_factory, count=2)

        resp = await client.post(
            "/api/v1/intervention/bulk-reject",
            json={"ids": pm_ids, "reason": "Wrong series"},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["succeeded"] == 2

        # Verify reason stored in DB
        async with _db_factory() as session:
            pm = await session.get(PendingMatch, pm_ids[0])
            assert pm is not None
            assert pm.match_details.get("rejection_reason") == "Wrong series"

    @pytest.mark.asyncio
    async def test_approve_no_registry(
        self,
        _db_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """approve_pending_match raises ProviderError when no clients configured."""
        from pullbox.api.v1.intervention import approve_pending_match
        from pullbox.core.exceptions import ProviderError

        pm_ids = await _seed_pending_matches(_db_factory, count=1)

        async with _db_factory() as session:
            mock_user = MagicMock()
            mock_user.username = "testuser"

            with (
                patch(
                    "pullbox.composition.providers.build_registry",
                    new_callable=AsyncMock,
                    return_value=None,
                ),
                pytest.raises(ProviderError),
            ):
                await approve_pending_match(pending_id=pm_ids[0], user=mock_user, session=session)
