"""Review retained direct-download files without reacquiring them."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import select

from pullbox.config import get_settings
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
from pullbox.schemas.issue_file_metadata import (
    FileMetadataApproval,
    FileMetadataChange,
    FileMetadataConflict,
)
from pullbox.services.direct_acquisition_state import (
    advance_acquisition_progress,
    transition_acquisition,
    transition_artifact,
)
from pullbox.services.direct_download_history_adapter import sync_direct_download_history
from pullbox.services.direct_metadata_file_proof import verify_file_proof
from pullbox.services.issue_file_metadata import prepare_file_metadata, write_file_metadata
from pullbox.services.issue_import_service import (
    execute_manual_issue_import,
    prepare_manual_issue_import,
)
from pullbox.services.library_mutation_coordination import lock_file_mutation_admission
from pullbox.services.manual_import_metadata_review import preview_manual_import_metadata
from pullbox.services.manual_paired_metadata import read_manual_metadata_plan
from pullbox.services.post_processing_operation_progress import (
    project_post_processing_operation_progress,
)
from pullbox.utilities.import_guards import ensure_no_active_import_file_mutation
from pullbox.utilities.models import UtilityJob, UtilityJobItem

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from pullbox.schemas.issue_file_metadata import FileMetadataChoices
    from pullbox.services.archive_metadata_binding import ArchiveMetadataTarget
    from pullbox.utilities.executors.archive_subprocess import ControlCheck, ProgressCallback


class DirectMetadataRecoveryError(ValueError):
    """An operator-actionable recovery error without a download URL or secret."""


class DirectMetadataRecoveryPreview(BaseModel):
    issue_id: int
    file_name: str
    review_key: str
    unchanged: bool = False
    ready: bool
    conflicts: list[FileMetadataConflict] = Field(default_factory=list)
    changes: list[FileMetadataChange] = Field(default_factory=list)
    converts_to_cbz: bool = False
    restores_copy: bool = False
    superseded: bool = False


@dataclass(frozen=True)
class RecoveryOwner:
    pending_id: int
    issue_id: int
    attempt_id: int
    artifact_id: int
    history_id: int
    plan_revision: int
    marker: dict[str, Any]
    safety_review: object
    file_id: int | None
    file_path: str | None
    source: Path
    superseded: bool
    source_proof: dict[str, int | str] | None = None


@dataclass(frozen=True)
class PreparedRecovery:
    owner: RecoveryOwner
    metadata_key: str
    preview: DirectMetadataRecoveryPreview
    file_target: ArchiveMetadataTarget | None


async def _read_owner(session: AsyncSession, pending_id: int) -> RecoveryOwner:
    pending = await session.get(PendingMatch, pending_id, populate_existing=True)
    if (
        pending is None
        or pending.status != PendingMatchStatus.PENDING
        or (pending.match_details or {}).get("failure_code") != "direct_metadata_review_required"
    ):
        raise DirectMetadataRecoveryError(
            "This metadata recovery is no longer pending. Refresh the queue."
        )
    attempt_id = pending.match_details.get("direct_attempt_id")
    if type(attempt_id) is not int:
        raise DirectMetadataRecoveryError("The saved download identity is unavailable.")
    attempt = await session.get(DirectAcquisitionAttempt, attempt_id, populate_existing=True)
    if (
        attempt is None
        or attempt.issue_id != pending.issue_id
        or attempt.state is not DirectAcquisitionState.INTERVENTION
        or attempt.failure_class is not DirectArtifactFailureClass.POST_PROCESS
        or attempt.failure_code != "direct_metadata_review_required"
    ):
        raise DirectMetadataRecoveryError(
            "The download changed. Refresh the queue before reviewing metadata."
        )
    selected = list(
        await session.scalars(
            select(DirectArtifactAttempt)
            .where(
                DirectArtifactAttempt.acquisition_attempt_id == attempt.id,
                DirectArtifactAttempt.is_selected.is_(True),
            )
            .limit(2)
        )
    )
    marker = (attempt.plan_snapshot or {}).get("paired_metadata_copy")
    if (
        len(selected) != 1
        or not isinstance(marker, dict)
        or marker.get("artifact_id") != selected[0].id
        or marker.get("plan_revision") != attempt.plan_revision
        or type(marker.get("file_id")) is not int
        or selected[0].state is not DirectArtifactState.INTERVENTION
        or selected[0].failure_code != "direct_metadata_review_required"
        or not selected[0].quarantine_path
    ):
        raise DirectMetadataRecoveryError(
            "The retained download evidence changed. Review the issue before retrying."
        )
    histories = list(
        await session.scalars(
            select(DownloadHistory)
            .where(
                DownloadHistory.external_id == f"direct:{attempt.id}",
            )
            .limit(2)
        )
    )
    if (
        len(histories) != 1
        or histories[0].issue_id != pending.issue_id
        or histories[0].imported_at is not None
        or histories[0].state is not DownloadState.FAILED
    ):
        raise DirectMetadataRecoveryError(
            "The download history changed. Refresh the queue before retrying."
        )
    files = list(
        await session.scalars(
            select(LibraryFile).where(LibraryFile.issue_id == pending.issue_id).limit(2)
        )
    )
    if len(files) > 1:
        raise DirectMetadataRecoveryError(
            "This issue has multiple registered files. Reconcile them before recovery."
        )
    file = files[0] if files else None
    if file is None:
        issue = await session.get(Issue, pending.issue_id)
        if issue is None or issue.status is not IssueStatus.WANTED:
            raise DirectMetadataRecoveryError(
                "The library copy is missing. Mark this issue Wanted "
                "before explicitly restoring it."
            )
    return RecoveryOwner(
        pending.id,
        pending.issue_id,
        attempt.id,
        selected[0].id,
        histories[0].id,
        attempt.plan_revision,
        dict(marker),
        (attempt.plan_snapshot or {}).get("safety_review"),
        file.id if file else None,
        file.file_path if file else None,
        Path(selected[0].quarantine_path),
        file is not None and (file.id != marker["file_id"] or attempt.library_file_id != file.id),
    )


async def _check_source(owner: RecoveryOwner) -> dict[str, int | str] | None:
    try:
        if (
            owner.source.parent.name != f"artifact-{owner.artifact_id}"
            or owner.source.parent.parent.name != f"attempt-{owner.attempt_id}"
        ):
            raise ValueError("The retained download path changed.")
        return await asyncio.to_thread(
            verify_file_proof,
            owner.source,
            owner.marker.get("source_signature"),
            owner.marker.get("source_sha256"),
            owner.source_proof,
        )
    except (OSError, ValueError) as exc:
        raise DirectMetadataRecoveryError(
            "The retained download is missing, changed, or has no saved content proof for "
            "its changed storage identity. Review the file on the issue page before retrying; "
            "Pullbox will not download it again."
        ) from exc


async def prepare_direct_metadata_recovery(
    session: AsyncSession,
    pending_id: int,
    *,
    choices: FileMetadataChoices | None = None,
) -> PreparedRecovery:
    if not get_settings().metadata_paired_direct_writer_enabled:
        raise DirectMetadataRecoveryError(
            "Enable direct-download paired metadata writing before recovering this file."
        )
    owner = await _read_owner(session, pending_id)
    await ensure_no_active_import_file_mutation(session)
    await session.commit()
    owner = replace(owner, source_proof=await _check_source(owner))
    file_target = None
    if owner.file_id is not None:
        prepared = await prepare_file_metadata(session, owner.issue_id, choices=choices)
        file_target = prepared.target
        if prepared.target.binding.library_file_id != owner.file_id:
            raise DirectMetadataRecoveryError("The library file changed. Preview again.")
        try:
            await asyncio.to_thread(
                verify_file_proof,
                prepared.target.path,
                owner.marker.get("copy_signature"),
                owner.marker.get("copy_sha256"),
            )
        except (OSError, ValueError):
            owner = replace(owner, superseded=True)
        preview = DirectMetadataRecoveryPreview(
            issue_id=owner.issue_id,
            **prepared.preview.model_dump(exclude={"file_id"}),
            superseded=owner.superseded,
        )
        if owner.superseded and (not preview.ready or not preview.unchanged):
            raise DirectMetadataRecoveryError(
                "A different library copy now owns this issue. Review its file metadata on "
                "the issue page first; this recovery will not overwrite it."
            )
    else:
        if not get_settings().metadata_paired_import_writer_enabled:
            raise DirectMetadataRecoveryError(
                "Enable paired import metadata writing before restoring the retained download."
            )
        plan = await read_manual_metadata_plan(session, owner.issue_id)
        if plan.existing_file is not None or not plan.policy.update_embedded_comicinfo_from_match:
            raise DirectMetadataRecoveryError(
                "The import settings or library binding changed. "
                "Enable embedded metadata updates and preview again."
            )
        await session.commit()
        reviewed = await preview_manual_import_metadata(plan, owner.source, choices or {})
        preview = DirectMetadataRecoveryPreview(
            issue_id=owner.issue_id,
            file_name=owner.source.name,
            review_key=reviewed.preview.review_key,
            ready=reviewed.preview.ready,
            conflicts=reviewed.preview.conflicts,
            restores_copy=True,
            converts_to_cbz=owner.source.suffix.lower() in {".cbr", ".cb7", ".pdf"},
        )
    metadata_key = preview.review_key
    preview.review_key = hashlib.sha256(
        json.dumps(
            {"owner": asdict(owner), "metadata_key": metadata_key},
            default=str,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return PreparedRecovery(owner, metadata_key, preview, file_target)


async def _complete(
    session: AsyncSession,
    owner: RecoveryOwner,
    file: LibraryFile,
    operation: UUID,
    review_key: str,
) -> None:
    await lock_file_mutation_admission(session)
    await ensure_no_active_import_file_mutation(session)
    current = await _read_owner(session, owner.pending_id)
    # The approved writer may convert the owned path or restore its missing registration.
    expected = replace(
        owner,
        file_id=file.id,
        file_path=file.file_path,
        superseded=current.superseded if owner.file_id is None else owner.superseded,
    )
    # Supersession can also be detected from changed bytes at the same registered path.
    if (
        replace(
            current,
            superseded=owner.superseded if owner.file_id else current.superseded,
            source_proof=owner.source_proof,
        )
        != expected
    ):
        raise DirectMetadataRecoveryError(
            "Recovery ownership changed before completion. The saved file needs review."
        )
    pending = await session.get(PendingMatch, owner.pending_id)
    attempt = await session.get(DirectAcquisitionAttempt, owner.attempt_id)
    artifact = await session.get(DirectArtifactAttempt, owner.artifact_id)
    assert pending is not None and attempt is not None and artifact is not None
    now = datetime.now(UTC)
    reason = "metadata_superseded" if owner.superseded else "metadata_completed"
    if owner.superseded:
        transition_acquisition(attempt, DirectAcquisitionState.FAILED, at=now)
        transition_artifact(artifact, DirectArtifactState.FAILED, at=now)
        attempt.error_message = (
            "Superseded by a verified library copy; "
            "the previous transfer was not marked successful."
        )
        artifact.error_message = attempt.error_message
    else:
        transition_acquisition(attempt, DirectAcquisitionState.POST_PROCESSING, at=now)
        transition_artifact(artifact, DirectArtifactState.VALIDATING, at=now)
        transition_acquisition(attempt, DirectAcquisitionState.COMPLETED, at=now)
        transition_artifact(artifact, DirectArtifactState.COMPLETED, at=now)
        attempt.library_file_id = file.id
        attempt.failure_class = artifact.failure_class = None
        attempt.failure_code = artifact.failure_code = None
        attempt.error_message = artifact.error_message = None
        issue = await session.get(Issue, owner.issue_id)
        assert issue is not None
        issue.status = IssueStatus.OWNED
    advance_acquisition_progress(
        attempt,
        revision=attempt.progress_revision + 1,
        snapshot={
            "schema_version": 1,
            "stage": "completed" if not owner.superseded else "superseded",
            "percent": 100,
            "artifact_attempt_id": artifact.id,
        },
    )
    history = await sync_direct_download_history(
        session, attempt, artifact, at=now, final_path=file.file_path
    )
    await project_post_processing_operation_progress(session, history, None)
    pending.status = PendingMatchStatus.APPROVED
    pending.resolved_at = now
    pending.resolved_by = "metadata_recovery"
    pending.match_details = {
        **pending.match_details,
        "resolution_reason": reason,
        "metadata_operation_id": str(operation),
        "metadata_review_key": review_key,
        "resolved_library_file_id": file.id,
    }


async def run_direct_metadata_recovery(
    factory: async_sessionmaker[AsyncSession],
    pending_id: int,
    review_key: str,
    operation: UUID,
    *,
    choices: FileMetadataChoices,
    limit: int,
    check_control: ControlCheck,
    progress: ProgressCallback,
    job_id: str | None = None,
) -> str:
    async with factory() as reader:
        pending = await reader.get(PendingMatch, pending_id)
        if (
            pending is not None
            and pending.status == PendingMatchStatus.APPROVED
            and (
                pending.match_details.get("metadata_operation_id") == str(operation)
                and pending.match_details.get("metadata_review_key") == review_key
            )
        ):
            return "already_completed"
        prepared = await prepare_direct_metadata_recovery(reader, pending_id, choices=choices)
    if not prepared.preview.ready or prepared.preview.review_key != review_key:
        raise DirectMetadataRecoveryError(
            "The file, metadata, or recovery state changed. Preview again before writing."
        )
    owner = prepared.owner

    async def control() -> None:
        await check_control()
        async with factory() as reader:
            current = await _read_owner(reader, pending_id)
        if replace(current, superseded=owner.superseded, source_proof=owner.source_proof) != owner:
            raise DirectMetadataRecoveryError(
                "The recovery or library binding changed. No further file changes will be made."
            )
        await _check_source(owner)

    await control()
    if job_id is not None:
        async with factory.begin() as writer:
            item = await writer.get(UtilityJobItem, operation.hex)
            if item is None or item.job_id != job_id:
                raise DirectMetadataRecoveryError("The recovery job no longer owns this operation.")
            item.before_state = json.dumps(
                {
                    "direct_metadata_recovery": {
                        "owner": asdict(owner),
                        "review_key": review_key,
                    }
                },
                default=str,
            )
    if owner.file_id is None:
        async with factory() as writer:
            plan = await prepare_manual_issue_import(
                writer, issue_id=owner.issue_id, file_path=str(owner.source), move_to_library=True
            )
            result = await execute_manual_issue_import(
                writer,
                plan,
                metadata_approval=FileMetadataApproval(
                    review_key=prepared.metadata_key, choices=choices
                ),
                cancellation_check=control,
                preparation_progress_callback=progress,
                comicinfo_progress_callback=progress,
            )
            await _complete(writer, owner, result.library_file, operation, review_key)
            await writer.commit()
        return "restored"
    outcome = (
        "superseded"
        if owner.superseded
        else await write_file_metadata(
            factory,
            owner.issue_id,
            prepared.metadata_key,
            operation,
            limit=limit,
            choices=choices,
            check_control=control,
            progress=progress,
            job_id=job_id,
        )
    )
    if job_id is not None and await recover_direct_metadata_operation(factory, operation, job_id):
        return outcome
    async with factory.begin() as writer:
        if outcome in {"superseded", "unchanged"}:
            from pullbox.services.archive_metadata_binding import revalidate_archive_metadata_target

            assert prepared.file_target is not None
            await lock_file_mutation_admission(writer)
            await revalidate_archive_metadata_target(writer, prepared.file_target)
            await asyncio.to_thread(prepared.file_target.check_unchanged)
        file = await writer.get(LibraryFile, owner.file_id)
        if file is None or file.issue_id != owner.issue_id:
            raise DirectMetadataRecoveryError("The library binding changed before completion.")
        await _complete(writer, owner, file, operation, review_key)
    return outcome


async def recover_direct_metadata_operation(
    factory: async_sessionmaker[AsyncSession],
    operation: UUID,
    job_id: str,
) -> bool:
    """A lost acknowledgement needs the exact writer journal, never OWNED alone."""
    from pullbox.models.archive_metadata_publication import PublicationState
    from pullbox.services.archive_metadata_binding import revalidate_archive_metadata_target
    from pullbox.services.archive_metadata_publication import (
        inspect_archive_publication,
        load_archive_publication,
    )
    from pullbox.services.issue_file_metadata import recover_file_metadata
    from pullbox.services.native_file_metadata import recover_native_file_metadata

    async with factory() as session:
        job = await session.get(UtilityJob, job_id)
        item = await session.get(UtilityJobItem, operation.hex)
        if job is None or item is None or item.job_id != job_id:
            return False
        config = json.loads(job.config)
        saved = json.loads(item.before_state or "{}").get("direct_metadata_recovery")
        if not saved:
            return False
        owner = TypeAdapter(RecoveryOwner).validate_python(saved["owner"])
        if (
            config.get("recovery_pending_id") != owner.pending_id
            or config.get("issue_id") != owner.issue_id
            or config.get("review_key") != saved["review_key"]
        ):
            raise DirectMetadataRecoveryError(
                "The recovery job evidence changed. Review the queue entry again."
            )
        pending = await session.get(PendingMatch, owner.pending_id)
        if (
            pending is not None
            and pending.status == PendingMatchStatus.APPROVED
            and pending.match_details.get("metadata_operation_id") == str(operation)
        ):
            return True
        # Restoration commits its new registration and recovery result together.
        if owner.file_id is None:
            return False
        receipt = await load_archive_publication(session, operation)
    await _check_source(owner)
    if receipt is not None:
        if (
            receipt.plan.metadata_job_id != job_id
            or receipt.plan.target.binding.library_file_id != owner.file_id
        ):
            raise DirectMetadataRecoveryError(
                "The completed metadata journal belongs to a different file."
            )
        if await recover_file_metadata(factory, operation) != PublicationState.FINALIZED:
            return False
        async with factory() as session:
            receipt = await load_archive_publication(session, operation)
        assert receipt is not None
        inspected = await inspect_archive_publication(receipt)
        if inspected.digest != receipt.plan.output_digest:
            raise DirectMetadataRecoveryError(
                "The written file changed after publication. Review it before completing recovery."
            )
    elif await recover_native_file_metadata(factory, owner.issue_id, operation) is None:
        return False
    async with factory() as session:
        prepared = await prepare_file_metadata(session, owner.issue_id)
        if not prepared.preview.ready or not prepared.preview.unchanged:
            raise DirectMetadataRecoveryError(
                "The recovered file metadata changed. Preview the queue entry again."
            )
    async with factory.begin() as session:
        await lock_file_mutation_admission(session)
        await revalidate_archive_metadata_target(session, prepared.target)
        await asyncio.to_thread(prepared.target.check_unchanged)
        file = await session.get(LibraryFile, owner.file_id)
        if file is None or file.issue_id != owner.issue_id:
            raise DirectMetadataRecoveryError("The library binding changed after publication.")
        await _complete(session, owner, file, operation, saved["review_key"])
    return True
