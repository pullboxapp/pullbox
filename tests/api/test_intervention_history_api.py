"""API coverage for intervention history cleanup endpoints."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models import Base
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.models.user import APIKey, User


@pytest.fixture
async def _db_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def _api_key_header(
    _db_factory: async_sessionmaker[AsyncSession],
    seeded_user_password_hash: str,
) -> str:
    raw_key = "pb_k1_" + "c" * 64
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    async with _db_factory() as session:
        user = User(
            username="interventionhistoryapiuser",
            password_hash=seeded_user_password_hash,
        )
        session.add(user)
        await session.flush()
        session.add(
            APIKey(
                user_id=user.id,
                key_hash=key_hash,
                name="intervention-history-api-test",
            )
        )
        await session.commit()
    return raw_key


@pytest.fixture
async def client(
    _db_factory: async_sessionmaker[AsyncSession],
    _api_key_header: str,
) -> AsyncClient:
    from pullbox.api.deps import get_db_dep
    from pullbox.api.middleware import reset_setup_cache
    from pullbox.app import create_app

    app = create_app()

    async def _override_db() -> AsyncSession:
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


async def _seed_history_and_queue(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int, int]:
    async with factory() as session:
        series = Series(
            comicvine_id=91000,
            title="Daredevil",
            sort_title="Daredevil",
            year_start=2019,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=2,
        )
        session.add(series)
        await session.flush()

        issue = Issue(
            series_id=series.id,
            comicvine_id=61000,
            issue_number=8.0,
            title="Issue #8",
            status=IssueStatus.WANTED,
            issue_type=IssueType.ISSUE,
        )
        session.add(issue)
        await session.flush()

        pending = PendingMatch(
            issue_id=issue.id,
            release_title="Daredevil 008 Pending.cbz",
            download_url="https://example.invalid/pending",
            is_torrent=False,
            file_size=88_000_000,
            confidence="medium",
            match_details={"indexer_name": "Pending Source"},
            status=PendingMatchStatus.PENDING,
        )
        approved = PendingMatch(
            issue_id=issue.id,
            release_title="Daredevil 008 Approved.cbz",
            download_url="https://example.invalid/approved",
            is_torrent=False,
            file_size=89_000_000,
            confidence="high",
            match_details={"indexer_name": "Approved Source"},
            status=PendingMatchStatus.APPROVED,
            resolved_at=datetime.now(UTC),
            resolved_by="user",
        )
        rejected = PendingMatch(
            issue_id=issue.id,
            release_title="Daredevil 008 Rejected.cbz",
            download_url="https://example.invalid/rejected",
            is_torrent=True,
            file_size=90_000_000,
            confidence="low",
            match_details={"indexer_name": "Rejected Source", "rejection_reason": "Bad scan"},
            status=PendingMatchStatus.REJECTED,
            resolved_at=datetime.now(UTC),
            resolved_by="user",
        )
        session.add_all([pending, approved, rejected])
        await session.flush()
        await session.commit()
        return pending.id, approved.id, rejected.id


@pytest.mark.asyncio
async def test_clear_intervention_history_preserves_pending(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    pending_id, _, _ = await _seed_history_and_queue(_db_factory)

    resp = await client.delete("/api/v1/intervention/history")

    assert resp.status_code == 200
    assert resp.json() == {"deleted": 2}

    async with _db_factory() as session:
        remaining = list((await session.execute(select(PendingMatch))).scalars().all())

    assert len(remaining) == 1
    assert remaining[0].id == pending_id
    assert remaining[0].status == PendingMatchStatus.PENDING


@pytest.mark.asyncio
async def test_remove_intervention_history_entry_rejects_pending(
    client: AsyncClient,
    _db_factory: async_sessionmaker[AsyncSession],
) -> None:
    pending_id, approved_id, _ = await _seed_history_and_queue(_db_factory)

    pending_resp = await client.delete(f"/api/v1/intervention/history/{pending_id}")
    assert pending_resp.status_code == 409

    approved_resp = await client.delete(f"/api/v1/intervention/history/{approved_id}")
    assert approved_resp.status_code == 204

    async with _db_factory() as session:
        remaining_ids = {
            pending_match.id
            for pending_match in (await session.execute(select(PendingMatch))).scalars().all()
        }

    assert pending_id in remaining_ids
    assert approved_id not in remaining_ids
