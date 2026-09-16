"""Helpers for freezing import-time library policy on an import job."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pullbox.core.library_policy import (
    LibraryIngestPolicy,
    serialize_library_ingest_policy,
)
from pullbox.models.import_job import ImportFileHandlingMode, ImportSourceType

if TYPE_CHECKING:
    from pullbox.models.import_job import ImportJob


def _effective_transfer_method_for_import(job: ImportJob, policy: LibraryIngestPolicy) -> str:
    """Return the non-destructive execution transfer method for collection imports."""
    if job.source_type in {ImportSourceType.FILESYSTEM, ImportSourceType.MYLAR3}:
        return "copy"
    return policy.post_processing_method


def apply_ingest_policy_to_import_job(
    job: ImportJob,
    policy: LibraryIngestPolicy,
) -> None:
    """Apply and snapshot the ingest policy that this import job should honor."""
    job.ingest_policy_snapshot = serialize_library_ingest_policy(policy)
    job.transfer_method = policy.post_processing_method
    job.torrent_import_strategy = policy.torrent_import_strategy
    job.effective_import_strategy = "standard"
    handling_mode = job.file_handling_mode or ImportFileHandlingMode.MANAGED_COPY
    if handling_mode == ImportFileHandlingMode.IN_PLACE:
        job.move_to_library = False
        job.effective_transfer_method = "leave_in_place"
        job.source_preserved = True
        job.convert_to_preferred_format = False
        job.update_embedded_comicinfo_from_match = False
        return

    effective_transfer_method = _effective_transfer_method_for_import(job, policy)
    job.move_to_library = True
    job.effective_transfer_method = effective_transfer_method
    job.source_preserved = effective_transfer_method in {"copy", "hardlink", "symlink"}
    job.convert_to_preferred_format = policy.normalize_imported_archives_to_cbz
    job.update_embedded_comicinfo_from_match = policy.update_embedded_comicinfo_from_match
