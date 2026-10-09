"""Reuse a direct size approval only for its unchanged, registered managed copy."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pullbox.core.archive import ArchiveReader
from pullbox.core.archive_metadata import MAX_METADATA_BYTES
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactAttempt,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class DirectMetadataApproval:
    source: Path
    source_signature: dict[str, Any]
    copy_signature: dict[str, Any]

    async def budget(self, target: Path) -> int | None:
        def inspect() -> int | None:
            if (
                build_file_identity_signature(self.source) != self.source_signature
                or build_file_identity_signature(target) != self.copy_signature
            ):
                return None
            members = ArchiveReader(self.source).list_members()
            if (
                build_file_identity_signature(self.source) != self.source_signature
                or build_file_identity_signature(target) != self.copy_signature
            ):
                return None
            return sum(member.size for member in members) + 2 * MAX_METADATA_BYTES

        return await asyncio.to_thread(inspect)


async def read_direct_metadata_approval(
    session: AsyncSession, file_id: int
) -> DirectMetadataApproval | None:
    candidates = list(
        await session.execute(
            select(DirectAcquisitionAttempt, DirectArtifactAttempt)
            .join(
                DirectArtifactAttempt,
                DirectArtifactAttempt.acquisition_attempt_id == DirectAcquisitionAttempt.id,
            )
            .where(
                DirectAcquisitionAttempt.library_file_id == file_id,
                DirectAcquisitionAttempt.state.in_(
                    [DirectAcquisitionState.POST_PROCESSING, DirectAcquisitionState.INTERVENTION]
                ),
                DirectArtifactAttempt.is_selected.is_(True),
            )
            .limit(2)
        )
    )
    if len(candidates) != 1:
        return None
    attempt, artifact = candidates[0]
    snapshot = attempt.plan_snapshot or {}
    review = snapshot.get("safety_review")
    marker = snapshot.get("paired_metadata_copy")
    if (
        not isinstance(review, dict)
        or review.get("overrideable") is not True
        or review.get("allowed_once") is not True
        or not isinstance(marker, dict)
        or marker.get("file_id") != file_id
        or marker.get("artifact_id") != artifact.id
        or marker.get("plan_revision") != attempt.plan_revision
        or not isinstance(marker.get("source_signature"), dict)
        or marker.get("source_signature") != review.get("source_signature")
        or not isinstance(marker.get("copy_signature"), dict)
        or not artifact.quarantine_path
    ):
        return None
    return DirectMetadataApproval(
        Path(artifact.quarantine_path), marker["source_signature"], marker["copy_signature"]
    )
