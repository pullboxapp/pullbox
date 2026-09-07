"""Safe, previewed recovery actions for completed collection imports."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Final

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.orm import aliased

from pullbox.core.config_resolver import get_application_secret
from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.core.issue_numbers import parse_issue_number_text
from pullbox.core.name_matcher import NameMatcher
from pullbox.models.audit_log import AuditEventType
from pullbox.models.import_job import (
    ImportControlRequest,
    ImportedFile,
    ImportedFileStatus,
    ImportedSeries,
    ImportJob,
    ImportJobLog,
    ImportJobStatus,
    ImportSeriesStatus,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import LibraryFile, LibraryFileStorageMode
from pullbox.models.series import Series
from pullbox.services.audit_service import AuditService
from pullbox.services.import_counters import recompute_file_counters, recompute_series_counters
from pullbox.services.import_review_actions import apply_safety_allow_once_to_file
from pullbox.services.import_safety_diagnostics import ImportSafetyCategory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_PREVIEW_TOKEN_SALT: Final = "completed-import-cleanup-v1"
_PREVIEW_TOKEN_MAX_AGE_SECONDS: Final = 15 * 60
_PREVIEW_TOKEN_VERSION: Final = 1
_PAGE_SIZE: Final = 500
_EXAMPLE_LIMIT: Final = 3


class CompletedImportCleanupAction(enum.StrEnum):
    """Supported completed-import cleanup operations."""

    DISMISS_MISSING_REFERENCES = "dismiss_missing_references"
    SKIP_PROBABLE_COVERS = "skip_probable_covers"
    SKIP_UNUSABLE_FILES = "skip_unusable_files"
    ALLOW_OVERSIZED_FILES = "allow_oversized_files"
    RETRY_SOURCE_INSPECTION = "retry_source_inspection"
    NORMALIZE_ALREADY_OWNED = "normalize_already_owned"
    ACCEPT_RECOMMENDED_CONFLICTS = "accept_recommended_conflicts"
    RESOLVE_MIXED_FOLDER_FILES = "resolve_mixed_folder_files"


@dataclass(frozen=True, slots=True)
class CompletedImportCleanupSnapshot:
    """Exact identity summary for one previewed cleanup scope."""

    affected_count: int
    affected_file_count: int
    min_file_id: int | None
    max_file_id: int | None
    max_updated_at: str | None
    scope_digest: str


@dataclass(frozen=True, slots=True)
class CompletedImportCleanupPreview:
    """User-facing bounded preview of a completed-import cleanup action."""

    job_id: int
    action: CompletedImportCleanupAction
    affected_count: int
    affected_file_count: int
    item_unit: str
    examples: tuple[str, ...]
    preview_token: str


@dataclass(frozen=True, slots=True)
class CompletedImportCleanupResult:
    """Outcome of a completed-import cleanup action."""

    job_id: int
    action: CompletedImportCleanupAction
    affected_count: int
    affected_file_count: int
    requires_import_retry: bool
    retry_file_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class CompletedImportCleanupFilePage:
    """One bounded page of files in an actionable recovery scope."""

    items: tuple[ImportedFile, ...]
    total: int
    page: int
    page_size: int
    total_pages: int


@dataclass(frozen=True, slots=True)
class CompletedImportCleanupSummary:
    """Counts and examples for one results-page recovery card."""

    affected_count: int
    affected_file_count: int
    examples: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _MixedFolderResolution:
    """One exact, source-preserving mixed-folder ownership correction."""

    file_id: int
    source_import_series_id: int
    source_import_series_name: str
    target_series_id: int
    target_series_title: str
    target_issue_id: int
    target_issue_cv_id: int | None
    target_issue_number: float
    target_issue_number_text: str
    target_library_file_id: int | None
    source_library_file_id: int | None
    source_issue_id: int | None
    source_library_updated_at: str | None
    evidence_source: str
    source_series_name: str
    source_updated_at: str


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_application_secret(), salt=_PREVIEW_TOKEN_SALT)


def _category_expression() -> Any:
    return ImportedFile.diagnostics["safety_block"]["category"].as_string()


def _overrideable_expression() -> Any:
    return ImportedFile.diagnostics["safety_block"]["overrideable"].as_boolean()


def _source_revalidation_category_expression() -> Any:
    return ImportedFile.diagnostics["source_revalidation"]["category"].as_string()


def _source_revalidation_retryable_expression() -> Any:
    return ImportedFile.diagnostics["source_revalidation"]["retryable"].as_boolean()


def _safety_filter(*categories: ImportSafetyCategory) -> Any:
    return _category_expression().in_([category.value for category in categories])


def _candidate_conflict_groups(job_id: int) -> Any:
    """Groups with exactly one high-confidence preferred candidate."""
    candidate = aliased(ImportedFile)
    return (
        select(candidate.conflict_group_id)
        .where(
            candidate.import_job_id == job_id,
            candidate.status == ImportedFileStatus.CONFLICT,
            candidate.conflict_group_id.is_not(None),
            ~exists().where(LibraryFile.issue_id == candidate.matched_issue_id),
        )
        .group_by(candidate.conflict_group_id)
        .having(func.sum(case((candidate.is_preferred.is_(True), 1), else_=0)) == 1)
        .having(
            func.sum(
                case(
                    (
                        candidate.is_preferred.is_(True) & (candidate.match_confidence == "high"),
                        1,
                    ),
                    else_=0,
                )
            )
            == 1
        )
    )


def _fully_recoverable_conflict_series(job_id: int) -> Any:
    """Series whose remaining conflicts are all safe recommended groups."""
    conflict = aliased(ImportedFile)
    candidate_groups = _candidate_conflict_groups(job_id)
    return (
        select(conflict.import_series_id)
        .where(
            conflict.import_job_id == job_id,
            conflict.status == ImportedFileStatus.CONFLICT,
        )
        .group_by(conflict.import_series_id)
        .having(
            func.sum(
                case(
                    (
                        or_(
                            conflict.conflict_group_id.is_(None),
                            ~conflict.conflict_group_id.in_(candidate_groups),
                        ),
                        1,
                    ),
                    else_=0,
                )
            )
            == 0
        )
    )


def _eligible_conflict_groups(job_id: int) -> Any:
    """Recommended groups that can be resumed without stranding sibling conflicts."""
    candidate = aliased(ImportedFile)
    return (
        select(candidate.conflict_group_id)
        .where(
            candidate.import_job_id == job_id,
            candidate.status == ImportedFileStatus.CONFLICT,
            candidate.conflict_group_id.in_(_candidate_conflict_groups(job_id)),
            candidate.import_series_id.in_(_fully_recoverable_conflict_series(job_id)),
        )
        .distinct()
    )


def _file_filters(job_id: int, action: CompletedImportCleanupAction) -> tuple[Any, ...]:
    filters: list[Any] = [ImportedFile.import_job_id == job_id]
    if action is CompletedImportCleanupAction.DISMISS_MISSING_REFERENCES:
        filters.extend(
            [
                ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
                _safety_filter(ImportSafetyCategory.SOURCE_MISSING),
            ]
        )
    elif action is CompletedImportCleanupAction.SKIP_PROBABLE_COVERS:
        filters.extend(
            [
                ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
                _safety_filter(ImportSafetyCategory.SINGLE_PAGE_COMIC),
            ]
        )
    elif action is CompletedImportCleanupAction.SKIP_UNUSABLE_FILES:
        filters.extend(
            [
                ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
                _safety_filter(
                    ImportSafetyCategory.ZERO_BYTE,
                    ImportSafetyCategory.ARCHIVE_NO_PAGES,
                    ImportSafetyCategory.UNSUPPORTED_FILE_TYPE,
                ),
            ]
        )
    elif action is CompletedImportCleanupAction.ALLOW_OVERSIZED_FILES:
        filters.extend(
            [
                ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
                _safety_filter(ImportSafetyCategory.DECOMPRESSION_SIZE_LIMIT),
                _overrideable_expression().is_(True),
            ]
        )
    elif action is CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION:
        retryable_categories = [
            category.value
            for category in (
                ImportSafetyCategory.PERMISSION_UNREADABLE,
                ImportSafetyCategory.ARCHIVE_INSPECTION_FAILED,
                ImportSafetyCategory.SOURCE_CHANGED,
            )
        ]
        filters.append(
            or_(
                and_(
                    ImportedFile.status == ImportedFileStatus.SAFETY_BLOCKED,
                    _category_expression().in_(retryable_categories),
                ),
                and_(
                    ImportedFile.status == ImportedFileStatus.FAILED,
                    _source_revalidation_category_expression().in_(retryable_categories),
                    _source_revalidation_retryable_expression().is_(True),
                ),
            )
        )
    elif action is CompletedImportCleanupAction.NORMALIZE_ALREADY_OWNED:
        filters.extend(
            [
                ImportedFile.status == ImportedFileStatus.CONFLICT,
                ImportedFile.matched_issue_id.is_not(None),
                exists().where(LibraryFile.issue_id == ImportedFile.matched_issue_id),
            ]
        )
    elif action is CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS:
        filters.extend(
            [
                ImportedFile.status == ImportedFileStatus.CONFLICT,
                ImportedFile.conflict_group_id.in_(_eligible_conflict_groups(job_id)),
            ]
        )
    elif action is CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES:
        filters.append(
            ImportedFile.status.in_((ImportedFileStatus.NO_MATCH, ImportedFileStatus.IMPORTED))
        )
    else:  # pragma: no cover - exhaustive enum guard
        raise ValidationError("Unsupported completed-import cleanup action.")
    return tuple(filters)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _safe_int(value: object) -> int | None:
    if not isinstance(value, str | bytes | bytearray | int | float):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mixed_folder_source_identity(
    imported_file: ImportedFile,
) -> tuple[str, str | float | int, int | None, int | None, str] | None:
    """Return only embedded or sidecar identity strong enough for bulk correction."""
    diagnostics = _mapping(imported_file.diagnostics)
    signals = _mapping(diagnostics.get("metadata_signals"))
    series_signal = str(signals.get("series_name") or "")
    issue_signal = str(signals.get("issue_number") or "")
    trusted_signals = {"comicinfo", "sidecar"}
    if series_signal not in trusted_signals or issue_signal not in trusted_signals:
        return None

    source_metadata = _mapping(diagnostics.get("source_metadata"))
    comicinfo = _mapping(source_metadata.get("comicinfo"))
    if series_signal == "comicinfo":
        source_series_name = str(comicinfo.get("series") or "").strip()
    else:
        source_series_name = str(imported_file.parsed_series or "").strip()
    if issue_signal == "comicinfo":
        source_issue_number = comicinfo.get("number")
    else:
        source_issue_number = imported_file.issue_number_raw or imported_file.parsed_issue_number
    if not source_series_name or not isinstance(source_issue_number, str | float | int):
        return None

    trusted_series_cv_id = (
        _safe_int(diagnostics.get("comicvine_series_id"))
        if str(signals.get("comicvine_series_id") or "") in trusted_signals
        else None
    )
    trusted_issue_cv_id = (
        imported_file.comicvine_issue_id
        if str(signals.get("comicvine_issue_id") or "") in trusted_signals
        else None
    )
    return (
        source_series_name,
        source_issue_number,
        trusted_series_cv_id,
        trusted_issue_cv_id,
        series_signal,
    )


async def _load_mixed_folder_resolutions(
    session: AsyncSession,
    job_id: int,
) -> tuple[_MixedFolderResolution, ...]:
    """Resolve exact local targets without provider calls or source-file access."""
    source_rows = (
        await session.execute(
            select(ImportedFile, ImportedSeries)
            .join(ImportedSeries, ImportedSeries.id == ImportedFile.import_series_id)
            .where(
                ImportedFile.import_job_id == job_id,
                ImportedFile.status.in_((ImportedFileStatus.NO_MATCH, ImportedFileStatus.IMPORTED)),
            )
            .order_by(ImportedFile.id)
        )
    ).all()
    if not source_rows:
        return ()

    current_library_by_file_id: dict[int, LibraryFile] = {}
    imported_library_ids = {
        int(imported_file.library_file_id)
        for imported_file, _imported_series in source_rows
        if imported_file.status is ImportedFileStatus.IMPORTED
        and imported_file.library_file_id is not None
    }
    if imported_library_ids:
        current_library_by_id = {
            int(library_file.id): library_file
            for library_file in (
                await session.scalars(
                    select(LibraryFile).where(LibraryFile.id.in_(imported_library_ids))
                )
            ).all()
        }
        for imported_file, _imported_series in source_rows:
            if imported_file.library_file_id is None:
                continue
            library_file = current_library_by_id.get(int(imported_file.library_file_id))
            if (
                library_file is not None
                and library_file.storage_mode is LibraryFileStorageMode.REFERENCED
                and library_file.file_path == imported_file.file_path
                and library_file.issue_id == imported_file.matched_issue_id
            ):
                current_library_by_file_id[int(imported_file.id)] = library_file

    source_candidates: list[
        tuple[ImportedFile, ImportedSeries, str, str, int | None, int | None, str]
    ] = []
    normalized_titles: set[str] = set()
    trusted_series_cv_ids: set[int] = set()
    for imported_file, imported_series in source_rows:
        if (
            imported_file.status is ImportedFileStatus.IMPORTED
            and int(imported_file.id) not in current_library_by_file_id
        ):
            continue
        identity = _mixed_folder_source_identity(imported_file)
        if identity is None:
            continue
        source_title, raw_number, series_cv_id, issue_cv_id, evidence_source = identity
        normalized_source_title = NameMatcher.normalize(source_title)
        parent_title = imported_series.cv_title or imported_series.raw_series_name
        if not normalized_source_title or normalized_source_title == NameMatcher.normalize(
            parent_title
        ):
            continue
        try:
            _numeric_number, exact_number = parse_issue_number_text(raw_number)
        except ValueError:
            continue
        source_candidates.append(
            (
                imported_file,
                imported_series,
                source_title,
                exact_number,
                series_cv_id,
                issue_cv_id,
                evidence_source,
            )
        )
        normalized_titles.add(normalized_source_title)
        if series_cv_id is not None:
            trusted_series_cv_ids.add(series_cv_id)
    if not source_candidates:
        return ()

    local_series = list((await session.scalars(select(Series))).all())
    series_by_cv_id = {
        int(series.comicvine_id): series
        for series in local_series
        if series.comicvine_id is not None and int(series.comicvine_id) in trusted_series_cv_ids
    }
    series_by_title: dict[str, list[Series]] = {}
    for series in local_series:
        normalized = NameMatcher.normalize(series.title)
        if normalized in normalized_titles:
            series_by_title.setdefault(normalized, []).append(series)

    candidate_targets: list[
        tuple[ImportedFile, ImportedSeries, str, str, str, Series, int | None]
    ] = []
    target_series_ids: set[int] = set()
    for (
        imported_file,
        imported_series,
        source_title,
        exact_number,
        series_cv_id,
        issue_cv_id,
        evidence_source,
    ) in source_candidates:
        target_series = series_by_cv_id.get(series_cv_id) if series_cv_id is not None else None
        if target_series is None:
            title_matches = series_by_title.get(NameMatcher.normalize(source_title), [])
            if len(title_matches) != 1:
                continue
            target_series = title_matches[0]
        if imported_series.series_id == target_series.id:
            continue
        candidate_targets.append(
            (
                imported_file,
                imported_series,
                source_title,
                exact_number,
                evidence_source,
                target_series,
                issue_cv_id,
            )
        )
        target_series_ids.add(int(target_series.id))
    if not candidate_targets:
        return ()

    target_issues = list(
        (
            await session.scalars(
                select(Issue).where(Issue.series_id.in_(sorted(target_series_ids)))
            )
        ).all()
    )
    issues_by_cv_id = {
        int(issue.comicvine_id): issue for issue in target_issues if issue.comicvine_id is not None
    }
    issues_by_number: dict[tuple[int, str], list[Issue]] = {}
    for issue in target_issues:
        issues_by_number.setdefault(
            (int(issue.series_id), issue.effective_issue_number_text), []
        ).append(issue)

    resolved_targets: list[tuple[ImportedFile, ImportedSeries, str, str, Series, Issue]] = []
    for (
        imported_file,
        imported_series,
        source_title,
        exact_number,
        evidence_source,
        target_series,
        issue_cv_id,
    ) in candidate_targets:
        target_issue: Issue | None = (
            issues_by_cv_id.get(issue_cv_id) if issue_cv_id is not None else None
        )
        if target_issue is not None and target_issue.series_id != target_series.id:
            target_issue = None
        if target_issue is None:
            number_matches = issues_by_number.get((int(target_series.id), exact_number), [])
            if len(number_matches) != 1:
                continue
            target_issue = number_matches[0]
        resolved_targets.append(
            (
                imported_file,
                imported_series,
                source_title,
                evidence_source,
                target_series,
                target_issue,
            )
        )
    if not resolved_targets:
        return ()

    target_issue_ids = {int(item[5].id) for item in resolved_targets}
    owned_files_by_issue_id: dict[int, list[int]] = {}
    for library_file in (
        await session.scalars(
            select(LibraryFile).where(LibraryFile.issue_id.in_(sorted(target_issue_ids)))
        )
    ).all():
        if library_file.issue_id is not None:
            owned_files_by_issue_id.setdefault(int(library_file.issue_id), []).append(
                int(library_file.id)
            )
    files_by_target_issue: dict[int, list[int]] = {}
    for imported_file, _series, _title, _source, _target_series, issue in resolved_targets:
        files_by_target_issue.setdefault(int(issue.id), []).append(int(imported_file.id))

    resolutions: list[_MixedFolderResolution] = []
    for (
        imported_file,
        imported_series,
        source_title,
        evidence_source,
        target_series,
        issue,
    ) in resolved_targets:
        existing_library_file_ids = owned_files_by_issue_id.get(int(issue.id), [])
        if len(existing_library_file_ids) > 1:
            continue
        library_file_id = existing_library_file_ids[0] if existing_library_file_ids else None
        if library_file_id is None and len(files_by_target_issue[int(issue.id)]) != 1:
            continue
        current_library_file = current_library_by_file_id.get(int(imported_file.id))
        resolutions.append(
            _MixedFolderResolution(
                file_id=int(imported_file.id),
                source_import_series_id=int(imported_series.id),
                source_import_series_name=imported_series.raw_series_name,
                target_series_id=int(target_series.id),
                target_series_title=target_series.title,
                target_issue_id=int(issue.id),
                target_issue_cv_id=(
                    int(issue.comicvine_id) if issue.comicvine_id is not None else None
                ),
                target_issue_number=float(issue.issue_number),
                target_issue_number_text=issue.effective_issue_number_text,
                target_library_file_id=library_file_id,
                source_library_file_id=(
                    int(current_library_file.id) if current_library_file is not None else None
                ),
                source_issue_id=(
                    int(current_library_file.issue_id)
                    if current_library_file is not None
                    and current_library_file.issue_id is not None
                    else None
                ),
                source_library_updated_at=(
                    current_library_file.updated_at.isoformat(timespec="microseconds")
                    if current_library_file is not None
                    else None
                ),
                evidence_source=evidence_source,
                source_series_name=source_title,
                source_updated_at=imported_file.updated_at.isoformat(timespec="microseconds"),
            )
        )
    return tuple(resolutions)


async def _load_completed_job(session: AsyncSession, job_id: int) -> ImportJob:
    job = await session.get(ImportJob, job_id, populate_existing=True)
    if job is None:
        raise NotFoundError("ImportJob", job_id)
    if job.status is not ImportJobStatus.COMPLETED:
        raise ValidationError("Job must be in COMPLETED state for recovery cleanup")
    if job.control_request is not ImportControlRequest.NONE:
        raise ValidationError("The import job has a pending control request")
    if job.archived_at is not None:
        raise ValidationError("Archived import jobs must be restored before cleanup")
    return job


def _safe_example_name(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    leaf = normalized.rsplit("/", maxsplit=1)[-1]
    safe_leaf = "".join(character for character in leaf if character >= " " and character != "\x7f")
    return (safe_leaf or "File")[:200]


async def _load_snapshot(
    session: AsyncSession,
    job_id: int,
    action: CompletedImportCleanupAction,
) -> CompletedImportCleanupSnapshot:
    if action is CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES:
        resolutions = await _load_mixed_folder_resolutions(session, job_id)
        digest = sha256()
        for resolution in resolutions:
            digest.update(
                (
                    f"{resolution.file_id}|{resolution.target_series_id}|"
                    f"{resolution.target_issue_id}|{resolution.target_library_file_id or 0}|"
                    f"{resolution.source_library_file_id or 0}|"
                    f"{resolution.source_library_updated_at or ''}|"
                    f"{resolution.source_updated_at}\n"
                ).encode()
            )
        file_ids = [resolution.file_id for resolution in resolutions]
        return CompletedImportCleanupSnapshot(
            affected_count=len(resolutions),
            affected_file_count=len(resolutions),
            min_file_id=min(file_ids) if file_ids else None,
            max_file_id=max(file_ids) if file_ids else None,
            max_updated_at=max(
                (resolution.source_updated_at for resolution in resolutions),
                default=None,
            ),
            scope_digest=digest.hexdigest(),
        )
    filters = _file_filters(job_id, action)
    aggregate = (
        await session.execute(
            select(
                func.count(ImportedFile.id),
                func.min(ImportedFile.id),
                func.max(ImportedFile.id),
                func.max(ImportedFile.updated_at),
            ).where(*filters)
        )
    ).one()
    file_count = int(aggregate[0] or 0)
    if file_count == 0:
        return CompletedImportCleanupSnapshot(0, 0, None, None, None, sha256().hexdigest())

    digest = sha256()
    group_ids: set[int] = set()
    result = await session.stream(
        select(
            ImportedFile.id,
            ImportedFile.conflict_group_id,
            ImportedFile.updated_at,
        )
        .where(*filters)
        .order_by(ImportedFile.id)
        .execution_options(yield_per=20_000)
    )
    try:
        async for rows in result.partitions(20_000):
            for file_id, conflict_group_id, updated_at in rows:
                digest_line = (
                    f"{int(file_id)}|{int(conflict_group_id or 0)}|{updated_at.isoformat()}\n"
                )
                digest.update(digest_line.encode())
                if conflict_group_id is not None:
                    group_ids.add(int(conflict_group_id))
    finally:
        await result.close()
    affected_count = (
        len(group_ids)
        if action is CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS
        else file_count
    )
    return CompletedImportCleanupSnapshot(
        affected_count=affected_count,
        affected_file_count=file_count,
        min_file_id=int(aggregate[1]),
        max_file_id=int(aggregate[2]),
        max_updated_at=aggregate[3].isoformat(timespec="microseconds"),
        scope_digest=digest.hexdigest(),
    )


async def count_completed_import_cleanup_scope(
    session: AsyncSession,
    job_id: int,
    action: CompletedImportCleanupAction,
) -> tuple[int, int]:
    """Return action and file counts without hashing the full preview scope."""
    if action is CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES:
        count = len(await _load_mixed_folder_resolutions(session, job_id))
        return count, count
    filters = _file_filters(job_id, action)
    file_count = int(
        (await session.scalar(select(func.count(ImportedFile.id)).where(*filters))) or 0
    )
    if action is not CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS:
        return file_count, file_count
    group_count = int(
        (
            await session.scalar(
                select(func.count(func.distinct(ImportedFile.conflict_group_id))).where(*filters)
            )
        )
        or 0
    )
    return group_count, file_count


async def list_completed_import_cleanup_files(
    session: AsyncSession,
    job_id: int,
    action: CompletedImportCleanupAction,
    *,
    page: int = 1,
    page_size: int = 25,
) -> CompletedImportCleanupFilePage:
    """Return a bounded, deterministic page for user review."""
    await _load_completed_job(session, job_id)
    normalized_page = max(1, int(page))
    normalized_page_size = min(max(1, int(page_size)), 100)
    if action is CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES:
        resolutions = await _load_mixed_folder_resolutions(session, job_id)
        eligible_file_ids = [resolution.file_id for resolution in resolutions]
        total = len(eligible_file_ids)
        total_pages = max(1, (total + normalized_page_size - 1) // normalized_page_size)
        normalized_page = min(normalized_page, total_pages)
        page_file_ids = eligible_file_ids[
            (normalized_page - 1) * normalized_page_size : normalized_page * normalized_page_size
        ]
        items_by_id = {
            int(item.id): item
            for item in (
                await session.scalars(
                    select(ImportedFile).where(ImportedFile.id.in_(page_file_ids))
                )
            ).all()
        }
        items = tuple(items_by_id[file_id] for file_id in page_file_ids)
        return CompletedImportCleanupFilePage(
            items=items,
            total=total,
            page=normalized_page,
            page_size=normalized_page_size,
            total_pages=total_pages,
        )
    filters = _file_filters(job_id, action)
    total = int((await session.scalar(select(func.count(ImportedFile.id)).where(*filters))) or 0)
    total_pages = max(1, (total + normalized_page_size - 1) // normalized_page_size)
    normalized_page = min(normalized_page, total_pages)
    items = tuple(
        (
            await session.scalars(
                select(ImportedFile)
                .where(*filters)
                .order_by(ImportedFile.id)
                .offset((normalized_page - 1) * normalized_page_size)
                .limit(normalized_page_size)
            )
        ).all()
    )
    return CompletedImportCleanupFilePage(
        items=items,
        total=total,
        page=normalized_page,
        page_size=normalized_page_size,
        total_pages=total_pages,
    )


async def list_completed_import_cleanup_examples(
    session: AsyncSession,
    job_id: int,
    action: CompletedImportCleanupAction,
    *,
    limit: int = _EXAMPLE_LIMIT,
) -> tuple[str, ...]:
    """Return sanitized example filenames without hydrating the full scope."""
    if action is CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES:
        resolutions = await _load_mixed_folder_resolutions(session, job_id)
        file_ids = [resolution.file_id for resolution in resolutions[:limit]]
        names_by_id = {
            int(file_id): file_name
            for file_id, file_name in (
                await session.execute(
                    select(ImportedFile.id, ImportedFile.file_name).where(
                        ImportedFile.id.in_(file_ids)
                    )
                )
            ).all()
        }
        return tuple(_safe_example_name(names_by_id[file_id]) for file_id in file_ids)
    names = (
        await session.scalars(
            select(ImportedFile.file_name)
            .where(*_file_filters(job_id, action))
            .order_by(ImportedFile.id)
            .limit(min(max(1, int(limit)), 10))
        )
    ).all()
    return tuple(_safe_example_name(name) for name in names)


async def summarize_completed_import_cleanup_scope(
    session: AsyncSession,
    job_id: int,
    action: CompletedImportCleanupAction,
    *,
    example_limit: int = _EXAMPLE_LIMIT,
) -> CompletedImportCleanupSummary:
    """Load a recovery-card summary without resolving mixed folders twice."""
    normalized_limit = min(max(1, int(example_limit)), 10)
    if action is not CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES:
        affected_count, affected_file_count = await count_completed_import_cleanup_scope(
            session,
            job_id,
            action,
        )
        examples = (
            await list_completed_import_cleanup_examples(
                session,
                job_id,
                action,
                limit=normalized_limit,
            )
            if affected_count
            else ()
        )
        return CompletedImportCleanupSummary(
            affected_count=affected_count,
            affected_file_count=affected_file_count,
            examples=examples,
        )

    resolutions = await _load_mixed_folder_resolutions(session, job_id)
    file_ids = [resolution.file_id for resolution in resolutions[:normalized_limit]]
    names_by_id = {
        int(file_id): file_name
        for file_id, file_name in (
            await session.execute(
                select(ImportedFile.id, ImportedFile.file_name).where(ImportedFile.id.in_(file_ids))
            )
        ).all()
    }
    return CompletedImportCleanupSummary(
        affected_count=len(resolutions),
        affected_file_count=len(resolutions),
        examples=tuple(_safe_example_name(names_by_id[file_id]) for file_id in file_ids),
    )


def _snapshot_payload(snapshot: CompletedImportCleanupSnapshot) -> dict[str, object]:
    return {
        "affected_count": snapshot.affected_count,
        "affected_file_count": snapshot.affected_file_count,
        "min_file_id": snapshot.min_file_id,
        "max_file_id": snapshot.max_file_id,
        "max_updated_at": snapshot.max_updated_at,
        "scope_digest": snapshot.scope_digest,
    }


def _load_token(token: str) -> Mapping[str, object]:
    try:
        payload = _serializer().loads(token, max_age=_PREVIEW_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired as exc:
        raise ValidationError("The cleanup preview expired. Preview the action again.") from exc
    except BadSignature as exc:
        raise ValidationError("The cleanup preview is invalid. Preview the action again.") from exc
    if not isinstance(payload, Mapping):
        raise ValidationError("The cleanup preview is invalid. Preview the action again.")
    return payload


def _snapshot_from_payload(payload: Mapping[str, object]) -> CompletedImportCleanupSnapshot:
    raw = payload.get("snapshot")
    if not isinstance(raw, Mapping):
        raise ValidationError("The cleanup preview is invalid. Preview the action again.")
    try:
        affected_count = int(raw["affected_count"])
        affected_file_count = int(raw["affected_file_count"])
        min_file_id = int(raw["min_file_id"]) if raw.get("min_file_id") is not None else None
        max_file_id = int(raw["max_file_id"]) if raw.get("max_file_id") is not None else None
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("The cleanup preview is invalid. Preview the action again.") from exc
    max_updated_at = raw.get("max_updated_at")
    scope_digest = raw.get("scope_digest")
    if not isinstance(max_updated_at, (str, type(None))) or not isinstance(scope_digest, str):
        raise ValidationError("The cleanup preview is invalid. Preview the action again.")
    return CompletedImportCleanupSnapshot(
        affected_count,
        affected_file_count,
        min_file_id,
        max_file_id,
        max_updated_at,
        scope_digest,
    )


async def preview_completed_import_cleanup(
    session: AsyncSession,
    job_id: int,
    action: CompletedImportCleanupAction,
    *,
    actor_id: int,
) -> CompletedImportCleanupPreview:
    """Return a bounded preview and actor-bound confirmation token."""
    job = await _load_completed_job(session, job_id)
    snapshot = await _load_snapshot(session, job_id, action)
    if snapshot.affected_count == 0:
        raise ValidationError("No files are eligible for this cleanup action.")
    examples = await list_completed_import_cleanup_examples(
        session,
        job_id,
        action,
    )
    token = str(
        _serializer().dumps(
            {
                "version": _PREVIEW_TOKEN_VERSION,
                "job_id": job.id,
                "action": action.value,
                "actor_id": actor_id,
                "snapshot": _snapshot_payload(snapshot),
            }
        )
    )
    return CompletedImportCleanupPreview(
        job_id=job.id,
        action=action,
        affected_count=snapshot.affected_count,
        affected_file_count=snapshot.affected_file_count,
        item_unit=(
            "group"
            if action is CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS
            else "file"
        ),
        examples=examples,
        preview_token=token,
    )


def _validate_token(
    token: str,
    *,
    job_id: int,
    action: CompletedImportCleanupAction,
    actor_id: int,
) -> CompletedImportCleanupSnapshot:
    payload = _load_token(token)
    if (
        payload.get("version") != _PREVIEW_TOKEN_VERSION
        or payload.get("job_id") != job_id
        or payload.get("action") != action.value
        or payload.get("actor_id") != actor_id
    ):
        raise ValidationError("The cleanup preview does not match this job and action.")
    return _snapshot_from_payload(payload)


def _mark_skipped(file: ImportedFile, *, action: CompletedImportCleanupAction) -> None:
    diagnostics = dict(file.diagnostics or {})
    diagnostics["completed_import_cleanup"] = {
        "action": action.value,
        "resolved_at": datetime.now(UTC).isoformat(),
        "source_preserved": True,
    }
    file.status = ImportedFileStatus.SKIPPED
    file.include_in_import = False
    file.error_message = None
    file.match_method = "completed_import_cleanup"
    file.diagnostics = diagnostics


def _prepare_source_retry(file: ImportedFile) -> None:
    diagnostics = dict(file.diagnostics or {})
    raw_block = diagnostics.get("safety_block")
    raw_revalidation = diagnostics.get("source_revalidation")
    if isinstance(raw_block, Mapping):
        retry_evidence = dict(raw_block)
        diagnostics.pop("safety_block", None)
    elif isinstance(raw_revalidation, Mapping) and raw_revalidation.get("retryable") is True:
        retry_evidence = dict(raw_revalidation)
    else:
        raise ValidationError("A selected source failure no longer has safety evidence.")
    diagnostics["source_revalidation"] = {
        **retry_evidence,
        "kind": "source_revalidation",
        "retryable": True,
        "source": "completed_import_cleanup",
    }
    file.status = ImportedFileStatus.FAILED
    file.include_in_import = False
    file.diagnostics = diagnostics


async def _apply_file_action(
    session: AsyncSession,
    job: ImportJob,
    action: CompletedImportCleanupAction,
) -> tuple[set[int], tuple[int, ...], bool]:
    affected_series_ids: set[int] = set()
    affected_file_ids: list[int] = []
    requires_import_retry = False
    cursor = 0
    while True:
        files = list(
            (
                await session.scalars(
                    select(ImportedFile)
                    .where(*_file_filters(job.id, action), ImportedFile.id > cursor)
                    .order_by(ImportedFile.id)
                    .limit(_PAGE_SIZE)
                )
            ).all()
        )
        if not files:
            break
        cursor = int(files[-1].id)
        for file in files:
            affected_series_ids.add(int(file.import_series_id))
            affected_file_ids.append(int(file.id))
            if action in {
                CompletedImportCleanupAction.DISMISS_MISSING_REFERENCES,
                CompletedImportCleanupAction.SKIP_PROBABLE_COVERS,
                CompletedImportCleanupAction.SKIP_UNUSABLE_FILES,
            }:
                _mark_skipped(file, action=action)
            elif action is CompletedImportCleanupAction.ALLOW_OVERSIZED_FILES:
                apply_safety_allow_once_to_file(file, retry_import=True)
                requires_import_retry = True
            elif action is CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION:
                _prepare_source_retry(file)
                requires_import_retry = True
            elif action is CompletedImportCleanupAction.NORMALIZE_ALREADY_OWNED:
                file.status = ImportedFileStatus.ALREADY_OWNED
                file.include_in_import = False
                file.error_message = None
            else:  # pragma: no cover - conflict groups use a separate path
                raise ValidationError("Unsupported file cleanup action.")
        await session.flush()
    return affected_series_ids, tuple(affected_file_ids), requires_import_retry


async def _apply_recommended_conflicts(
    session: AsyncSession,
    job: ImportJob,
) -> set[int]:
    eligible_group_ids = [
        int(group_id)
        for group_id in (
            await session.scalars(
                select(ImportedFile.conflict_group_id)
                .where(
                    ImportedFile.import_job_id == job.id,
                    ImportedFile.status == ImportedFileStatus.CONFLICT,
                    ImportedFile.conflict_group_id.in_(_eligible_conflict_groups(job.id)),
                )
                .distinct()
                .order_by(ImportedFile.conflict_group_id)
            )
        ).all()
        if group_id is not None
    ]
    if not eligible_group_ids:
        return set()
    affected_series_ids = set(
        await session.scalars(
            select(ImportedFile.import_series_id)
            .where(
                ImportedFile.import_job_id == job.id,
                ImportedFile.status == ImportedFileStatus.CONFLICT,
                ImportedFile.conflict_group_id.in_(eligible_group_ids),
            )
            .distinct()
        )
    )
    await session.execute(
        update(ImportedFile)
        .where(
            ImportedFile.import_job_id == job.id,
            ImportedFile.status == ImportedFileStatus.CONFLICT,
            ImportedFile.conflict_group_id.in_(eligible_group_ids),
            ImportedFile.is_preferred.is_(False),
        )
        .values(status=ImportedFileStatus.SKIPPED, include_in_import=False)
    )
    await session.execute(
        update(ImportedFile)
        .where(
            ImportedFile.import_job_id == job.id,
            ImportedFile.status == ImportedFileStatus.CONFLICT,
            ImportedFile.conflict_group_id.in_(eligible_group_ids),
            ImportedFile.is_preferred.is_(True),
            ImportedFile.match_confidence == "high",
        )
        .values(status=ImportedFileStatus.CONFIRMED, include_in_import=True)
    )
    await session.flush()
    return {int(series_id) for series_id in affected_series_ids}


async def _apply_mixed_folder_resolutions(
    session: AsyncSession,
    job: ImportJob,
) -> tuple[set[int], set[int]]:
    """Rebucket exact embedded identities while preserving every source artifact."""
    resolutions = await _load_mixed_folder_resolutions(session, int(job.id))
    if not resolutions:
        return set(), set()

    target_series_ids = {resolution.target_series_id for resolution in resolutions}
    target_series_by_id = {
        int(series.id): series
        for series in (
            await session.scalars(select(Series).where(Series.id.in_(target_series_ids)))
        ).all()
    }
    target_issue_by_id = {
        int(issue.id): issue
        for issue in (
            await session.scalars(
                select(Issue).where(
                    Issue.id.in_({resolution.target_issue_id for resolution in resolutions})
                )
            )
        ).all()
    }
    source_library_by_id = {
        int(library_file.id): library_file
        for library_file in (
            await session.scalars(
                select(LibraryFile).where(
                    LibraryFile.id.in_(
                        {
                            resolution.source_library_file_id
                            for resolution in resolutions
                            if resolution.source_library_file_id is not None
                        }
                    )
                )
            )
        ).all()
    }
    source_issue_by_id = {
        int(issue.id): issue
        for issue in (
            await session.scalars(
                select(Issue).where(
                    Issue.id.in_(
                        {
                            resolution.source_issue_id
                            for resolution in resolutions
                            if resolution.source_issue_id is not None
                        }
                    )
                )
            )
        ).all()
    }
    source_series_by_id = {
        int(series.id): series
        for series in (
            await session.scalars(
                select(Series).where(
                    Series.id.in_({issue.series_id for issue in source_issue_by_id.values()})
                )
            )
        ).all()
    }
    source_issue_owner_counts = {
        int(issue_id): int(owner_count)
        for issue_id, owner_count in (
            await session.execute(
                select(LibraryFile.issue_id, func.count(LibraryFile.id))
                .where(LibraryFile.issue_id.in_(set(source_issue_by_id)))
                .group_by(LibraryFile.issue_id)
            )
        ).all()
        if issue_id is not None
    }
    imported_target_by_series_id: dict[int, ImportedSeries] = {}
    existing_target_rows = list(
        (
            await session.scalars(
                select(ImportedSeries)
                .where(
                    ImportedSeries.import_job_id == job.id,
                    ImportedSeries.series_id.in_(target_series_ids),
                )
                .order_by(ImportedSeries.id)
            )
        ).all()
    )
    for imported_series in existing_target_rows:
        if imported_series.series_id is not None:
            imported_target_by_series_id.setdefault(int(imported_series.series_id), imported_series)

    affected_series_ids: set[int] = set()
    retry_series_ids: set[int] = set()
    for resolution in resolutions:
        target_import_series = imported_target_by_series_id.get(resolution.target_series_id)
        if target_import_series is None:
            target_series = target_series_by_id[resolution.target_series_id]
            target_import_series = ImportedSeries(
                import_job_id=job.id,
                raw_series_name=target_series.title,
                raw_year=target_series.year_start,
                file_count=0,
                sample_paths=[],
                has_files=True,
                cv_id=target_series.comicvine_id,
                cv_title=target_series.title,
                cv_year=target_series.year_start,
                cv_match_score=1.0,
                cv_match_method="completed_import_mixed_folder_recovery",
                status=ImportSeriesStatus.DUPLICATE,
                selected_for_import=False,
                series_id=target_series.id,
                diagnostics={
                    "kind": "completed_import_mixed_folder_recovery",
                    "existing_series_id": target_series.id,
                    "source_preserved": True,
                },
            )
            session.add(target_import_series)
            await session.flush()
            imported_target_by_series_id[resolution.target_series_id] = target_import_series

        imported_file = await session.get(ImportedFile, resolution.file_id)
        if imported_file is None:  # pragma: no cover - signed snapshot guards deletion
            raise ValidationError("A mixed-folder file disappeared. Preview the action again.")
        diagnostics = dict(imported_file.diagnostics or {})
        diagnostics["completed_import_cleanup"] = {
            "action": CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES.value,
            "resolved_at": datetime.now(UTC).isoformat(),
            "source_preserved": True,
            "evidence_source": resolution.evidence_source,
            "source_import_series_id": resolution.source_import_series_id,
            "source_import_series_name": resolution.source_import_series_name,
            "source_series_name": resolution.source_series_name,
            "target_import_series_id": target_import_series.id,
            "target_series_id": resolution.target_series_id,
            "target_series_title": resolution.target_series_title,
            "target_issue_id": resolution.target_issue_id,
            "target_issue_number": resolution.target_issue_number_text,
        }
        imported_file.import_series_id = int(target_import_series.id)
        imported_file.parsed_series = resolution.target_series_title
        imported_file.parsed_issue_number = resolution.target_issue_number
        imported_file.issue_number_raw = resolution.target_issue_number_text
        imported_file.matched_issue_id = resolution.target_issue_id
        imported_file.matched_issue_cv_id = resolution.target_issue_cv_id
        imported_file.match_confidence = "high"
        imported_file.match_method = "completed_import_metadata_reassignment"
        imported_file.conflict_group_id = None
        imported_file.duplicate_group_id = None
        imported_file.duplicate_of_file_id = None
        imported_file.is_preferred = False
        imported_file.error_message = None
        imported_file.diagnostics = diagnostics
        source_library_file = (
            source_library_by_id.get(resolution.source_library_file_id)
            if resolution.source_library_file_id is not None
            else None
        )
        target_issue = target_issue_by_id[resolution.target_issue_id]
        if source_library_file is not None:
            source_issue_id_before = source_library_file.issue_id
            previous_issue = (
                source_issue_by_id.get(resolution.source_issue_id)
                if resolution.source_issue_id is not None
                else None
            )
            if source_library_file.issue_id == resolution.target_issue_id:
                imported_file.status = ImportedFileStatus.IMPORTED
                imported_file.include_in_import = False
                imported_file.library_file_id = source_library_file.id
                target_issue.status = IssueStatus.OWNED
            elif (
                resolution.target_library_file_id is not None
                and resolution.target_library_file_id != source_library_file.id
            ):
                imported_file.status = ImportedFileStatus.ALREADY_OWNED
                imported_file.include_in_import = False
                imported_file.library_file_id = resolution.target_library_file_id
                await session.delete(source_library_file)
                target_issue.status = IssueStatus.OWNED
            else:
                source_library_file.issue_id = resolution.target_issue_id
                imported_file.status = ImportedFileStatus.IMPORTED
                imported_file.include_in_import = False
                imported_file.library_file_id = source_library_file.id
                target_issue.status = IssueStatus.OWNED
            if previous_issue is not None and previous_issue.id != resolution.target_issue_id:
                previous_series = source_series_by_id.get(int(previous_issue.series_id))
                remaining_owners = source_issue_owner_counts.get(int(previous_issue.id), 0)
                if source_issue_id_before == previous_issue.id:
                    remaining_owners = max(0, remaining_owners - 1)
                    source_issue_owner_counts[int(previous_issue.id)] = remaining_owners
                if remaining_owners:
                    previous_issue.status = IssueStatus.OWNED
                else:
                    previous_issue.status = (
                        IssueStatus.WANTED
                        if previous_series is not None and previous_series.monitored
                        else IssueStatus.SKIPPED
                    )
        elif resolution.target_library_file_id is not None:
            imported_file.status = ImportedFileStatus.ALREADY_OWNED
            imported_file.include_in_import = False
            imported_file.library_file_id = resolution.target_library_file_id
            target_issue.status = IssueStatus.OWNED
        else:
            imported_file.status = ImportedFileStatus.CONFIRMED
            imported_file.include_in_import = True
            retry_series_ids.add(int(target_import_series.id))
            target_import_series.status = ImportSeriesStatus.DUPLICATE
            target_import_series.selected_for_import = True
            target_import_series.error_message = None

        affected_series_ids.update(
            {resolution.source_import_series_id, int(target_import_series.id)}
        )
    await session.flush()
    return affected_series_ids, retry_series_ids


async def _prepare_series_for_retry(
    session: AsyncSession,
    job: ImportJob,
    series_ids: set[int],
) -> bool:
    if not series_ids:
        return False
    remaining_conflicts = set(
        await session.scalars(
            select(ImportedFile.import_series_id)
            .where(
                ImportedFile.import_series_id.in_(series_ids),
                ImportedFile.status == ImportedFileStatus.CONFLICT,
            )
            .distinct()
        )
    )
    retry_series_ids = sorted(series_ids - {int(value) for value in remaining_conflicts})
    if not retry_series_ids:
        return False
    await session.execute(
        update(ImportedSeries)
        .where(ImportedSeries.id.in_(retry_series_ids))
        .values(
            status=ImportSeriesStatus.CONFIRMED,
            selected_for_import=True,
            error_message=None,
        )
    )
    job.status = ImportJobStatus.IMPORTING
    job.error_message = None
    return True


async def apply_completed_import_cleanup(
    session: AsyncSession,
    job_id: int,
    action: CompletedImportCleanupAction,
    *,
    actor_id: int,
    preview_token: str,
    actor_username: str | None = None,
    source_ip: str | None = None,
) -> CompletedImportCleanupResult:
    """Apply exactly the previewed cleanup scope without touching source files."""
    job = await _load_completed_job(session, job_id)
    preview_snapshot = _validate_token(
        preview_token,
        job_id=job_id,
        action=action,
        actor_id=actor_id,
    )
    current_snapshot = await _load_snapshot(session, job_id, action)
    if current_snapshot != preview_snapshot:
        raise ValidationError("The cleanup scope changed. Preview the action again.")

    if action is CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS:
        affected_series_ids = await _apply_recommended_conflicts(session, job)
        affected_file_ids: tuple[int, ...] = ()
        requires_import_retry = await _prepare_series_for_retry(session, job, affected_series_ids)
    elif action is CompletedImportCleanupAction.RESOLVE_MIXED_FOLDER_FILES:
        affected_series_ids, retry_series_ids = await _apply_mixed_folder_resolutions(session, job)
        affected_file_ids = ()
        requires_import_retry = await _prepare_series_for_retry(session, job, retry_series_ids)
    else:
        affected_series_ids, affected_file_ids, requires_import_retry = await _apply_file_action(
            session, job, action
        )
        if action is CompletedImportCleanupAction.ALLOW_OVERSIZED_FILES:
            requires_import_retry = await _prepare_series_for_retry(
                session, job, affected_series_ids
            )

    await recompute_file_counters(session, job, series_ids=sorted(affected_series_ids))
    await recompute_series_counters(session, job)
    result = CompletedImportCleanupResult(
        job_id=job.id,
        action=action,
        affected_count=preview_snapshot.affected_count,
        affected_file_count=preview_snapshot.affected_file_count,
        requires_import_retry=requires_import_retry,
        retry_file_ids=(
            affected_file_ids
            if action is CompletedImportCleanupAction.RETRY_SOURCE_INSPECTION
            else ()
        ),
    )
    item_unit = (
        "group" if action is CompletedImportCleanupAction.ACCEPT_RECOMMENDED_CONFLICTS else "file"
    )
    session.add(
        ImportJobLog(
            import_job_id=job.id,
            level="INFO",
            event="import_completed_cleanup_applied",
            message=(
                f"Applied {action.value} to {result.affected_count} "
                f"{item_unit}"
                f"{'s' if result.affected_count != 1 else ''}."
            ),
            data={
                "action": action.value,
                "affected_count": result.affected_count,
                "affected_file_count": result.affected_file_count,
                "requires_import_retry": result.requires_import_retry,
                "source_preserved": True,
            },
        )
    )
    await AuditService.log_event(
        session,
        AuditEventType.IMPORT_RECOVERY_BULK_ACTION,
        source_ip=source_ip,
        user_id=actor_id,
        username=actor_username,
        detail="Completed import recovery action applied.",
        metadata={
            "job_id": job.id,
            "action": action.value,
            "affected_count": result.affected_count,
            "affected_file_count": result.affected_file_count,
            "source_preserved": True,
        },
    )
    await session.flush()
    return result
