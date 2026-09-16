"""Intervention queue service — manages pending matches for user review.

When a search finds a release with medium or low confidence, it is
stored as a PendingMatch for the user to approve or reject via the
intervention queue UI. This service handles all CRUD and lifecycle
operations for pending matches.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactAttempt,
    DirectArtifactFailureClass,
    DirectArtifactState,
)
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.services.direct_acquisition_planner_service import (
    DirectAcquisitionPlanningError,
    DirectAcquisitionPlanningResult,
    plan_direct_acquisition,
)
from pullbox.services.direct_acquisition_state import (
    advance_acquisition_progress,
    transition_acquisition,
    transition_artifact,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.download import DownloadHistory
    from pullbox.providers.artifact_hosts.contract import HostResolutionRequest
    from pullbox.providers.base import ReleaseResult
    from pullbox.services.airdcpp_search_types import DcValidatedCandidate
    from pullbox.services.direct_search_coordinator import DirectValidatedCandidate
    from pullbox.services.download_service import DownloadService
    from pullbox.services.release_validator import ValidationResult

logger = structlog.get_logger(__name__)


class DirectRunnerLike(Protocol):
    async def dispatch(
        self,
        acquisition_id: int,
        artifact_id: int,
        *,
        initial_source: HostResolutionRequest | None = None,
    ) -> bool: ...


DirectPlanner = Callable[..., Awaitable[DirectAcquisitionPlanningResult]]
DirectRunnerGetter = Callable[[], DirectRunnerLike]


def _direct_candidate_match_details(snapshot: dict[str, object]) -> dict[str, object]:
    """Extract redacted parsed evidence from a durable direct candidate snapshot."""
    parsed = snapshot.get("parsed")
    if not isinstance(parsed, dict):
        return {}

    details: dict[str, object] = {}
    series_title = parsed.get("series_title")
    if isinstance(series_title, str) and series_title.strip():
        details["parsed_series"] = series_title.strip()

    issue_numbers = parsed.get("issue_numbers")
    if isinstance(issue_numbers, list) and issue_numbers:
        issue_number = issue_numbers[0]
        if isinstance(issue_number, str | int | float) and not isinstance(issue_number, bool):
            details["parsed_issue"] = issue_number

    year = parsed.get("year")
    if isinstance(year, int) and not isinstance(year, bool):
        details["parsed_year"] = year
    return details


async def _direct_artifact_host_kind(
    session: AsyncSession,
    attempt_id: int,
) -> str | None:
    """Return the selected artifact host, falling back to the latest attempted route."""
    result = await session.execute(
        select(DirectArtifactAttempt.host_kind)
        .where(DirectArtifactAttempt.acquisition_attempt_id == attempt_id)
        .order_by(
            DirectArtifactAttempt.is_selected.desc(),
            DirectArtifactAttempt.sequence_no.desc(),
        )
        .limit(1)
    )
    host_kind = result.scalar_one_or_none()
    return host_kind.value if host_kind is not None else None


class InterventionService:
    """Manages the intervention queue for ambiguous search matches."""

    def __init__(
        self,
        download_service: DownloadService | None = None,
        *,
        direct_planner: DirectPlanner = plan_direct_acquisition,
        direct_runner_getter: DirectRunnerGetter | None = None,
    ) -> None:
        self._download_service = download_service
        self._direct_planner = direct_planner
        self._direct_runner_getter = direct_runner_getter

    async def create_pending_match(
        self,
        session: AsyncSession,
        issue_id: int,
        release: ReleaseResult,
        validation: ValidationResult,
        indexer_id: int | None = None,
    ) -> PendingMatch | None:
        """Create a pending match if one doesn't already exist for this issue+URL.

        Returns None if a duplicate already exists.
        """
        # Check for existing pending match with same issue+URL
        existing = await session.execute(
            select(PendingMatch).where(
                PendingMatch.issue_id == issue_id,
                PendingMatch.download_url == release.download_url,
            )
        )
        if existing.scalar_one_or_none() is not None:
            logger.debug(
                "pending_match_duplicate_skipped",
                issue_id=issue_id,
                url=release.download_url,
            )
            return None

        match_details: dict[str, object] = {
            "parsed_series": getattr(validation.parsed, "series_name", None),
            "parsed_issue": getattr(validation.parsed, "issue_number", None),
            "parsed_year": getattr(validation.parsed, "year", None),
            "parsed_type": getattr(getattr(validation.parsed, "issue_type", None), "value", None),
            "series_similarity": validation.series_similarity,
            "series_match_type": ("exact" if validation.series_similarity >= 0.95 else "fuzzy"),
            "issue_match": validation.issue_match,
            "year_match": validation.year_match,
            "type_match": validation.issue_type_match,
            "rejection_flags": [],
            "size_warning": None,
            "indexer_name": release.indexer_name,
            "age_days": release.age_days,
            "seeders": release.seeders,
            "leechers": release.leechers,
            "info_url": release.info_url,
        }

        pm = PendingMatch(
            issue_id=issue_id,
            release_title=release.title,
            download_url=release.download_url,
            indexer_id=indexer_id if indexer_id is not None else release.indexer_id,
            is_torrent=release.is_torrent,
            file_size=release.size_bytes,
            confidence=validation.confidence.value,
            match_details=match_details,
        )
        session.add(pm)
        await session.flush()

        logger.info(
            "pending_match_created",
            pending_id=pm.id,
            issue_id=issue_id,
            confidence=validation.confidence.value,
            release_title=release.title,
        )
        return pm

    async def create_dc_pending_match(
        self,
        session: AsyncSession,
        issue_id: int,
        result: DcValidatedCandidate,
        search_log_id: int,
    ) -> PendingMatch | None:
        """Use the shared review UI while preserving the exact client/file route."""
        from pullbox.services.airdcpp_search_acquisition import dc_review_snapshot

        pending = await self.create_pending_match(
            session, issue_id, result.release, result.validation
        )
        if pending is not None:
            pending.match_details = {
                **pending.match_details,
                "source_kind": "dc",
                "dc_route_snapshot": dc_review_snapshot(
                    result, issue_id=issue_id, search_log_id=search_log_id
                ),
            }
            await session.flush()
        return pending

    async def create_direct_pending_match(
        self,
        session: AsyncSession,
        issue_id: int,
        attempt_id: int,
        result: DirectValidatedCandidate,
    ) -> PendingMatch | None:
        """Adapt one direct candidate into the existing intervention contract."""
        locator = f"pullbox-direct://attempt/{attempt_id}"
        existing = await session.execute(
            select(PendingMatch).where(
                PendingMatch.issue_id == issue_id,
                PendingMatch.download_url == locator,
            )
        )
        if existing.scalar_one_or_none() is not None:
            logger.debug(
                "direct_pending_match_duplicate_skipped",
                issue_id=issue_id,
                attempt_id=attempt_id,
            )
            return None

        validation = result.validation
        parsed = result.candidate.parsed
        pending_match = PendingMatch(
            issue_id=issue_id,
            release_title=result.candidate.display_title,
            download_url=locator,
            indexer_id=None,
            is_torrent=False,
            file_size=None,
            confidence=validation.confidence.value,
            match_details={
                "source_kind": "direct",
                "direct_attempt_id": attempt_id,
                "provider_identity": result.provider.provider_identity,
                "provider_name": result.provider.display_name,
                "parsed_series": parsed.series_title,
                "parsed_issue": parsed.issue_numbers[0] if parsed.issue_numbers else None,
                "parsed_year": parsed.year,
                "parsed_type": parsed.edition,
                "format": parsed.format,
                "quality": parsed.quality,
                "provider_confidence": result.candidate.provider_confidence,
                "series_similarity": validation.series_similarity,
                "series_match_type": ("exact" if validation.series_similarity >= 0.95 else "fuzzy"),
                "issue_match": validation.issue_match,
                "year_match": validation.year_match,
                "type_match": validation.issue_type_match,
                "rejection_flags": [],
                "size_warning": None,
            },
        )
        session.add(pending_match)
        await session.flush()
        logger.info(
            "direct_pending_match_created",
            pending_id=pending_match.id,
            issue_id=issue_id,
            attempt_id=attempt_id,
            confidence=validation.confidence.value,
        )
        return pending_match

    async def create_direct_attempt_intervention(
        self,
        session: AsyncSession,
        attempt: DirectAcquisitionAttempt,
    ) -> PendingMatch:
        """Create or reopen an intervention row for a failed direct attempt."""
        locator = f"pullbox-direct://attempt/{attempt.id}"
        pending_match = (
            await session.execute(
                select(PendingMatch).where(
                    PendingMatch.issue_id == attempt.issue_id,
                    PendingMatch.download_url == locator,
                )
            )
        ).scalar_one_or_none()
        snapshot = attempt.candidate_snapshot or {}
        semantic = snapshot.get("semantic_decision")
        semantic_details = semantic if isinstance(semantic, dict) else {}
        details = dict(pending_match.match_details or {}) if pending_match is not None else {}
        details.update(_direct_candidate_match_details(snapshot))
        artifact_host_kind = await _direct_artifact_host_kind(session, attempt.id)
        details.update(
            {
                "source_kind": "direct",
                "direct_attempt_id": attempt.id,
                "provider_identity": attempt.provider_identity,
                "provider_name": attempt.provider_identity,
                "series_similarity": semantic_details.get("series_similarity"),
                "series_match_type": semantic_details.get("match_type") or "exact",
                "failure_class": (
                    attempt.failure_class.value if attempt.failure_class is not None else None
                ),
                "failure_code": attempt.failure_code,
            }
        )
        if artifact_host_kind is not None:
            details["artifact_host_kind"] = artifact_host_kind
        safety_review = (attempt.plan_snapshot or {}).get("safety_review")
        if isinstance(safety_review, dict):
            details["safety_block"] = dict(safety_review)
        if pending_match is None:
            pending_match = PendingMatch(
                issue_id=attempt.issue_id,
                release_title=str(snapshot.get("display_title") or attempt.provider_candidate_id),
                download_url=locator,
                indexer_id=None,
                is_torrent=False,
                file_size=None,
                confidence=str(semantic_details.get("confidence") or "low"),
                match_details=details,
            )
            session.add(pending_match)
        else:
            pending_match.status = PendingMatchStatus.PENDING
            pending_match.resolved_at = None
            pending_match.resolved_by = None
            pending_match.match_details = details
        await session.flush()
        logger.info(
            "direct_attempt_intervention_created",
            pending_id=pending_match.id,
            issue_id=attempt.issue_id,
            attempt_id=attempt.id,
            failure_code=attempt.failure_code,
        )
        return pending_match

    async def approve_match(
        self,
        session: AsyncSession,
        pending_id: int,
    ) -> DownloadHistory | DirectAcquisitionAttempt:
        """Approve a pending match and send it to the download client.

        Raises:
            ValueError: If pending match not found or not in PENDING status.
        """
        pm = await session.get(PendingMatch, pending_id)
        if pm is None:
            raise ValueError(f"Pending match {pending_id} not found")
        if pm.status != PendingMatchStatus.PENDING:
            raise ValueError(f"Pending match {pending_id} is not pending (status={pm.status})")

        if pm.match_details.get("source_kind") == "dc":
            from pullbox.services.airdcpp_search_acquisition import (
                acquire_dc_candidate,
                dc_review_candidate,
            )

            candidate, search_log_id = dc_review_candidate(pm)
            download, _created = await acquire_dc_candidate(
                session,
                candidate=candidate,
                issue_id=pm.issue_id,
                search_log_id=search_log_id,
                request_key=f"dc-review:{pm.id}",
                automatic=False,
            )
            pm.status = PendingMatchStatus.APPROVED
            pm.resolved_at = datetime.now(UTC)
            pm.resolved_by = "user"
            return download

        direct_attempt_id = _direct_attempt_id(pm)
        if direct_attempt_id is not None:
            return await self._approve_direct_match(session, pm, direct_attempt_id)

        if self._download_service is None:
            raise RuntimeError("No download service configured")

        # Reconstruct ReleaseResult from stored data
        from pullbox.providers.base import ReleaseResult

        release = ReleaseResult(
            title=pm.release_title,
            indexer_name=pm.match_details.get("indexer_name", "unknown"),
            download_url=pm.download_url,
            size_bytes=pm.file_size,
            age_days=pm.match_details.get("age_days"),
            seeders=pm.match_details.get("seeders"),
            leechers=pm.match_details.get("leechers"),
            grabs=None,
            is_torrent=pm.is_torrent,
            category=None,
            published_at=None,
            indexer_id=pm.indexer_id,
        )

        download = await self._download_service.send_to_client(
            session, release, pm.issue_id, pm.indexer_id
        )

        pm.status = PendingMatchStatus.APPROVED
        pm.resolved_at = datetime.now(UTC)
        pm.resolved_by = "user"

        logger.info(
            "pending_match_approved",
            pending_id=pending_id,
            issue_id=pm.issue_id,
            release_title=pm.release_title,
        )
        return download

    async def _approve_direct_match(
        self,
        session: AsyncSession,
        pending_match: PendingMatch,
        attempt_id: int,
    ) -> DirectAcquisitionAttempt:
        attempt = (
            await session.execute(
                select(DirectAcquisitionAttempt)
                .options(selectinload(DirectAcquisitionAttempt.artifact_attempts))
                .where(DirectAcquisitionAttempt.id == attempt_id)
            )
        ).scalar_one_or_none()
        if attempt is None or attempt.issue_id != pending_match.issue_id:
            raise ValueError("Pending match has an invalid direct attempt reference")

        initial_source = None
        if not attempt.artifact_attempts:
            try:
                planned = await self._direct_planner(session, acquisition_id=attempt_id)
            except DirectAcquisitionPlanningError as exc:
                if exc.code == "candidate_not_found":
                    pending_match.status = PendingMatchStatus.EXPIRED
                    pending_match.resolved_at = datetime.now(UTC)
                    pending_match.resolved_by = "system"
                    details = dict(pending_match.match_details or {})
                    details["resolution_reason"] = exc.code
                    pending_match.match_details = details
                    await session.commit()
                raise
            attempt = planned.attempt
            artifact = planned.selected_artifact
            initial_source = planned.initial_source
        else:
            selected = [artifact for artifact in attempt.artifact_attempts if artifact.is_selected]
            if len(selected) != 1:
                raise ValueError("Direct attempt does not have one selected artifact to resume")
            artifact = selected[0]

        safety_review = (attempt.plan_snapshot or {}).get("safety_review")
        is_safety_intervention = (
            DirectAcquisitionState(attempt.state) is DirectAcquisitionState.INTERVENTION
            and attempt.failure_class is DirectArtifactFailureClass.SAFETY
        )
        can_override_safety_once = (
            is_safety_intervention
            and attempt.failure_code == "artifact_resource_safety_review"
            and isinstance(safety_review, dict)
            and safety_review.get("overrideable") is True
            and artifact.quarantine_path
        )
        if can_override_safety_once:
            assert isinstance(safety_review, dict)
            transition_acquisition(attempt, DirectAcquisitionState.POST_PROCESSING)
            transition_artifact(artifact, DirectArtifactState.VALIDATING)
            plan_snapshot = dict(attempt.plan_snapshot or {})
            approved_review = dict(safety_review)
            approved_review["allowed_once"] = True
            plan_snapshot["safety_review"] = approved_review
            attempt.plan_snapshot = plan_snapshot
            details = dict(pending_match.match_details or {})
            details["safety_override_allowed_once"] = True
            pending_match.match_details = details
        elif is_safety_intervention:
            raise ValueError("This direct artifact safety failure cannot be overridden")

        # Persist the planned attempt before the runner opens its own session,
        # but keep the user review pending until dispatch has accepted it.
        await session.commit()

        runner_getter = self._direct_runner_getter or _get_direct_runner
        runner = runner_getter()
        dispatched = await runner.dispatch(
            attempt.id,
            artifact.id,
            initial_source=initial_source,
        )
        if not dispatched:
            raise RuntimeError("Direct acquisition dispatch was not accepted")
        pending_match.status = PendingMatchStatus.APPROVED
        pending_match.resolved_at = datetime.now(UTC)
        pending_match.resolved_by = "user"
        await session.commit()
        logger.info(
            "direct_pending_match_approved",
            pending_id=pending_match.id,
            issue_id=pending_match.issue_id,
            attempt_id=attempt.id,
        )
        return attempt

    async def retry_direct_recovery(
        self,
        session: AsyncSession,
        pending_id: int,
    ) -> DirectAcquisitionAttempt:
        """Resolve a failed direct candidate again without replaying its stale artifact."""
        pending_match = await session.get(PendingMatch, pending_id)
        if pending_match is None or pending_match.status != PendingMatchStatus.PENDING:
            raise ValueError("Direct recovery item is not pending")
        attempt_id = _direct_attempt_id(pending_match)
        if attempt_id is None or not (pending_match.match_details or {}).get("failure_class"):
            raise ValueError("Pending match is not a direct acquisition recovery")

        prior = await session.get(DirectAcquisitionAttempt, attempt_id)
        if prior is None or prior.issue_id != pending_match.issue_id:
            raise ValueError("Pending match has an invalid direct attempt reference")

        fresh = DirectAcquisitionAttempt(
            request_key=f"direct-recovery:{uuid4().hex}",
            issue_id=prior.issue_id,
            search_log_id=prior.search_log_id,
            provider_config_id=prior.provider_config_id,
            provider_identity=prior.provider_identity,
            provider_candidate_id=prior.provider_candidate_id,
            state=DirectAcquisitionState.DISCOVERED,
            requested_coverage=dict(prior.requested_coverage or {}),
            candidate_snapshot=dict(prior.candidate_snapshot or {}),
            progress_snapshot={"schema_version": 1, "stage": "discovered", "recovery_of": prior.id},
            max_retries=prior.max_retries,
            replace_existing_file=prior.replace_existing_file,
        )
        session.add(fresh)
        await session.flush()
        try:
            planned = await self._direct_planner(session, acquisition_id=fresh.id)
        except Exception:
            await session.delete(fresh)
            await session.flush()
            raise

        # Persist the fresh plan before dispatch while retaining the recovery
        # row when the runner rejects or cannot start the attempt.
        await session.commit()

        runner_getter = self._direct_runner_getter or _get_direct_runner
        dispatched = await runner_getter().dispatch(
            planned.attempt.id,
            planned.selected_artifact.id,
            initial_source=planned.initial_source,
        )
        if not dispatched:
            raise RuntimeError("Direct acquisition dispatch was not accepted")
        details = dict(pending_match.match_details or {})
        details["resolution_reason"] = "recovery_replanned"
        details["recovery_attempt_id"] = fresh.id
        pending_match.match_details = details
        pending_match.status = PendingMatchStatus.APPROVED
        pending_match.resolved_at = datetime.now(UTC)
        pending_match.resolved_by = "user"
        await session.commit()
        logger.info(
            "direct_acquisition_recovery_replanned",
            pending_id=pending_id,
            issue_id=pending_match.issue_id,
            prior_attempt_id=prior.id,
            fresh_attempt_id=fresh.id,
        )
        return planned.attempt

    async def reject_match(
        self,
        session: AsyncSession,
        pending_id: int,
        reason: str | None = None,
    ) -> bool:
        """Reject a pending match.

        Returns whether the rejected release is now covered by the blocklist.

        Raises:
            ValueError: If pending match not found or not in PENDING status.
        """
        pm = await session.get(PendingMatch, pending_id)
        if pm is None:
            raise ValueError(f"Pending match {pending_id} not found")
        if pm.status != PendingMatchStatus.PENDING:
            raise ValueError(f"Pending match {pending_id} is not pending (status={pm.status})")

        direct_attempt_id = _direct_attempt_id(pm)
        should_blocklist_release = True
        if direct_attempt_id is not None:
            attempt = await session.get(DirectAcquisitionAttempt, direct_attempt_id)
            if attempt is None or attempt.issue_id != pm.issue_id:
                raise ValueError("Pending match has an invalid direct attempt reference")
            attempt_state = DirectAcquisitionState(attempt.state)
            if attempt_state in {
                DirectAcquisitionState.COMPLETED,
                DirectAcquisitionState.CANCELLED,
                DirectAcquisitionState.FAILED,
            }:
                should_blocklist_release = False
            else:
                transition_acquisition(attempt, DirectAcquisitionState.CANCELLED)
                attempt.failure_class = DirectArtifactFailureClass.USER_ACTION
                attempt.failure_code = "user_rejected"
                attempt.error_message = "The direct result was rejected by the user."
                advance_acquisition_progress(
                    attempt,
                    revision=attempt.progress_revision + 1,
                    snapshot={
                        "schema_version": 1,
                        "stage": "cancelled",
                        "failure_code": "user_rejected",
                    },
                )

        pm.status = PendingMatchStatus.REJECTED
        pm.resolved_at = datetime.now(UTC)
        pm.resolved_by = "user"

        if reason:
            details = dict(pm.match_details) if pm.match_details else {}
            details["rejection_reason"] = reason
            pm.match_details = details

        logger.info(
            "pending_match_rejected",
            pending_id=pending_id,
            issue_id=pm.issue_id,
            reason=reason,
        )

        blocklist_applied = False
        if should_blocklist_release:
            try:
                from pullbox.core.release_parser import parse_release_title
                from pullbox.models.blocklist import BlocklistReason
                from pullbox.services.blocklist_service import BlocklistService

                parsed = parse_release_title(pm.release_title)
                release_group = parsed.scan_group if parsed else None

                await BlocklistService.add_entry(
                    session,
                    pm.release_title,
                    BlocklistReason.REJECTED,
                    download_url=None if direct_attempt_id is not None else pm.download_url,
                    issue_id=pm.issue_id,
                    indexer_id=pm.indexer_id,
                    release_group=release_group,
                )
                blocklist_applied = True
            except Exception:
                logger.debug(
                    "blocklist_reject_add_failed",
                    pending_id=pending_id,
                )
        return blocklist_applied

    async def get_pending(
        self,
        session: AsyncSession,
        *,
        page: int = 1,
        per_page: int = 20,
        status: PendingMatchStatus | None = PendingMatchStatus.PENDING,
        series_id: int | None = None,
        confidence: str | None = None,
    ) -> tuple[list[PendingMatch], int]:
        """Get paginated pending matches with optional filters.

        Returns (items, total_count).
        """
        from pullbox.models.issue import Issue

        query = select(PendingMatch)
        count_query = select(func.count(PendingMatch.id))

        if status is not None:
            query = query.where(PendingMatch.status == status)
            count_query = count_query.where(PendingMatch.status == status)

        if series_id is not None:
            query = query.join(Issue, PendingMatch.issue_id == Issue.id).where(
                Issue.series_id == series_id
            )
            count_query = count_query.join(Issue, PendingMatch.issue_id == Issue.id).where(
                Issue.series_id == series_id
            )

        if confidence is not None:
            query = query.where(PendingMatch.confidence == confidence)
            count_query = count_query.where(PendingMatch.confidence == confidence)

        total = (await session.execute(count_query)).scalar_one()

        query = query.order_by(PendingMatch.created_at.desc())
        query = query.offset((page - 1) * per_page).limit(per_page)

        result = await session.execute(query)
        items = list(result.scalars().all())

        return items, total

    async def get_pending_count(self, session: AsyncSession) -> int:
        """Count pending matches (PENDING status only)."""
        result = await session.execute(
            select(func.count(PendingMatch.id)).where(
                PendingMatch.status == PendingMatchStatus.PENDING
            )
        )
        return result.scalar_one()

    async def expire_stale(
        self,
        session: AsyncSession,
        max_age_days: int = 30,
    ) -> int:
        """Expire PENDING matches older than max_age_days. Returns count expired."""
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)

        # Count first, then update — avoids mypy issues with CursorResult.rowcount
        stale = await session.execute(
            select(PendingMatch.id).where(
                PendingMatch.status == PendingMatchStatus.PENDING,
                PendingMatch.created_at < cutoff,
            )
        )
        stale_ids = [row[0] for row in stale.all()]

        if stale_ids:
            await session.execute(
                update(PendingMatch)
                .where(PendingMatch.id.in_(stale_ids))
                .values(
                    status=PendingMatchStatus.EXPIRED,
                    resolved_at=datetime.now(UTC),
                    resolved_by="expiry",
                )
            )
        count = len(stale_ids)

        if count:
            logger.info("pending_matches_expired", count=count, max_age_days=max_age_days)

        return count

    async def has_pending_for_issue(
        self,
        session: AsyncSession,
        issue_id: int,
    ) -> bool:
        """Check if issue already has a PENDING match."""
        result = await session.execute(
            select(func.count(PendingMatch.id)).where(
                PendingMatch.issue_id == issue_id,
                PendingMatch.status == PendingMatchStatus.PENDING,
            )
        )
        return result.scalar_one() > 0


def _direct_attempt_id(pending_match: PendingMatch) -> int | None:
    details = pending_match.match_details or {}
    if details.get("source_kind") != "direct":
        return None
    attempt_id = details.get("direct_attempt_id")
    if isinstance(attempt_id, bool) or not isinstance(attempt_id, int) or attempt_id <= 0:
        raise ValueError("Pending match has an invalid direct attempt reference")
    if pending_match.download_url != f"pullbox-direct://attempt/{attempt_id}":
        raise ValueError("Pending match has an invalid direct attempt reference")
    return attempt_id


def is_direct_pending_match(pending_match: PendingMatch) -> bool:
    """Return whether a PendingMatch is the explicit direct-acquisition adapter."""
    return (pending_match.match_details or {}).get("source_kind") == "direct"


def _get_direct_runner() -> DirectRunnerLike:
    from pullbox.tasks.direct_acquisition_task import get_direct_acquisition_runner

    return get_direct_acquisition_runner()
