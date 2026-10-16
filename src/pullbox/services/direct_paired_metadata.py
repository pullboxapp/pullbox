"""Direct acquisitions own their managed copy without impersonating a queue lease."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pullbox.core.exceptions import JobCancelledError, ValidationError
from pullbox.core.file_safety import get_archive_size_limit_bytes
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.models import Issue, LibraryFile
from pullbox.models.archive_metadata_publication import PublicationState
from pullbox.models.direct_acquisition import (
    DirectAcquisitionAttempt,
    DirectAcquisitionState,
    DirectArtifactAttempt,
    DirectArtifactState,
)
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.issue import IssueStatus
from pullbox.services.issue_file_metadata import (
    file_metadata_error,
    prepare_file_metadata,
    recover_file_metadata,
    write_file_metadata,
)
from pullbox.services.native_file_metadata import recover_native_file_metadata
from pullbox.tasks.post_processing_progress import PostProcessingPhase

if TYPE_CHECKING:
    from pullbox.tasks.download_post_processing_runtime import PostProcessingRuntime

_COPY_KEY = "paired_metadata_copy"


class DirectMetadataReviewError(ValidationError):
    """A managed direct copy needs attention before acquisition completion."""


@dataclass
class DirectMetadataHandoff:
    acquisition_id: int
    artifact_id: int
    download_id: int
    issue_id: int
    plan_revision: int
    source_path: Path
    source_signature: dict[str, int | str]
    safety_review: object
    allow_resource_safety_exception: bool
    marker: dict[str, Any] | None
    final_path: str | None
    cancel_event: asyncio.Event | None

    async def owner(
        self, session: AsyncSession
    ) -> tuple[DirectAcquisitionAttempt, DownloadHistory]:
        attempt = await session.get(DirectAcquisitionAttempt, self.acquisition_id)
        artifact = await session.get(DirectArtifactAttempt, self.artifact_id)
        history = await session.get(DownloadHistory, self.download_id)
        if (
            attempt is None
            or artifact is None
            or history is None
            or attempt.issue_id != self.issue_id
            or attempt.state is not DirectAcquisitionState.POST_PROCESSING
            or attempt.plan_revision != self.plan_revision
            or artifact.acquisition_attempt_id != self.acquisition_id
            or not artifact.is_selected
            or artifact.state is not DirectArtifactState.VALIDATING
            or history.issue_id != self.issue_id
            or history.external_id != f"direct:{self.acquisition_id}"
            or history.state is not DownloadState.POST_PROCESSING
            or history.imported_at is not None
            or (attempt.plan_snapshot or {}).get(_COPY_KEY) != self.marker
            or (attempt.plan_snapshot or {}).get("safety_review") != self.safety_review
        ):
            raise DirectMetadataReviewError(
                "This direct acquisition changed. Review it before retrying."
            )
        if self.marker is not None and (
            attempt.library_file_id != self.marker["file_id"]
            or history.final_path != self.final_path
        ):
            raise DirectMetadataReviewError(
                "The saved direct-download copy changed. Review it before retrying."
            )
        return attempt, history

    async def finish(
        self, session: AsyncSession, download: DownloadHistory, runtime: PostProcessingRuntime
    ) -> Path:
        """Commit copy ownership before archive work; only the executor completes acquisition."""
        attempt, history = await self.owner(session)
        file = await session.scalar(
            select(LibraryFile).where(LibraryFile.issue_id == self.issue_id)
        )
        if file is None or (self.marker is not None and file.id != self.marker["file_id"]):
            raise DirectMetadataReviewError("The registered direct-download copy is unavailable.")
        if self.marker is None:
            if file.file_path != download.final_path:
                raise DirectMetadataReviewError("The direct-download destination changed.")
            if (
                await asyncio.to_thread(build_file_identity_signature, self.source_path)
                != self.source_signature
            ):
                raise DirectMetadataReviewError(
                    "The downloaded source changed during placement. Review it before retrying."
                )
            self.marker = {
                "artifact_id": self.artifact_id,
                "plan_revision": self.plan_revision,
                "file_id": file.id,
                "source_signature": self.source_signature,
                "copy_signature": await asyncio.to_thread(
                    build_file_identity_signature, Path(file.file_path)
                ),
            }
            attempt.plan_snapshot = {**(attempt.plan_snapshot or {}), _COPY_KEY: self.marker}
            attempt.library_file_id = file.id
            self.final_path = history.final_path = file.file_path
        await session.commit()
        factory = async_sessionmaker(session.bind, expire_on_commit=False)
        operation = uuid5(
            NAMESPACE_URL, f"pullbox:direct-metadata:{self.acquisition_id}:{self.artifact_id}"
        )

        async def control() -> None:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise JobCancelledError("Direct metadata writing cancelled.")
            async with factory() as reader:
                await self.owner(reader)

        async def progress(_stage: str, _current: int, _total: int, _unit: str) -> None:
            await control()

        runtime.enter_phase(PostProcessingPhase.WRITING_METADATA)
        try:
            await control()
            native_recovered = await recover_native_file_metadata(factory, self.issue_id, operation)
            recovered = await recover_file_metadata(factory, operation)
            async with factory() as reader:
                file = await reader.get(LibraryFile, self.marker["file_id"])
                if file is None or file.issue_id != self.issue_id:
                    raise DirectMetadataReviewError("The direct-download library binding changed.")
                if native_recovered is None and file.file_path != self.final_path:
                    raise DirectMetadataReviewError(
                        "The saved direct-download path changed. Review it before retrying."
                    )
                limit = await get_archive_size_limit_bytes(reader)
                prepared = await prepare_file_metadata(reader, self.issue_id)
                limit = max(limit, prepared.approved_resource_limit or 0)
            if not prepared.preview.ready:
                raise DirectMetadataReviewError(
                    "The downloaded file and library metadata disagree. Review file metadata "
                    "on the issue, then retry this direct acquisition. Both copies were preserved."
                )
            if not prepared.preview.unchanged:
                if recovered is PublicationState.FINALIZED or native_recovered is not None:
                    raise DirectMetadataReviewError(
                        "The previously written copy changed. Review file metadata before retrying."
                    )
                if recovered is PublicationState.ABANDONED:
                    operation = uuid5(operation, prepared.preview.review_key)
                await write_file_metadata(
                    factory,
                    self.issue_id,
                    prepared.preview.review_key,
                    operation,
                    limit=limit,
                    check_control=control,
                    progress=progress,
                )
            async with factory.begin() as writer:
                _, current_history = await self.owner(writer)
                file = await writer.get(LibraryFile, self.marker["file_id"])
                if file is None or file.issue_id != self.issue_id:
                    raise DirectMetadataReviewError("The direct-download library binding changed.")
                self.final_path = current_history.final_path = file.file_path
                issue = await writer.get(Issue, self.issue_id)
                assert issue is not None
                issue.status = IssueStatus.OWNED
            # The caller's session still contains the pre-publication objects.
            await session.refresh(attempt)
            return Path(self.final_path)
        except ValueError as exc:
            raise DirectMetadataReviewError(
                f"{file_metadata_error(exc)} Review file metadata on the issue, "
                "then retry this acquisition."
            ) from exc


async def prepare_direct_handoff(
    session: AsyncSession,
    *,
    acquisition_id: int,
    download_id: int,
    issue_id: int,
    source_path: Path,
    allow_resource_safety_exception: bool,
    cancel_event: asyncio.Event | None = None,
) -> DirectMetadataHandoff:
    attempt = await session.get(DirectAcquisitionAttempt, acquisition_id)
    selected = list(
        await session.scalars(
            select(DirectArtifactAttempt)
            .where(
                DirectArtifactAttempt.acquisition_attempt_id == acquisition_id,
                DirectArtifactAttempt.is_selected.is_(True),
            )
            .limit(2)
        )
    )
    history = await session.get(DownloadHistory, download_id)
    if attempt is None or history is None or len(selected) != 1:
        raise DirectMetadataReviewError(
            "The direct acquisition no longer has one selected artifact."
        )
    marker = (attempt.plan_snapshot or {}).get(_COPY_KEY)
    if marker is not None and (
        not isinstance(marker, dict)
        or marker.get("artifact_id") != selected[0].id
        or marker.get("plan_revision") != attempt.plan_revision
        or not isinstance(marker.get("file_id"), int)
    ):
        raise DirectMetadataReviewError(
            "A previous direct-download copy needs review before changing sources."
        )
    handoff = DirectMetadataHandoff(
        acquisition_id,
        selected[0].id,
        download_id,
        issue_id,
        attempt.plan_revision,
        source_path,
        await asyncio.to_thread(build_file_identity_signature, source_path),
        (attempt.plan_snapshot or {}).get("safety_review"),
        allow_resource_safety_exception,
        marker,
        history.final_path if marker is not None else None,
        cancel_event,
    )
    await handoff.owner(session)
    if allow_resource_safety_exception and (
        not isinstance(handoff.safety_review, dict)
        or handoff.safety_review.get("source_signature") != handoff.source_signature
    ):
        raise DirectMetadataReviewError(
            "The downloaded source changed or its approval has no file evidence. "
            "Retry the download and review its size before approving it again."
        )
    return handoff
