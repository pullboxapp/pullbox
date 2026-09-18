"""Reinspect explicitly recovered files without relaxing ordinary source guards."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.core.file_safety import (
    get_archive_size_limit_bytes,
    is_dangerous_file_blocking_enabled,
)
from pullbox.core.library_file_ownership import (
    ReferencedFileValidationError,
    build_file_identity_signature,
    validate_file_identity_signature,
)
from pullbox.models.issue import Issue
from pullbox.models.series import Series
from pullbox.services.import_review_recheck import (
    _apply_completed_file_recheck,
    _retry_source_roots,
    inspect_review_source,
)
from pullbox.services.import_source_metadata import source_metadata_for_import_file

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportedFile, ImportedSeries, ImportJob


async def refresh_recovery_source(
    session: AsyncSession, job: ImportJob, item: ImportedSeries, file: ImportedFile
) -> None:
    """Refresh a remounted source only after archive and exact issue checks pass."""
    manual = file.match_method == "orphan_recovery"
    diagnostics = dict(file.diagnostics or {})
    source_metadata = diagnostics.get("source_metadata")
    folder_conflict = isinstance(source_metadata, dict) and bool(
        source_metadata.get("identity_conflicts")
    )
    if not manual and dict(item.diagnostics or {}).get("kind") not in {
        "known_series_recovery",
        "deferred_recovery",
        "completed_import_mixed_folder_recovery",
    }:
        return
    previous = dict(file.source_signature or {})
    path = Path(file.file_path)
    current = await asyncio.to_thread(build_file_identity_signature, path)
    device_changed = previous.get("device") != current["device"]
    # Only device renumbering is eligible, never a different path, inode,
    # size or timestamp. The global signature validator remains strict.
    expected = {**previous, "device": current["device"]} if device_changed else previous
    validate_file_identity_signature(expected, current)
    if device_changed and not isinstance(previous.get("device"), int):
        raise ReferencedFileValidationError("source_signature_missing", "Missing source identity.")
    if not folder_conflict and not device_changed:
        return

    target = await session.scalar(
        select(Issue)
        .join(Series, Series.id == Issue.series_id)
        .where(
            Issue.id == file.matched_issue_id
            if file.matched_issue_id
            else Issue.comicvine_id == file.matched_issue_cv_id,
            Series.id == item.series_id,
            Series.comicvine_id == item.cv_id,
        )
    )
    if target is None or target.comicvine_id is None:
        raise ReferencedFileValidationError(
            "source_identity_changed", "The recovery issue does not belong to the reviewed series."
        )
    if file.matched_issue_cv_id not in (None, target.comicvine_id):
        raise ReferencedFileValidationError(
            "source_identity_changed", "Recovery issue IDs disagree."
        )
    roots = await _retry_source_roots(session, job, file_ids=[file.id])
    pairs = [(root, await asyncio.to_thread(root.resolve, strict=True)) for root in roots]
    block_dangerous = await is_dangerous_file_blocking_enabled(session)
    max_size = await get_archive_size_limit_bytes(session)
    exception = diagnostics.get("safety_exception")
    previous_block = exception.get("previous_block") if isinstance(exception, dict) else None
    approved_code = (
        previous_block.get("code")
        if isinstance(exception, dict)
        and exception.get("allowed_once") is True
        and isinstance(previous_block, dict)
        and previous_block.get("overrideable") is True
        else None
    )
    if approved_code == "archive_decompressed_size_limit":
        max_size = sys.maxsize
    metadata, content, signature = await asyncio.to_thread(
        inspect_review_source,
        path,
        source_metadata_for_import_file(item, file),
        expected,
        roots=pairs,
        block_dangerous=block_dangerous,
        max_archive_size=max_size,
        accept_replaced_files=False,
        sidecars={},
    )
    content_block = content.get("file_safety")
    if (
        approved_code == "single_page_comic"
        and isinstance(content_block, dict)
        and content_block.get("code") == approved_code
    ):
        content = {key: value for key, value in content.items() if key != "file_safety"}
    embedded = metadata.diagnostics.get("embedded_identity")
    if "file_safety" not in content and (
        not isinstance(embedded, dict) or embedded.get("issue_id") != target.comicvine_id
    ):
        raise ReferencedFileValidationError(
            "source_identity_changed",
            "Fresh ComicInfo does not prove the selected recovery issue. Review it in Follow-up.",
        )
    ready = _apply_completed_file_recheck(
        file,
        metadata,
        content,
        signature,
        reviewed_series_cv_id=item.cv_id,
    )
    if not ready:
        block = file.diagnostics["source_revalidation"]
        raise ReferencedFileValidationError(str(block["code"]), str(block["reason"]))
    file.diagnostics = {
        **file.diagnostics,
        "source_recheck": {
            **file.diagnostics["source_recheck"],
            "reason": "device_renumbered" if device_changed else "reviewed_issue_assignment",
            "previous_signature": previous,
        },
    }
