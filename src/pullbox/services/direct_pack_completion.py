"""Reconcile redundant pack requests without changing their library files."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from pullbox.core.archive_metadata import MAX_METADATA_BYTES, read_archive_metadata
from pullbox.models import Issue, LibraryFile
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactAttempt,
    DirectArtifactFailureClass,
    DirectArtifactState,
)
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.issue import IssueStatus
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.services.archive_metadata_binding import (
    lock_archive_metadata_binding,
    revalidate_archive_metadata_target,
)
from pullbox.services.archive_metadata_reconciliation import reconcile_archive_metadata
from pullbox.services.direct_acquisition_state import (
    advance_acquisition_progress,
    transition_acquisition,
    transition_artifact,
)
from pullbox.services.direct_artifact_pack import PACK_FULFILLED_MESSAGE
from pullbox.services.direct_artifact_quarantine import (
    DirectArtifactValidationError,
    validate_direct_artifact,
)
from pullbox.services.issue_file_metadata import prepare_file_metadata
from pullbox.services.library_mutation_coordination import lock_file_mutation_admission
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


async def reconcile_completed_pack_issues(
    session: AsyncSession, issue_ids: tuple[int, ...]
) -> None:
    """Bound follow-up to ownership failures for this pack's imported members."""
    ids = list(
        await session.scalars(
            select(DirectAcquisitionAttempt.id)
            .where(
                DirectAcquisitionAttempt.issue_id.in_(issue_ids),
                DirectAcquisitionAttempt.state == DirectAcquisitionState.INTERVENTION,
                DirectAcquisitionAttempt.failure_code.in_(
                    (
                        "direct_pack_ownership_changed",
                        "direct_metadata_review_required",
                    )
                ),
            )
            .order_by(DirectAcquisitionAttempt.id)
            .limit(100)
        )
    )
    await session.commit()
    for attempt_id in ids:
        await reconcile_fulfilled_pack(session, attempt_id)
        await session.commit()


def is_pack_ownership_failure(attempt: DirectAcquisitionAttempt) -> bool:
    """Recognize the old misclassified guard, never arbitrary metadata conflicts."""
    return attempt.failure_code == "direct_pack_ownership_changed" or (
        attempt.failure_code == "direct_metadata_review_required"
        and attempt.error_message == "Pack metadata or ownership changed; review before retrying."
    )


@dataclass(frozen=True)
class _Owner:
    issue_id: int
    file_id: int
    artifact_id: int
    history_id: int
    pending_ids: tuple[int, ...]
    snapshot: str


async def _read_owner(session: AsyncSession, attempt_id: int) -> _Owner | None:
    attempt = await session.get(DirectAcquisitionAttempt, attempt_id, populate_existing=True)
    if (
        attempt is None
        or attempt.state is not DirectAcquisitionState.INTERVENTION
        or attempt.failure_class is not DirectArtifactFailureClass.POST_PROCESS
        or attempt.replace_existing_file
        or not is_pack_ownership_failure(attempt)
        or (attempt.plan_snapshot or {}).get("paired_metadata_copy") is not None
    ):
        return None
    coverage = (attempt.plan_snapshot or {}).get("coverage")
    numbers = coverage.get("selected_content_issue_numbers") if isinstance(coverage, dict) else None
    issue = await session.get(Issue, attempt.issue_id, populate_existing=True)
    if (
        issue is None
        or issue.status is not IssueStatus.OWNED
        or not isinstance(numbers, list)
        or len(numbers) <= 1
        or issue.issue_number_text not in numbers
    ):
        return None
    artifacts = list(
        await session.scalars(
            select(DirectArtifactAttempt)
            .where(
                DirectArtifactAttempt.acquisition_attempt_id == attempt_id,
                DirectArtifactAttempt.is_selected.is_(True),
            )
            .limit(2)
            .execution_options(populate_existing=True)
        )
    )
    files = list(
        await session.scalars(
            select(LibraryFile)
            .where(
                LibraryFile.issue_id == issue.id,
            )
            .limit(2)
            .execution_options(populate_existing=True)
        )
    )
    histories = list(
        await session.scalars(
            select(DownloadHistory)
            .where(
                DownloadHistory.external_id == f"direct:{attempt_id}",
            )
            .limit(2)
            .execution_options(populate_existing=True)
        )
    )
    pending = list(
        await session.scalars(
            select(PendingMatch)
            .where(
                PendingMatch.download_url == f"pullbox-direct://attempt/{attempt_id}",
            )
            .limit(2)
            .execution_options(populate_existing=True)
        )
    )
    if len(artifacts) != 1 or len(files) != 1 or len(histories) != 1 or len(pending) > 1:
        return None
    artifact, file, history = artifacts[0], files[0], histories[0]
    if (
        artifact.state is not DirectArtifactState.INTERVENTION
        or artifact.failure_code != attempt.failure_code
        or history.issue_id != issue.id
        or history.state is not DownloadState.FAILED
        or history.imported_at is not None
        or any(
            p.status != PendingMatchStatus.PENDING
            or p.issue_id != issue.id
            or p.match_details.get("direct_attempt_id") != attempt_id
            for p in pending
        )
    ):
        return None
    snapshot = json.dumps(
        {
            "plan": attempt.plan_snapshot,
            "revision": attempt.plan_revision,
            "progress_revision": attempt.progress_revision,
            "failure": attempt.failure_code,
            "message": attempt.error_message,
            "file": file.id,
            "path": file.file_path,
            "artifact": artifact.id,
            "source": artifact.quarantine_path,
            "history": history.id,
            "pending": [(p.id, p.match_details) for p in pending],
        },
        sort_keys=True,
    )
    return _Owner(
        issue.id, file.id, artifact.id, history.id, tuple(p.id for p in pending), snapshot
    )


async def reconcile_fulfilled_pack(session: AsyncSession, attempt_id: int) -> bool:
    """Read/verify/recheck a clean session; the caller commits the final state.

    Archive work happens outside a write transaction. No files, providers, queues,
    or import timestamps are changed by this completion-only reconciliation.
    """
    if session.new or session.dirty or session.deleted:
        raise ValueError("Pack reconciliation requires a clean session")
    owner = await _read_owner(session, attempt_id)
    if owner is None:
        return False
    try:
        prepared = await prepare_file_metadata(session, owner.issue_id)
        if not prepared.preview.ready:
            return False
        if prepared.target.binding.library_file_id != owner.file_id:
            return False
        if prepared.target.path.suffix.lower() not in {".cbz", ".zip"}:
            return False
        embedded = reconcile_archive_metadata(
            await asyncio.to_thread(
                read_archive_metadata,
                prepared.target.path,
                "cbz",
                max_solid_scan_bytes=MAX_METADATA_BYTES,
            )
        )
        expected = prepared.target.binding.metadata.issues[0]
        if (
            embedded.identity_conflicts
            or embedded.diagnostics
            or embedded.issue.issue_number_text != prepared.issue.values.issue_number_text
            or not any(item.identity in expected.identities for item in embedded.evidence)
        ):
            return False
        await validate_direct_artifact(session, prepared.target.path)
        await session.commit()
        await lock_file_mutation_admission(session)
        await ensure_no_active_import_file_mutation(session)
        await lock_archive_metadata_binding(session, prepared.target.binding)
        for column, local_id in (
            (DirectAcquisitionAttempt.id, attempt_id),
            (DirectArtifactAttempt.id, owner.artifact_id),
            (DownloadHistory.id, owner.history_id),
            *((PendingMatch.id, pid) for pid in owner.pending_ids),
        ):
            await session.execute(select(column).where(column == local_id).with_for_update())
        if await _read_owner(session, attempt_id) != owner:
            await session.rollback()
            return False
        await revalidate_archive_metadata_target(session, prepared.target)
        await asyncio.to_thread(prepared.target.check_unchanged)
    except (ValueError, OSError, DirectArtifactValidationError) as exc:
        await session.rollback()
        logger.info(
            "direct_pack_completion_unverified", attempt_id=attempt_id, reason=type(exc).__name__
        )
        return False

    attempt = await session.get(DirectAcquisitionAttempt, attempt_id)
    artifact = await session.get(DirectArtifactAttempt, owner.artifact_id)
    assert attempt is not None and artifact is not None
    now = datetime.now(UTC)
    receipt = {
        "resolution_reason": "pack_fulfilled_by_library",
        "resolved_library_file_id": owner.file_id,
        "original_failure_code": attempt.failure_code,
        "original_error_message": attempt.error_message,
    }
    attempt.plan_snapshot = {**attempt.plan_snapshot, "pack_completion": receipt}
    transition_acquisition(attempt, DirectAcquisitionState.FAILED, at=now)
    transition_artifact(artifact, DirectArtifactState.FAILED, at=now)
    attempt.error_message = artifact.error_message = PACK_FULFILLED_MESSAGE
    attempt.next_retry_at = artifact.next_retry_at = None
    advance_acquisition_progress(
        attempt,
        revision=attempt.progress_revision + 1,
        snapshot={
            "schema_version": 1,
            "stage": "superseded",
            "artifact_attempt_id": artifact.id,
        },
    )
    from pullbox.services.direct_download_history_adapter import sync_direct_download_history
    from pullbox.services.post_processing_operation_progress import (
        project_post_processing_operation_progress,
    )

    history = await sync_direct_download_history(session, attempt, artifact, at=now)
    await project_post_processing_operation_progress(session, history)
    for pending_id in owner.pending_ids:
        pending = await session.get(PendingMatch, pending_id)
        assert pending is not None
        pending.status = PendingMatchStatus.APPROVED
        pending.resolved_at = now
        pending.resolved_by = "pack_completion"
        pending.match_details = {**pending.match_details, **receipt}
    await session.flush()
    logger.info(
        "direct_pack_fulfilled_by_library",
        attempt_id=attempt_id,
        issue_id=owner.issue_id,
        file_id=owner.file_id,
    )
    return True
