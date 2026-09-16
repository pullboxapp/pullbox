"""Database application of proven, read-only series rescan matches."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from pullbox.core.exceptions import ConfigurationError
from pullbox.core.file_ops import register_library_file
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import (
    FileFormat,
    LibraryFile,
    LibraryFileStorageMode,
    LibraryRoot,
    MatchConfidence,
)
from pullbox.utilities.import_guards import active_import_file_mutation_job_id


async def apply_rescan_match(session: Any, series_id: int, item: dict[str, Any]) -> tuple[str, str]:
    """Recheck ownership immediately before registering a proven local file."""
    if item["outcome"] != "register":
        return "review", item["reason"]
    path = Path(item["file_path"])
    try:
        signature = await asyncio.to_thread(build_file_identity_signature, path)
    except (OSError, ConfigurationError):
        return (
            "review",
            "File is unavailable. Check the mount and rescan; ownership was not changed.",
        )
    if signature != item["signature"]:
        return "review", "File changed after inspection. Wait for copying to finish, then rescan."
    with session.no_autoflush:
        existing = list(
            (
                await session.scalars(
                    select(LibraryFile).where(LibraryFile.issue_id == item["issue_id"])
                )
            ).all()
        )
        existing_snapshot = [(record.id, record.file_path) for record in existing]
        for record in existing:
            if record.file_path == str(path):
                continue
            try:
                await asyncio.to_thread(Path(record.file_path).stat)
            except FileNotFoundError:
                continue
            except OSError:
                return (
                    "review",
                    "Existing copy is unreadable. Check the mount; no replacement was made.",
                )
            return "review", "An existing copy is already registered. No file was replaced."
        if len(existing) > 1:
            return (
                "review",
                "Multiple registrations already exist for this issue. Review them before retrying.",
            )
        # Serialize the final ownership checks with other database writers, without
        # holding that write lock during archive inspection or hashing.
        await session.execute(
            update(Issue).where(Issue.id == item["issue_id"]).values(status=Issue.status)
        )
        issue = await session.get(Issue, item["issue_id"], populate_existing=True)
        if issue is None or issue.series_id != series_id:
            return "review", "The catalog issue changed or was removed. Rescan again."
        if (
            issue.issue_number_text != item["target_number"]
            or issue.comicvine_id != item["target_cv_id"]
        ):
            return "review", "The catalog identity changed during inspection. Rescan again."
        if await active_import_file_mutation_job_id(session) is not None:
            return "review", "An import is active. Wait for it to finish, then rescan."
        busy = await session.scalar(
            select(DownloadHistory.id)
            .where(
                DownloadHistory.issue_id == issue.id,
                DownloadHistory.state.not_in([DownloadState.FAILED, DownloadState.IMPORTED]),
                DownloadHistory.imported_at.is_(None),
            )
            .limit(1)
        )
        if busy is not None or issue.status == IssueStatus.DOWNLOADING:
            return (
                "review",
                "This issue has an active download or post-processing task. "
                "Rescan after it finishes.",
            )
        root = await session.get(LibraryRoot, item["root_id"], populate_existing=True)
        if root is None or not root.enabled or not root.allow_referenced_registrations:
            return "review", "The library root no longer allows referencing existing files."
        resolved_root = await asyncio.to_thread(Path(root.path).resolve)
        if not path.is_relative_to(resolved_root):
            return "review", "The library root moved during inspection. Rescan again."
        path_record = await session.scalar(
            select(LibraryFile).where(LibraryFile.file_path == str(path))
        )
        if path_record is not None and path_record.issue_id not in {None, issue.id}:
            return (
                "review",
                "This path is registered to another issue. "
                "Resolve the existing match through Import.",
            )
        # Refresh after taking the writer lock: another registration may have
        # committed while the filesystem checks ran.
        current = list(
            (
                await session.scalars(
                    select(LibraryFile)
                    .where(LibraryFile.issue_id == issue.id)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if [(f.id, f.file_path) for f in current] != existing_snapshot:
            return "review", "Ownership changed during the rescan. Rescan again."
        if path_record is not None:
            if path_record.issue_id is None and current:
                return "review", "This issue already has a registration. Review both file records."
            outcome = (
                "unchanged"
                if issue.status == IssueStatus.OWNED and path_record.issue_id == issue.id
                else "repaired"
            )
            path_record.issue_id = issue.id
            path_record.match_confidence = MatchConfidence.HIGH
            issue.status = IssueStatus.OWNED
            return outcome, "Existing file verified."
        if current:
            record = current[0]
            record.file_path = str(path)
            record.file_name = path.name
            record.file_size = signature["size"]
            record.file_modified_at = datetime.fromtimestamp(int(signature["mtime_ns"]) / 1e9, UTC)
            record.file_format = FileFormat(path.suffix.lower().lstrip("."))
            record.file_hash = None
            record.source_signature = signature
            record.storage_mode = LibraryFileStorageMode.REFERENCED
            record.library_root_id = root.id
            record.match_confidence = MatchConfidence.HIGH
            issue.status = IssueStatus.OWNED
            return (
                "repaired",
                "Missing file link repaired using a proven local match; source left unchanged.",
            )
        await register_library_file(
            session,
            path,
            issue,
            MatchConfidence.HIGH,
            move_to_library=False,
            rename=False,
            normalize_to_cbz=False,
            update_embedded_comicinfo_from_match=False,
            storage_mode=LibraryFileStorageMode.REFERENCED,
            library_root_id=root.id,
            expected_source_signature=item["signature"],
        )
        return "added", "Registered in place. Source file left unchanged."
