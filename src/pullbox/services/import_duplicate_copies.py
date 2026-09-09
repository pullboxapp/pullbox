"""Duplicate-copy detection helpers for import workflows."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries, ImportJob

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class ImportEventLogger(Protocol):
    """Callable contract for writing structured import-job events."""

    def __call__(
        self,
        session: AsyncSession,
        job_id: int,
        level: str,
        event: str,
        message: str | None = None,
        **kwargs: Any,
    ) -> Awaitable[None]: ...


ContentHashFunc = Callable[[str], str | None]
DuplicateSeriesPredicate = Callable[[ImportedSeries | None], bool]
DuplicateTargetKeyFunc = Callable[[ImportedFile], tuple[str, int | float] | None]
FileSortKeyFunc = Callable[[ImportedFile], tuple[int, int, int, int, int]]
NormalizedReleaseNameFunc = Callable[[str], str]


class DuplicateCopyClusterRecorder(Protocol):
    """Callable contract for persisting/logging a duplicate-copy cluster."""

    def __call__(
        self,
        session: AsyncSession,
        job: ImportJob,
        imp_series: ImportedSeries,
        representative: ImportedFile,
        duplicates: list[ImportedFile],
        *,
        duplicate_group_id: int,
        duplicate_reason: str,
        event_name: str,
        message: str,
        target_state: str | None = None,
    ) -> Awaitable[dict[str, Any]]: ...


def compute_content_hash(path_str: str) -> str | None:
    """Hash a file only when a same-target cluster needs stronger confirmation."""
    digest = hashlib.sha256()
    try:
        with Path(path_str).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def mark_duplicate_copy(
    duplicate_file: ImportedFile,
    representative: ImportedFile,
    *,
    duplicate_group_id: int,
    duplicate_reason: str,
    target_state: str | None = None,
) -> None:
    """Turn a redundant file into a durable read-only duplicate-copy row."""
    duplicate_file.status = ImportedFileStatus.DUPLICATE_FILE
    duplicate_file.include_in_import = False
    duplicate_file.conflict_group_id = None
    duplicate_file.is_preferred = False
    duplicate_file.duplicate_group_id = duplicate_group_id
    duplicate_file.duplicate_of_file_id = representative.id
    diagnostics = dict(duplicate_file.diagnostics or {})
    diagnostics.update(
        {
            "kind": "duplicate_copy",
            "duplicate_group_id": duplicate_group_id,
            "duplicate_of_file_id": representative.id,
            "representative_file_name": representative.file_name,
            "duplicate_reason": duplicate_reason,
            "target_state": target_state,
        }
    )
    duplicate_file.diagnostics = diagnostics


async def record_duplicate_copy_cluster(
    session: AsyncSession,
    job: ImportJob,
    imp_series: ImportedSeries,
    representative: ImportedFile,
    duplicates: list[ImportedFile],
    *,
    duplicate_group_id: int,
    duplicate_reason: str,
    event_name: str,
    message: str,
    log_event: ImportEventLogger,
    mark_duplicate: Callable[..., None] = mark_duplicate_copy,
    target_state: str | None = None,
) -> dict[str, Any]:
    """Persist duplicate-copy rows and emit a structured log for the cluster."""
    for duplicate_file in duplicates:
        mark_duplicate(
            duplicate_file,
            representative,
            duplicate_group_id=duplicate_group_id,
            duplicate_reason=duplicate_reason,
            target_state=target_state,
        )

    detail = {
        "kind": "duplicate_copy",
        "duplicate_group_id": duplicate_group_id,
        "duplicate_reason": duplicate_reason,
        "representative_file_id": representative.id,
        "representative_file_name": representative.file_name,
        "target_state": target_state,
        "files": [
            {
                "file_id": representative.id,
                "file_name": representative.file_name,
                "is_representative": True,
            },
            *[
                {
                    "file_id": duplicate_file.id,
                    "file_name": duplicate_file.file_name,
                    "is_representative": False,
                }
                for duplicate_file in duplicates
            ],
        ],
    }
    await log_event(
        session,
        job.id,
        "DEBUG",
        event_name,
        message=message,
        series=imp_series.raw_series_name,
        diagnostics=detail,
    )
    return detail


async def detect_duplicate_copies(
    session: AsyncSession,
    job: ImportJob,
    imp_series: ImportedSeries,
    files: list[ImportedFile],
    duplicate_group_counter: int,
    *,
    log_event: ImportEventLogger,
    is_duplicate_series: DuplicateSeriesPredicate,
    duplicate_target_key: DuplicateTargetKeyFunc,
    normalized_release_name: NormalizedReleaseNameFunc,
    preferred_file_sort_key: FileSortKeyFunc,
    record_cluster: DuplicateCopyClusterRecorder,
    compute_hash: ContentHashFunc = compute_content_hash,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Detect duplicate incoming copies after file matching within one logical series row."""
    duplicate_count = 0
    group_details: list[dict[str, Any]] = []
    candidate_groups: dict[tuple[str, str, int | float], list[ImportedFile]] = {}
    duplicate_series = is_duplicate_series(imp_series)

    for imp_file in files:
        target_key = duplicate_target_key(imp_file)
        if target_key is None:
            continue

        if imp_file.status == ImportedFileStatus.MATCHED:
            candidate_groups.setdefault(("matched", *target_key), []).append(imp_file)
        elif imp_file.status == ImportedFileStatus.ALREADY_OWNED:
            candidate_groups.setdefault(("already_owned", *target_key), []).append(imp_file)
        elif (
            duplicate_series
            and imp_file.status == ImportedFileStatus.NO_MATCH
            and (imp_file.diagnostics or {}).get("target_state") == "no_importable_targets"
        ):
            candidate_groups.setdefault(("informational", *target_key), []).append(imp_file)

    for (group_kind, _target_kind, _target_value), group in candidate_groups.items():
        if len(group) < 2:
            continue

        if group_kind == "matched":
            by_signature: dict[tuple[str, str, int], list[ImportedFile]] = {}
            for imp_file in group:
                signature = (
                    normalized_release_name(imp_file.file_name),
                    (imp_file.file_format or "").lower(),
                    int(imp_file.file_size or 0),
                )
                by_signature.setdefault(signature, []).append(imp_file)

            for duplicate_group in by_signature.values():
                if len(duplicate_group) < 2:
                    continue
                duplicate_group.sort(key=preferred_file_sort_key, reverse=True)
                representative = duplicate_group[0]
                duplicates = duplicate_group[1:]
                duplicate_group_counter += 1
                group_details.append(
                    await record_cluster(
                        session,
                        job,
                        imp_series,
                        representative,
                        duplicates,
                        duplicate_group_id=duplicate_group_counter,
                        duplicate_reason="exact_duplicate",
                        event_name="import_duplicate_copy_exact_cluster",
                        message=(
                            f"Collapsed {len(duplicate_group)} identical copies for "
                            f"{representative.file_name}"
                        ),
                    )
                )
                duplicate_count += len(duplicates)

            remaining_matches = [
                imp_file for imp_file in group if imp_file.status == ImportedFileStatus.MATCHED
            ]
            by_size: dict[int, list[ImportedFile]] = {}
            for imp_file in remaining_matches:
                by_size.setdefault(int(imp_file.file_size or 0), []).append(imp_file)

            for same_size_group in by_size.values():
                if len(same_size_group) < 2:
                    continue
                by_hash: dict[str, list[ImportedFile]] = {}
                for imp_file in same_size_group:
                    if imp_file.content_hash is None:
                        imp_file.content_hash = compute_hash(imp_file.file_path)
                    if imp_file.content_hash:
                        by_hash.setdefault(imp_file.content_hash, []).append(imp_file)

                for hash_group in by_hash.values():
                    if len(hash_group) < 2:
                        continue
                    hash_group.sort(key=preferred_file_sort_key, reverse=True)
                    representative = hash_group[0]
                    duplicates = hash_group[1:]
                    duplicate_group_counter += 1
                    group_details.append(
                        await record_cluster(
                            session,
                            job,
                            imp_series,
                            representative,
                            duplicates,
                            duplicate_group_id=duplicate_group_counter,
                            duplicate_reason="hash_confirmed_duplicate",
                            event_name="import_duplicate_copy_hash_confirmed",
                            message=(
                                f"Hash-confirmed duplicate copies for {representative.file_name}"
                            ),
                        )
                    )
                    duplicate_count += len(duplicates)

            remaining_matches = [
                imp_file for imp_file in group if imp_file.status == ImportedFileStatus.MATCHED
            ]
            if len(remaining_matches) > 1:
                await log_event(
                    session,
                    job.id,
                    "DEBUG",
                    "import_duplicate_variant_conflict",
                    message=(
                        f"Same-issue variant candidates need conflict review in "
                        f"{imp_series.raw_series_name}"
                    ),
                    series=imp_series.raw_series_name,
                    diagnostics={
                        "kind": "same_issue_variant_conflict",
                        "file_ids": [imp_file.id for imp_file in remaining_matches],
                        "file_names": [imp_file.file_name for imp_file in remaining_matches],
                    },
                )
            continue

        duplicate_group_counter += 1
        sorted_group = sorted(group, key=preferred_file_sort_key, reverse=True)
        representative = sorted_group[0]
        duplicates = sorted_group[1:]
        target_state = "already_owned" if group_kind == "already_owned" else "no_importable_targets"
        duplicate_reason = (
            "already_owned_duplicate"
            if group_kind == "already_owned"
            else "informational_duplicate"
        )
        event_name = (
            "import_duplicate_copy_already_owned_cluster"
            if group_kind == "already_owned"
            else "import_duplicate_copy_informational_cluster"
        )
        message = (
            f"Collapsed already-owned duplicate copies for {representative.file_name}"
            if group_kind == "already_owned"
            else f"Collapsed informational duplicate copies for {representative.file_name}"
        )
        group_details.append(
            await record_cluster(
                session,
                job,
                imp_series,
                representative,
                duplicates,
                duplicate_group_id=duplicate_group_counter,
                duplicate_reason=duplicate_reason,
                event_name=event_name,
                message=message,
                target_state=target_state,
            )
        )
        duplicate_count += len(duplicates)

    return duplicate_count, duplicate_group_counter, group_details
