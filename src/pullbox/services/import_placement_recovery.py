"""Narrow same-job recovery for durably published import placements."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.core.exceptions import ConfigurationError
from pullbox.core.library_file_ownership import build_managed_placement_signature
from pullbox.models.import_job import ImportJobAction, ImportJobActionStatus
from pullbox.models.library import LibraryFile

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class CompletedImportPlacementRecovery:
    """Validated same-job evidence for a placement awaiting DB registration."""

    action_id: int
    destination_path: Path
    destination_signature: dict[str, int | str]


async def has_completed_direct_move_placement_record(
    session: AsyncSession,
    *,
    job_id: int,
    imported_file_id: int,
    source_path: Path,
) -> bool:
    """Return whether a missing direct-move source has a durable completion row.

    This only permits execution to reach the full recovery validator. It does
    not authorize registration or filesystem mutation.
    """
    actions = await _candidate_actions(session, job_id, imported_file_id)
    for action in actions:
        payload = dict(action.payload or {})
        if payload.get("placement_completed") is not True:
            continue
        if str(payload.get("transfer_method") or "") != "move":
            continue
        original_source = _payload_path(payload, "original_source_path")
        artifact_source = _payload_path(payload, "artifact_source_path")
        if original_source is None or artifact_source is None:
            continue
        if not _same_unresolved_path(original_source, source_path):
            continue
        if not _same_unresolved_path(artifact_source, source_path):
            continue
        destination = _payload_path(payload, "destination_path")
        if destination is not None and os.path.lexists(destination):
            return True
    return False


async def load_completed_import_placement_recovery(
    session: AsyncSession,
    *,
    job_id: int,
    imported_file_id: int,
    issue_id: int,
    source_path: Path,
    transfer_method: str,
) -> CompletedImportPlacementRecovery | None:
    """Validate exact durable provenance and content for same-job recovery."""
    actions = await _candidate_actions(session, job_id, imported_file_id)
    for action in actions:
        payload = dict(action.payload or {})
        if payload.get("placement_completed") is not True:
            continue
        if int(payload.get("issue_id") or 0) != issue_id:
            continue
        if str(payload.get("transfer_method") or "") != transfer_method:
            continue
        original_source = _payload_path(payload, "original_source_path")
        artifact_source = _payload_path(payload, "artifact_source_path")
        destination = _payload_path(payload, "destination_path")
        if original_source is None or artifact_source is None or destination is None:
            continue
        if not _same_unresolved_path(original_source, source_path):
            continue
        if _same_unresolved_path(destination, source_path):
            continue
        if any(
            os.path.lexists(Path(str(temp_path)))
            for temp_path in payload.get("temp_paths") or []
            if str(temp_path)
        ):
            continue
        if not os.path.lexists(destination):
            continue
        if (
            transfer_method == "move"
            and _same_unresolved_path(artifact_source, original_source)
            and os.path.lexists(original_source)
        ):
            # A direct-move source reappeared. Preserve both paths for review.
            continue
        signature = payload.get("destination_signature")
        if not isinstance(signature, dict):
            continue
        if (
            not signature.get("content_digest")
            or signature.get("content_digest_algorithm") != "sha256"
        ):
            continue
        try:
            current_signature = build_managed_placement_signature(destination)
        except (ConfigurationError, OSError, RuntimeError, ValueError):
            continue
        if current_signature != signature:
            continue
        existing_library_file = await session.scalar(
            select(LibraryFile.id).where(LibraryFile.file_path == str(destination)).limit(1)
        )
        if existing_library_file is not None:
            # Recovery is only for the crash window before LibraryFile commit.
            continue
        return CompletedImportPlacementRecovery(
            action_id=int(action.id),
            destination_path=destination,
            destination_signature={str(key): value for key, value in signature.items()},
        )
    return None


async def _candidate_actions(
    session: AsyncSession,
    job_id: int,
    imported_file_id: int,
) -> list[ImportJobAction]:
    return list(
        (
            await session.scalars(
                select(ImportJobAction)
                .where(
                    ImportJobAction.import_job_id == job_id,
                    ImportJobAction.action_type == "library_file_placement_started",
                    ImportJobAction.status == ImportJobActionStatus.COMPLETED,
                    ImportJobAction.payload["imported_file_id"].as_integer() == imported_file_id,
                )
                .order_by(ImportJobAction.sequence_no.desc())
                .limit(2)
            )
        ).all()
    )


def _payload_path(payload: dict[str, object], key: str) -> Path | None:
    raw = str(payload.get(key) or "").strip()
    return Path(raw) if raw else None


def _same_unresolved_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
