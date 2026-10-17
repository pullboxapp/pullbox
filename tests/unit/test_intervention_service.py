"""Tests for InterventionService — ESM Phase 2, Task 2.2.

Covers:
- Creating pending matches (with deduplication)
- Approving pending matches (sends to download client)
- Rejecting pending matches (with optional reason)
- Listing pending matches (with filters and pagination)
- Expiring stale pending matches

Run:
    pytest tests/unit/test_intervention_service.py -v
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models.blocklist import BlocklistEntry
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactAttempt,
    DirectArtifactFailureClass,
    DirectArtifactHostKind,
    DirectArtifactRouteKind,
    DirectArtifactState,
)
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import MatchConfidence
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.providers.base import ReleaseResult
from pullbox.providers.direct.contract import DirectCandidate, DirectParsedCandidate
from pullbox.services.direct_search_coordinator import (
    DirectSearchProvider,
    DirectValidatedCandidate,
)
from pullbox.services.release_validator import ValidationResult
from tests.sqlite_schema import create_test_schema

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_conn: object, _rec: object) -> None:
        dbapi_conn.execute("PRAGMA foreign_keys=ON")  # type: ignore[union-attr]

    async with engine.begin() as conn:
        await create_test_schema(conn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _make_series(
    *,
    title: str = "Batman",
    comicvine_id: int = 99900,
    year_start: int = 2016,
) -> Series:
    return Series(
        comicvine_id=comicvine_id,
        title=title,
        sort_title=title,
        year_start=year_start,
        status=SeriesStatus.CONTINUING,
        series_type=SeriesType.STANDARD,
        monitored=True,
        issue_count=10,
    )


def _make_issue(series_id: int, *, issue_number: float = 1.0) -> Issue:
    return Issue(
        series_id=series_id,
        comicvine_id=50000 + int(issue_number),
        issue_number=issue_number,
        title=f"Issue #{int(issue_number)}",
        status=IssueStatus.WANTED,
        issue_type=IssueType.ISSUE,
    )


async def _seed_issue(
    session: AsyncSession,
    *,
    series_title: str = "Batman",
    issue_number: float = 1.0,
) -> Issue:
    series = _make_series(title=series_title)
    session.add(series)
    await session.flush()
    issue = _make_issue(series.id, issue_number=issue_number)
    session.add(issue)
    await session.flush()
    return issue


def _make_release(
    title: str = "Batman 001 (2016).cbz",
    *,
    download_url: str = "https://indexer.example.com/dl/123",
) -> ReleaseResult:
    return ReleaseResult(
        title=title,
        indexer_name="NZBgeek",
        download_url=download_url,
        size_bytes=100_000_000,
        age_days=2,
        seeders=None,
        leechers=None,
        grabs=50,
        is_torrent=False,
        category="7030",
        published_at=None,
    )


def _make_validation(
    release: ReleaseResult,
    *,
    confidence: MatchConfidence = MatchConfidence.MEDIUM,
    similarity: float = 0.88,
) -> ValidationResult:
    parsed = MagicMock(spec="ParsedRelease")
    parsed.series_name = "Batman"
    parsed.issue_number = 1.0
    parsed.year = 2016
    parsed.issue_type = IssueType.ISSUE
    parsed.original_title = release.title

    return ValidationResult(
        is_match=True,
        confidence=confidence,
        parsed=parsed,
        release=release,
        series_similarity=similarity,
        issue_match=True,
        year_match=True,
        issue_type_match=True,
    )


def _make_direct_candidate() -> DirectValidatedCandidate:
    release = _make_release(download_url="direct://candidate/opaque")
    validation = _make_validation(release)
    return DirectValidatedCandidate(
        provider=DirectSearchProvider(
            provider_config_id=7,
            provider_identity="pullbox.getcomics",
            display_name="GetComics",
            endpoint="http://provider:8780",
            bearer_token="provider-secret-that-must-not-be-stored",
        ),
        candidate=DirectCandidate(
            provider_candidate_id="candidate-7",
            source_reference="https://getcomics.org/private/post?token=secret",
            display_title=release.title,
            raw_title=release.title,
            parsed=DirectParsedCandidate(
                series_title="Batman",
                issue_numbers=["1"],
                year=2016,
                format="cbz",
                quality="digital",
            ),
            provider_confidence=0.91,
        ),
        release=release,
        validation=validation,
    )


def _make_pending(
    issue_id: int,
    *,
    url: str = "https://indexer.example.com/dl/123",
    status: PendingMatchStatus = PendingMatchStatus.PENDING,
    confidence: str = "medium",
    created_at: datetime | None = None,
) -> PendingMatch:
    pm = PendingMatch(
        issue_id=issue_id,
        release_title="Batman 001.cbz",
        download_url=url,
        is_torrent=False,
        file_size=100_000_000,
        confidence=confidence,
        match_details={"indexer_name": "NZBgeek", "age_days": 2},
        status=status,
    )
    if created_at is not None:
        pm.created_at = created_at
    return pm


def _make_direct_pending(issue_id: int, attempt_id: int) -> PendingMatch:
    return PendingMatch(
        issue_id=issue_id,
        release_title="Batman 001 (2016) (Digital).cbz",
        download_url=f"pullbox-direct://attempt/{attempt_id}",
        is_torrent=False,
        confidence="medium",
        match_details={
            "source_kind": "direct",
            "direct_attempt_id": attempt_id,
            "provider_identity": "pullbox.getcomics",
            "provider_name": "GetComics",
        },
    )


async def _seed_direct_attempt(session: AsyncSession, issue_id: int) -> DirectAcquisitionAttempt:
    attempt = DirectAcquisitionAttempt(
        request_key=f"intervention:{issue_id}",
        issue_id=issue_id,
        provider_identity="pullbox.getcomics",
        provider_candidate_id="candidate-1",
        state=DirectAcquisitionState.INTERVENTION,
        requested_coverage={"issue_numbers": ["1"]},
        candidate_snapshot={"display_title": "Batman 001 (2016) (Digital).cbz"},
        failure_class=DirectArtifactFailureClass.USER_ACTION,
        failure_code="semantic_review_required",
        error_message="Review this direct result before downloading.",
    )
    session.add(attempt)
    await session.flush()
    return attempt


def _mock_download_service() -> AsyncMock:
    svc = AsyncMock()
    download = MagicMock(spec=DownloadHistory)
    download.id = 1
    download.state = DownloadState.SENT
    download.title = "Batman 001.cbz"
    svc.send_to_client.return_value = download
    return svc


# ── TestCreatePendingMatch ────────────────────────────────────────────


class TestCreatePendingMatch:
    """Tests for InterventionService.create_pending_match."""

    @pytest.mark.asyncio
    async def test_creates_pending_match(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Creates a PendingMatch record with correct fields."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            issue = await _seed_issue(session)
            release = _make_release()
            validation = _make_validation(release)

            result = await svc.create_pending_match(session, issue.id, release, validation)
            await session.commit()

        assert result is not None
        assert result.issue_id == issue.id
        assert result.release_title == release.title
        assert result.download_url == release.download_url
        assert result.confidence == "medium"
        assert result.status == PendingMatchStatus.PENDING

    @pytest.mark.asyncio
    async def test_dedup_existing_pending(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Returns None if matching pending exists for issue+URL."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            issue = await _seed_issue(session)
            release = _make_release()
            validation = _make_validation(release)

            # First creation succeeds
            result1 = await svc.create_pending_match(session, issue.id, release, validation)
            await session.commit()
            assert result1 is not None

        async with db_factory() as session:
            # Second creation with same issue+URL returns None
            result2 = await svc.create_pending_match(session, issue.id, release, validation)
            assert result2 is None

    @pytest.mark.asyncio
    async def test_stores_match_details(self, db_factory: async_sessionmaker[AsyncSession]) -> None:
        """match_details JSON populated from ValidationResult."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            issue = await _seed_issue(session)
            release = _make_release()
            validation = _make_validation(release, similarity=0.92)

            result = await svc.create_pending_match(session, issue.id, release, validation)
            await session.commit()

        assert result is not None
        details = result.match_details
        assert details["parsed_series"] == "Batman"
        assert details["series_similarity"] == 0.92
        assert details["issue_match"] is True
        assert details["year_match"] is True
        assert details["indexer_name"] == "NZBgeek"
        assert details["age_days"] == 2

    @pytest.mark.asyncio
    async def test_creates_redacted_direct_pending_match(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Direct intervention stores an opaque attempt reference, never source secrets."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()
        result_candidate = _make_direct_candidate()

        async with db_factory() as session:
            issue = await _seed_issue(session)
            result = await svc.create_direct_pending_match(
                session,
                issue.id,
                42,
                result_candidate,
            )
            duplicate = await svc.create_direct_pending_match(
                session,
                issue.id,
                42,
                result_candidate,
            )
            await session.commit()

        assert result is not None
        assert duplicate is None
        assert result.download_url == "pullbox-direct://attempt/42"
        assert result.match_details["source_kind"] == "direct"
        assert result.match_details["direct_attempt_id"] == 42
        assert result.match_details["provider_name"] == "GetComics"
        assert result.match_details["parsed_issue"] == "1"
        persisted = f"{result.download_url} {result.match_details}"
        assert "getcomics.org/private" not in persisted
        assert "provider-secret" not in persisted

    @pytest.mark.asyncio
    async def test_runtime_direct_intervention_preserves_candidate_and_artifact_details(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Runtime artifact review keeps parsed evidence and selected host context."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()
        async with db_factory() as session:
            issue = await _seed_issue(
                session,
                series_title="Murder Drones",
                issue_number=4.0,
            )
            attempt = await _seed_direct_attempt(session, issue.id)
            attempt.candidate_snapshot = {
                "display_title": "Murder Drones #4 (2026)",
                "parsed": {
                    "series_title": "Murder Drones",
                    "issue_numbers": ["4"],
                    "year": 2026,
                },
                "semantic_decision": {
                    "confidence": "high",
                    "series_similarity": 1.0,
                    "match_type": "exact",
                },
            }
            artifact = DirectArtifactAttempt(
                acquisition_attempt_id=attempt.id,
                sequence_no=0,
                artifact_identity="route:datanodes-review",
                route_kind=DirectArtifactRouteKind.DIRECT,
                host_kind=DirectArtifactHostKind.DATANODES,
                state=DirectArtifactState.INTERVENTION,
                is_selected=True,
            )
            session.add(artifact)
            await session.flush()
            await session.refresh(attempt, ["artifact_attempts"])

            pending = await svc.create_direct_attempt_intervention(session, attempt)
            await session.commit()

        assert pending.match_details["parsed_series"] == "Murder Drones"
        assert pending.match_details["parsed_issue"] == "4"
        assert pending.match_details["parsed_year"] == 2026
        assert pending.match_details["artifact_host_kind"] == "datanodes"


# ── TestApproveMatch ──────────────────────────────────────────────────


class TestApproveMatch:
    """Tests for InterventionService.approve_match."""

    @pytest.mark.asyncio
    async def test_approve_sends_to_client(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Approving a match calls send_to_client with correct args."""
        from pullbox.services.intervention_service import InterventionService

        mock_dl = _mock_download_service()
        svc = InterventionService(download_service=mock_dl)

        async with db_factory() as session:
            issue = await _seed_issue(session)
            pm = _make_pending(issue.id)
            session.add(pm)
            await session.commit()
            pm_id = pm.id

        async with db_factory() as session:
            await svc.approve_match(session, pm_id)
            await session.commit()

        mock_dl.send_to_client.assert_called_once()
        call_args = mock_dl.send_to_client.call_args
        # Verify the release was reconstructed
        release_arg = call_args[1].get("release") or call_args[0][1]
        assert release_arg.title == "Batman 001.cbz"

    @pytest.mark.asyncio
    async def test_approve_updates_status(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Status changes from PENDING to APPROVED."""
        from pullbox.services.intervention_service import InterventionService

        mock_dl = _mock_download_service()
        svc = InterventionService(download_service=mock_dl)

        async with db_factory() as session:
            issue = await _seed_issue(session)
            pm = _make_pending(issue.id)
            session.add(pm)
            await session.commit()
            pm_id = pm.id

        async with db_factory() as session:
            await svc.approve_match(session, pm_id)
            await session.commit()

        async with db_factory() as session:
            result = await session.get(PendingMatch, pm_id)
            assert result is not None
            assert result.status == PendingMatchStatus.APPROVED

    @pytest.mark.asyncio
    async def test_approve_sets_resolved_at(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """resolved_at and resolved_by are set on approval."""
        from pullbox.services.intervention_service import InterventionService

        mock_dl = _mock_download_service()
        svc = InterventionService(download_service=mock_dl)

        async with db_factory() as session:
            issue = await _seed_issue(session)
            pm = _make_pending(issue.id)
            session.add(pm)
            await session.commit()
            pm_id = pm.id

        async with db_factory() as session:
            await svc.approve_match(session, pm_id)
            await session.commit()

        async with db_factory() as session:
            result = await session.get(PendingMatch, pm_id)
            assert result is not None
            assert result.resolved_at is not None
            assert result.resolved_by == "user"

    @pytest.mark.asyncio
    async def test_approve_non_pending_raises(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Cannot approve an already-approved match."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            issue = await _seed_issue(session)
            pm = _make_pending(issue.id, status=PendingMatchStatus.APPROVED)
            session.add(pm)
            await session.commit()
            pm_id = pm.id

        async with db_factory() as session:
            with pytest.raises(ValueError, match=r"not.*pending"):
                await svc.approve_match(session, pm_id)

    @pytest.mark.asyncio
    async def test_approve_missing_raises(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Approving non-existent ID raises ValueError."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            with pytest.raises(ValueError, match="not found"):
                await svc.approve_match(session, 99999)

    @pytest.mark.asyncio
    async def test_retry_direct_recovery_replans_from_a_fresh_attempt(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Recovery never dispatches the failed artifact route a second time."""
        from pullbox.services.intervention_service import InterventionService

        planner = AsyncMock()
        runner = SimpleNamespace(dispatch=AsyncMock(return_value=True))
        svc = InterventionService(direct_planner=planner, direct_runner_getter=lambda: runner)

        async with db_factory() as session:
            issue = await _seed_issue(session)
            attempt = await _seed_direct_attempt(session, issue.id)
            attempt.failure_class = DirectArtifactFailureClass.PERMANENT_MIRROR
            attempt.failure_code = "artifact_host_contract_changed"
            pending = _make_direct_pending(issue.id, attempt.id)
            pending.match_details.update(
                {
                    "failure_class": "permanent_mirror",
                    "failure_code": "artifact_host_contract_changed",
                }
            )
            session.add(pending)
            await session.commit()
            pending_id = pending.id

        async with db_factory() as session:
            pending = await session.get(PendingMatch, pending_id)
            assert pending is not None
            original = await session.get(DirectAcquisitionAttempt, attempt.id)
            assert original is not None

            async def plan_fresh(_session, *, acquisition_id: int):  # type: ignore[no-untyped-def]
                fresh = await _session.get(DirectAcquisitionAttempt, acquisition_id)
                assert fresh is not None
                assert fresh.id != original.id
                assert fresh.state is DirectAcquisitionState.DISCOVERED
                return SimpleNamespace(
                    attempt=fresh,
                    selected_artifact=SimpleNamespace(id=77),
                    initial_source=None,
                )

            svc._direct_planner = plan_fresh  # type: ignore[assignment]
            fresh_attempt = await svc.retry_direct_recovery(session, pending_id)
            await session.commit()

            assert fresh_attempt.id != original.id
            assert pending.status is PendingMatchStatus.APPROVED
            assert pending.match_details["resolution_reason"] == "recovery_replanned"
            runner.dispatch.assert_awaited_once_with(fresh_attempt.id, 77, initial_source=None)

    @pytest.mark.asyncio
    async def test_retry_direct_recovery_keeps_review_pending_when_dispatch_fails(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A failed recovery dispatch must leave the original recovery item actionable."""
        from pullbox.services.intervention_service import InterventionService

        runner = SimpleNamespace(dispatch=AsyncMock(side_effect=RuntimeError("runner unavailable")))
        svc = InterventionService(direct_runner_getter=lambda: runner)

        async with db_factory() as session:
            issue = await _seed_issue(session)
            prior = await _seed_direct_attempt(session, issue.id)
            prior.failure_class = DirectArtifactFailureClass.PERMANENT_MIRROR
            prior.failure_code = "artifact_host_contract_changed"
            pending = _make_direct_pending(issue.id, prior.id)
            pending.match_details.update({"failure_class": "permanent_mirror"})
            session.add(pending)
            await session.commit()
            pending_id = pending.id

            async def plan_fresh(_session, *, acquisition_id: int):  # type: ignore[no-untyped-def]
                fresh = await _session.get(DirectAcquisitionAttempt, acquisition_id)
                assert fresh is not None
                return SimpleNamespace(
                    attempt=fresh,
                    selected_artifact=SimpleNamespace(id=77),
                    initial_source=None,
                )

            svc._direct_planner = plan_fresh  # type: ignore[assignment]
            with pytest.raises(RuntimeError, match="runner unavailable"):
                await svc.retry_direct_recovery(session, pending_id)

        async with db_factory() as session:
            persisted = await session.get(PendingMatch, pending_id)
            assert persisted is not None
            assert persisted.status == PendingMatchStatus.PENDING
            assert persisted.resolved_at is None

    @pytest.mark.asyncio
    async def test_approve_direct_match_plans_and_dispatches_without_download_client(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Approved direct review uses the direct planner and runner adapter."""
        from pullbox.services.intervention_service import InterventionService

        planner = AsyncMock()
        runner = SimpleNamespace(dispatch=AsyncMock(return_value=True))
        svc = InterventionService(
            direct_planner=planner,
            direct_runner_getter=lambda: runner,
        )

        async with db_factory() as session:
            issue = await _seed_issue(session)
            attempt = await _seed_direct_attempt(session, issue.id)
            pending = _make_direct_pending(issue.id, attempt.id)
            session.add(pending)
            await session.commit()
            planner.return_value = SimpleNamespace(
                attempt=attempt,
                selected_artifact=SimpleNamespace(id=91),
                initial_source=object(),
            )

            result = await svc.approve_match(session, pending.id)

        planner.assert_awaited_once_with(session, acquisition_id=attempt.id)
        runner.dispatch.assert_awaited_once_with(
            attempt.id,
            91,
            initial_source=planner.return_value.initial_source,
        )
        assert result is attempt
        assert pending.status == PendingMatchStatus.APPROVED

    @pytest.mark.asyncio
    async def test_approve_direct_match_keeps_review_pending_when_dispatch_fails(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A dispatch failure must leave the review actionable for a user retry."""
        from pullbox.services.intervention_service import InterventionService

        planner = AsyncMock()
        runner = SimpleNamespace(dispatch=AsyncMock(side_effect=RuntimeError("runner unavailable")))
        svc = InterventionService(
            direct_planner=planner,
            direct_runner_getter=lambda: runner,
        )

        async with db_factory() as session:
            issue = await _seed_issue(session)
            attempt = await _seed_direct_attempt(session, issue.id)
            pending = _make_direct_pending(issue.id, attempt.id)
            session.add(pending)
            await session.commit()
            pending_id = pending.id
            planner.return_value = SimpleNamespace(
                attempt=attempt,
                selected_artifact=SimpleNamespace(id=91),
                initial_source=None,
            )

            with pytest.raises(RuntimeError, match="runner unavailable"):
                await svc.approve_match(session, pending_id)

        async with db_factory() as session:
            persisted = await session.get(PendingMatch, pending_id)
            assert persisted is not None
            assert persisted.status == PendingMatchStatus.PENDING
            assert persisted.resolved_at is None

    @pytest.mark.asyncio
    async def test_approve_direct_match_replans_pre_plan_provider_failure(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A reviewed candidate with no artifact remains safe to plan again."""
        from pullbox.services.intervention_service import InterventionService

        planner = AsyncMock()
        runner = SimpleNamespace(dispatch=AsyncMock(return_value=True))
        svc = InterventionService(
            direct_planner=planner,
            direct_runner_getter=lambda: runner,
        )

        async with db_factory() as session:
            issue = await _seed_issue(session)
            attempt = await _seed_direct_attempt(session, issue.id)
            attempt.failure_code = "source_unavailable"
            pending = _make_direct_pending(issue.id, attempt.id)
            session.add(pending)
            await session.commit()
            planner.return_value = SimpleNamespace(
                attempt=attempt,
                selected_artifact=SimpleNamespace(id=92),
                initial_source=object(),
            )

            result = await svc.approve_match(session, pending.id)

        planner.assert_awaited_once_with(session, acquisition_id=attempt.id)
        runner.dispatch.assert_awaited_once_with(
            attempt.id,
            92,
            initial_source=planner.return_value.initial_source,
        )
        assert result is attempt
        assert pending.status == PendingMatchStatus.APPROVED

    @pytest.mark.asyncio
    async def test_unavailable_direct_candidate_expires_pending_review(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A candidate that cannot resolve no longer blocks later searches."""
        from pullbox.services.direct_acquisition_planner_service import (
            DirectAcquisitionPlanningError,
        )
        from pullbox.services.intervention_service import InterventionService

        planner = AsyncMock(
            side_effect=DirectAcquisitionPlanningError(
                "candidate_not_found",
                "The provider candidate is unavailable.",
                failure_class=DirectArtifactFailureClass.CANDIDATE_INVALID,
                intervention=False,
            )
        )
        svc = InterventionService(
            direct_planner=planner,
            direct_runner_getter=lambda: SimpleNamespace(dispatch=AsyncMock()),
        )

        async with db_factory() as session:
            issue = await _seed_issue(session)
            attempt = await _seed_direct_attempt(session, issue.id)
            pending = _make_direct_pending(issue.id, attempt.id)
            session.add(pending)
            await session.commit()

            with pytest.raises(DirectAcquisitionPlanningError, match="unavailable"):
                await svc.approve_match(session, pending.id)

            await session.refresh(pending)

        assert pending.status == PendingMatchStatus.EXPIRED
        assert pending.resolved_by == "system"
        assert pending.match_details["resolution_reason"] == "candidate_not_found"

    @pytest.mark.asyncio
    async def test_approve_direct_runtime_intervention_resumes_selected_artifact(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Runtime intervention resumes its durable artifact without replanning."""
        from pullbox.services.intervention_service import InterventionService

        planner = AsyncMock()
        runner = SimpleNamespace(dispatch=AsyncMock(return_value=True))
        svc = InterventionService(
            direct_planner=planner,
            direct_runner_getter=lambda: runner,
        )

        async with db_factory() as session:
            issue = await _seed_issue(session)
            attempt = await _seed_direct_attempt(session, issue.id)
            attempt.failure_code = "artifact_host_authentication_required"
            artifact = DirectArtifactAttempt(
                acquisition_attempt_id=attempt.id,
                sequence_no=0,
                artifact_identity="route:runtime-review",
                route_kind=DirectArtifactRouteKind.DIRECT,
                host_kind=DirectArtifactHostKind.PIXELDRAIN,
                state=DirectArtifactState.INTERVENTION,
                is_selected=True,
            )
            session.add(artifact)
            await session.flush()
            pending = _make_direct_pending(issue.id, attempt.id)
            session.add(pending)
            await session.commit()

            result = await svc.approve_match(session, pending.id)

        planner.assert_not_awaited()
        runner.dispatch.assert_awaited_once_with(
            attempt.id,
            artifact.id,
            initial_source=None,
        )
        assert result is attempt
        assert pending.status == PendingMatchStatus.APPROVED

    @pytest.mark.asyncio
    async def test_approve_direct_resource_safety_review_reuses_quarantined_artifact(
        self, db_factory: async_sessionmaker[AsyncSession], tmp_path
    ) -> None:
        """Allow-once approval skips a second transfer and resumes post-processing."""
        from pullbox.core.library_file_ownership import build_file_identity_signature
        from pullbox.services.intervention_service import InterventionService

        retained = tmp_path / "artifact-1.cbz"
        retained.write_bytes(b"retained transfer; archive validation occurs in the worker")

        planner = AsyncMock()
        runner = SimpleNamespace(dispatch=AsyncMock(return_value=True))
        svc = InterventionService(
            direct_planner=planner,
            direct_runner_getter=lambda: runner,
        )

        async with db_factory() as session:
            issue = await _seed_issue(session)
            attempt = await _seed_direct_attempt(session, issue.id)
            attempt.failure_class = DirectArtifactFailureClass.SAFETY
            attempt.failure_code = "artifact_resource_safety_review"
            attempt.plan_snapshot = {
                "safety_review": {
                    "kind": "archive_decompressed_size",
                    "overrideable": True,
                    "source_signature": build_file_identity_signature(retained),
                }
            }
            artifact = DirectArtifactAttempt(
                acquisition_attempt_id=attempt.id,
                sequence_no=0,
                artifact_identity="route:oversized-review",
                route_kind=DirectArtifactRouteKind.DIRECT,
                host_kind=DirectArtifactHostKind.DATANODES,
                state=DirectArtifactState.INTERVENTION,
                is_selected=True,
                quarantine_path=str(retained),
            )
            session.add(artifact)
            await session.flush()
            pending = _make_direct_pending(issue.id, attempt.id)
            pending.match_details = {
                **pending.match_details,
                "failure_code": "artifact_resource_safety_review",
                "safety_block": {
                    "kind": "archive_decompressed_size",
                    "overrideable": True,
                },
            }
            session.add(pending)
            await session.commit()

            result = await svc.approve_match(session, pending.id)

        planner.assert_not_awaited()
        runner.dispatch.assert_awaited_once_with(
            attempt.id,
            artifact.id,
            initial_source=None,
        )
        assert result is attempt
        assert attempt.state is DirectAcquisitionState.POST_PROCESSING
        assert artifact.state is DirectArtifactState.VALIDATING
        assert pending.status == PendingMatchStatus.APPROVED
        assert pending.match_details["safety_override_allowed_once"] is True
        assert attempt.plan_snapshot["safety_review"]["allowed_once"] is True

    @pytest.mark.asyncio
    async def test_approve_direct_hard_safety_failure_does_not_redownload(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Hard safety failures cannot turn approval into another transfer."""
        from pullbox.services.intervention_service import InterventionService

        runner = SimpleNamespace(dispatch=AsyncMock(return_value=True))
        svc = InterventionService(direct_runner_getter=lambda: runner)

        async with db_factory() as session:
            issue = await _seed_issue(session)
            attempt = await _seed_direct_attempt(session, issue.id)
            attempt.failure_class = DirectArtifactFailureClass.SAFETY
            attempt.failure_code = "artifact_safety_rejected"
            artifact = DirectArtifactAttempt(
                acquisition_attempt_id=attempt.id,
                sequence_no=0,
                artifact_identity="route:unsafe-review",
                route_kind=DirectArtifactRouteKind.DIRECT,
                host_kind=DirectArtifactHostKind.DATANODES,
                state=DirectArtifactState.INTERVENTION,
                is_selected=True,
                quarantine_path="/quarantine/artifact-1.cbz",
            )
            session.add(artifact)
            await session.flush()
            pending = _make_direct_pending(issue.id, attempt.id)
            session.add(pending)
            await session.commit()

            with pytest.raises(ValueError, match="cannot be overridden"):
                await svc.approve_match(session, pending.id)

        runner.dispatch.assert_not_awaited()
        assert attempt.state is DirectAcquisitionState.INTERVENTION
        assert artifact.state is DirectArtifactState.INTERVENTION
        assert pending.status == PendingMatchStatus.PENDING

    @pytest.mark.asyncio
    async def test_approve_direct_rejects_mismatched_opaque_locator(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Tampered direct adapter metadata cannot dispatch another attempt."""
        from pullbox.services.intervention_service import InterventionService

        planner = AsyncMock()
        svc = InterventionService(
            direct_planner=planner,
            direct_runner_getter=lambda: SimpleNamespace(dispatch=AsyncMock()),
        )
        async with db_factory() as session:
            issue = await _seed_issue(session)
            attempt = await _seed_direct_attempt(session, issue.id)
            pending = _make_direct_pending(issue.id, attempt.id)
            pending.download_url = "pullbox-direct://attempt/999"
            session.add(pending)
            await session.commit()

            with pytest.raises(ValueError, match="invalid direct attempt reference"):
                await svc.approve_match(session, pending.id)

        planner.assert_not_awaited()


# ── TestRejectMatch ───────────────────────────────────────────────────


class TestRejectMatch:
    """Tests for InterventionService.reject_match."""

    @pytest.mark.asyncio
    async def test_reject_updates_status(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Status changes from PENDING to REJECTED."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            issue = await _seed_issue(session)
            pm = _make_pending(issue.id)
            session.add(pm)
            await session.commit()
            pm_id = pm.id

        async with db_factory() as session:
            await svc.reject_match(session, pm_id)
            await session.commit()

        async with db_factory() as session:
            result = await session.get(PendingMatch, pm_id)
            assert result is not None
            assert result.status == PendingMatchStatus.REJECTED
            assert result.resolved_at is not None
            assert result.resolved_by == "user"

    @pytest.mark.asyncio
    async def test_reject_stores_reason(self, db_factory: async_sessionmaker[AsyncSession]) -> None:
        """Optional reason string is stored in match_details."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            issue = await _seed_issue(session)
            pm = _make_pending(issue.id)
            session.add(pm)
            await session.commit()
            pm_id = pm.id

        async with db_factory() as session:
            await svc.reject_match(session, pm_id, reason="Wrong volume")
            await session.commit()

        async with db_factory() as session:
            result = await session.get(PendingMatch, pm_id)
            assert result is not None
            assert result.match_details.get("rejection_reason") == "Wrong volume"

    @pytest.mark.asyncio
    async def test_reject_non_pending_raises(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Cannot reject non-PENDING match."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            issue = await _seed_issue(session)
            pm = _make_pending(issue.id, status=PendingMatchStatus.REJECTED)
            session.add(pm)
            await session.commit()
            pm_id = pm.id

        async with db_factory() as session:
            with pytest.raises(ValueError, match=r"not.*pending"):
                await svc.reject_match(session, pm_id)

    @pytest.mark.asyncio
    async def test_reject_direct_match_cancels_attempt(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Rejecting direct review cancels its durable acquisition attempt."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()
        async with db_factory() as session:
            issue = await _seed_issue(session)
            attempt = await _seed_direct_attempt(session, issue.id)
            pending = _make_direct_pending(issue.id, attempt.id)
            session.add(pending)
            await session.commit()

            blocklisted = await svc.reject_match(session, pending.id, reason="Wrong edition")
            await session.commit()

        assert pending.status == PendingMatchStatus.REJECTED
        assert blocklisted is True
        assert attempt.state is DirectAcquisitionState.CANCELLED
        assert attempt.failure_code == "user_rejected"

    @pytest.mark.asyncio
    async def test_reject_direct_match_preserves_failed_attempt(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A stale review can be dismissed after its acquisition already failed."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()
        async with db_factory() as session:
            issue = await _seed_issue(session)
            attempt = await _seed_direct_attempt(session, issue.id)
            attempt.state = DirectAcquisitionState.FAILED
            attempt.failure_class = DirectArtifactFailureClass.PERMANENT_MIRROR
            attempt.failure_code = "artifact_unavailable"
            attempt.error_message = "The source is no longer available."
            pending = _make_direct_pending(issue.id, attempt.id)
            session.add(pending)
            await session.commit()

            blocklisted = await svc.reject_match(session, pending.id)
            await session.commit()

        assert pending.status == PendingMatchStatus.REJECTED
        assert blocklisted is False
        assert attempt.state is DirectAcquisitionState.FAILED
        assert attempt.failure_class is DirectArtifactFailureClass.PERMANENT_MIRROR
        assert attempt.failure_code == "artifact_unavailable"
        assert attempt.error_message == "The source is no longer available."
        async with db_factory() as session:
            result = await session.execute(select(BlocklistEntry))
            assert result.scalar_one_or_none() is None


# ── TestGetPending ────────────────────────────────────────────────────


class TestGetPending:
    """Tests for InterventionService.get_pending."""

    @pytest.mark.asyncio
    async def test_returns_pending_only_by_default(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Default filter returns only PENDING status."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            issue = await _seed_issue(session)
            session.add(_make_pending(issue.id, url="https://a.com/1"))
            session.add(
                _make_pending(
                    issue.id,
                    url="https://a.com/2",
                    status=PendingMatchStatus.APPROVED,
                )
            )
            session.add(
                _make_pending(
                    issue.id,
                    url="https://a.com/3",
                    status=PendingMatchStatus.REJECTED,
                )
            )
            await session.commit()

        async with db_factory() as session:
            items, total = await svc.get_pending(session)
            assert total == 1
            assert len(items) == 1
            assert items[0].status == PendingMatchStatus.PENDING

    @pytest.mark.asyncio
    async def test_filter_by_series(self, db_factory: async_sessionmaker[AsyncSession]) -> None:
        """series_id filter works."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            issue1 = await _seed_issue(session, series_title="Batman")
            series1_id = issue1.series_id

            series2 = _make_series(title="Superman", comicvine_id=99901)
            session.add(series2)
            await session.flush()
            issue2 = _make_issue(series2.id, issue_number=5.0)
            session.add(issue2)
            await session.flush()

            session.add(_make_pending(issue1.id, url="https://a.com/1"))
            session.add(_make_pending(issue2.id, url="https://a.com/2"))
            await session.commit()

        async with db_factory() as session:
            items, total = await svc.get_pending(session, series_id=series1_id)
            assert total == 1
            assert items[0].issue.series_id == series1_id

    @pytest.mark.asyncio
    async def test_filter_by_confidence(self, db_factory: async_sessionmaker[AsyncSession]) -> None:
        """confidence filter works."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            issue = await _seed_issue(session)
            session.add(_make_pending(issue.id, url="https://a.com/1", confidence="medium"))
            session.add(_make_pending(issue.id, url="https://a.com/2", confidence="low"))
            await session.commit()

        async with db_factory() as session:
            items, total = await svc.get_pending(session, confidence="low")
            assert total == 1
            assert items[0].confidence == "low"

    @pytest.mark.asyncio
    async def test_pagination(self, db_factory: async_sessionmaker[AsyncSession]) -> None:
        """Returns correct page and total_count."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            issue = await _seed_issue(session)
            for i in range(5):
                session.add(_make_pending(issue.id, url=f"https://a.com/{i}"))
            await session.commit()

        async with db_factory() as session:
            items, total = await svc.get_pending(session, page=1, per_page=2)
            assert total == 5
            assert len(items) == 2

        async with db_factory() as session:
            items, total = await svc.get_pending(session, page=3, per_page=2)
            assert total == 5
            assert len(items) == 1  # page 3: items 5 of 5


# ── TestExpireStale ───────────────────────────────────────────────────


class TestExpireStale:
    """Tests for InterventionService.expire_stale."""

    @pytest.mark.asyncio
    async def test_expires_old_pending(self, db_factory: async_sessionmaker[AsyncSession]) -> None:
        """Pending matches older than threshold get EXPIRED."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            issue = await _seed_issue(session)
            old = _make_pending(
                issue.id,
                url="https://a.com/old",
                created_at=datetime.now(UTC) - timedelta(days=31),
            )
            session.add(old)
            await session.commit()
            old_id = old.id

        async with db_factory() as session:
            count = await svc.expire_stale(session, max_age_days=30)
            await session.commit()

        assert count == 1

        async with db_factory() as session:
            result = await session.get(PendingMatch, old_id)
            assert result is not None
            assert result.status == PendingMatchStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_does_not_expire_recent(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Recent pending matches are not expired."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            issue = await _seed_issue(session)
            recent = _make_pending(issue.id, url="https://a.com/recent")
            session.add(recent)
            await session.commit()

        async with db_factory() as session:
            count = await svc.expire_stale(session, max_age_days=30)
            await session.commit()

        assert count == 0

    @pytest.mark.asyncio
    async def test_does_not_expire_approved(
        self, db_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Already-approved matches are not expired."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            issue = await _seed_issue(session)
            approved = _make_pending(
                issue.id,
                url="https://a.com/approved",
                status=PendingMatchStatus.APPROVED,
                created_at=datetime.now(UTC) - timedelta(days=60),
            )
            session.add(approved)
            await session.commit()

        async with db_factory() as session:
            count = await svc.expire_stale(session, max_age_days=30)
            await session.commit()

        assert count == 0

    @pytest.mark.asyncio
    async def test_returns_count(self, db_factory: async_sessionmaker[AsyncSession]) -> None:
        """Returns the number of expired entries."""
        from pullbox.services.intervention_service import InterventionService

        svc = InterventionService()

        async with db_factory() as session:
            issue = await _seed_issue(session)
            for i in range(3):
                session.add(
                    _make_pending(
                        issue.id,
                        url=f"https://a.com/old{i}",
                        created_at=datetime.now(UTC) - timedelta(days=45),
                    )
                )
            # One recent one should NOT be expired
            session.add(_make_pending(issue.id, url="https://a.com/recent"))
            await session.commit()

        async with db_factory() as session:
            count = await svc.expire_stale(session, max_age_days=30)
            await session.commit()

        assert count == 3
