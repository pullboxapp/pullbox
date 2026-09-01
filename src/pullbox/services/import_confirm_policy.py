"""Confirm-import policy helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pullbox.core.exceptions import ValidationError
from pullbox.core.library_policy import (
    load_effective_library_ingest_policy,
    load_library_ingest_policy,
    load_search_on_add_default,
)
from pullbox.models.import_job import ImportFileHandlingMode
from pullbox.models.library import LibraryRoot
from pullbox.services.import_policy_snapshot import apply_ingest_policy_to_import_job
from pullbox.services.import_root_policy_activation import (
    apply_future_root_policy_to_ingest_policy,
)
from pullbox.services.library_root_management import validate_managed_library_root

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportJob
    from pullbox.schemas.import_job import ConfirmImportRequest


async def apply_confirm_import_policy(
    session: AsyncSession,
    job: ImportJob,
    request: ConfirmImportRequest,
) -> None:
    """Apply global import policy and confirm-time overrides to an import job."""
    if request.monitored is not None:
        job.monitored = request.monitored
    if request.target_library_root_id is not None:
        if (
            job.future_layout_requested
            and job.target_library_root_id is not None
            and request.target_library_root_id != job.target_library_root_id
        ):
            raise ValidationError(
                "The target library root cannot change after future layout setup."
            )
        requested_root = await session.get(LibraryRoot, request.target_library_root_id)
        if requested_root is None:
            raise ValidationError("The selected managed library root does not exist.")
        await validate_managed_library_root(requested_root)
        job.target_library_root_id = request.target_library_root_id

    ingest_policy = (
        await load_effective_library_ingest_policy(session, job.target_library_root_id)
        if job.target_library_root_id is not None
        else await load_library_ingest_policy(session)
    )
    if job.future_layout_requested and job.future_root_policy_snapshot:
        ingest_policy = apply_future_root_policy_to_ingest_policy(
            ingest_policy,
            job.future_root_policy_snapshot,
            source_import_job_id=job.id,
        )
    search_on_add = await load_search_on_add_default(session)
    handling_mode = job.file_handling_mode or ImportFileHandlingMode.MANAGED_COPY
    if request.search_on_add is not None and request.search_on_add != search_on_add:
        raise ValidationError("Search on add is now controlled by the global import policy.")
    if handling_mode == ImportFileHandlingMode.IN_PLACE and request.move_to_library is True:
        raise ValidationError("move_to_library=true conflicts with the selected in-place mode.")
    if handling_mode == ImportFileHandlingMode.MANAGED_COPY and request.move_to_library is False:
        raise ValidationError(
            "Collection import now always creates library artifacts. "
            "The deprecated move_to_library=false override is no longer supported."
        )
    if (
        request.update_embedded_comicinfo_from_match is not None
        and request.update_embedded_comicinfo_from_match
        != ingest_policy.update_embedded_comicinfo_from_match
    ):
        raise ValidationError(
            "Embedded ComicInfo updates are now controlled by the global ingest policy."
        )

    job.search_on_add = search_on_add
    apply_ingest_policy_to_import_job(job, ingest_policy)
    if search_on_add:
        job.monitored = True

    effective_transfer_method = job.effective_transfer_method or job.transfer_method
    if job.convert_to_preferred_format and effective_transfer_method in {"hardlink", "symlink"}:
        raise ValidationError(
            "Normalize Imported Archives to CBZ requires the transfer method to be Move or Copy."
        )
    if job.update_embedded_comicinfo_from_match and effective_transfer_method not in {
        "move",
        "copy",
    }:
        raise ValidationError(
            "Updating embedded ComicInfo.xml from matched issue requires the "
            "transfer method to be Move or Copy."
        )
