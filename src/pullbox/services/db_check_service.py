"""Detection and repair helpers for DB Check preview and cleanup actions."""

from __future__ import annotations

import asyncio
import os
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pullbox.core.comicinfo_reader import read_comicinfo
from pullbox.core.release_parser import normalize_issue_number, parse_release_title
from pullbox.models.issue import Issue
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


_CV_SUFFIX_RE = re.compile(r"\[(?:cv-)?(?P<comicvine_id>\d+)\]$", re.IGNORECASE)
_IGNORED_SCAN_DIR_NAMES = frozenset({".trash", ".covers", "__pycache__"})
_FORMAT_MAP: dict[str, FileFormat] = {
    "cbz": FileFormat.CBZ,
    "cbr": FileFormat.CBR,
    "cb7": FileFormat.CB7,
    "cbt": FileFormat.CBT,
    "pdf": FileFormat.PDF,
    "epub": FileFormat.EPUB,
}


def normalize_library_path(path_value: str | Path | None) -> str | None:
    """Return a normalized absolute path string suitable for comparisons."""
    if path_value is None:
        return None
    return str(Path(path_value).expanduser().resolve(strict=False)).rstrip("/")


def _path_has_prefix(path_value: str | Path | None, prefix_value: str | Path | None) -> bool:
    normalized_path = normalize_library_path(path_value)
    normalized_prefix = normalize_library_path(prefix_value)
    if normalized_path is None or normalized_prefix is None:
        return False
    return normalized_path == normalized_prefix or normalized_path.startswith(
        f"{normalized_prefix}/"
    )


def resolve_enabled_root_for_path(
    path_value: str | Path | None,
    roots: Sequence[LibraryRoot],
) -> LibraryRoot | None:
    """Resolve the best enabled library root for a filesystem path."""
    matches: list[tuple[int, LibraryRoot]] = []
    normalized_path = normalize_library_path(path_value)
    if normalized_path is None:
        return None

    for root in roots:
        normalized_root = normalize_library_path(root.path)
        if normalized_root is None:
            continue
        if _path_has_prefix(normalized_path, normalized_root):
            matches.append((len(normalized_root), root))

    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


async def build_referential_findings(session: AsyncSession) -> list[dict[str, Any]]:
    """Build repairable referential/path consistency findings for DB Check preview."""
    enabled_roots = await _load_enabled_roots(session)
    root_by_id = {root.id: root for root in enabled_roots}
    findings: list[dict[str, Any]] = []
    broken_issue_file_ids: set[int] = set()

    broken_issue_links = await session.execute(
        select(LibraryFile.id, LibraryFile.file_path)
        .outerjoin(Issue, LibraryFile.issue_id == Issue.id)
        .where(LibraryFile.issue_id.is_not(None), Issue.id.is_(None))
    )
    for row_id, row_file_path in broken_issue_links.all():
        broken_issue_file_ids.add(int(row_id))
        findings.append(
            {
                "finding_id": f"referential-library-file-{int(row_id)}",
                "check_type": "referential",
                "record_id": int(row_id),
                "record_type": "library_file",
                "file_path": str(row_file_path or "") or None,
                "description": "Library file references a missing issue record.",
                "suggested_action": "delete",
                "allowed_actions": ["delete", "skip"],
                "context": {},
            }
        )

    series_parent_candidates = await _load_series_parent_candidates(session)
    series_rows = (
        await session.execute(
            select(Series).where((Series.path.is_not(None)) | (Series.library_root_id.is_not(None)))
        )
    ).scalars()
    all_series = list(series_rows.all())

    directory_index: _DirectoryIndex | None = None
    repaired_series_ids: set[int] = set()

    for series in all_series:
        candidate_path = _infer_series_candidate_path(
            series,
            series_parent_candidates.get(series.id),
        )
        if candidate_path is None:
            if directory_index is None:
                directory_index = _DirectoryIndex.build(enabled_roots)
            candidate_path = directory_index.find_candidate(series)

        normalized_candidate = normalize_library_path(candidate_path)
        normalized_stored = normalize_library_path(series.path)
        if normalized_candidate is None or normalized_candidate == normalized_stored:
            continue

        target_root = resolve_enabled_root_for_path(normalized_candidate, enabled_roots)
        findings.append(
            {
                "finding_id": f"referential-series-path-{series.id}",
                "check_type": "referential",
                "record_id": int(series.id),
                "record_type": "series",
                "file_path": normalized_candidate,
                "description": ("Stored series path is out of sync with the folder on disk."),
                "suggested_action": "repair",
                "allowed_actions": ["repair", "skip"],
                "context": {
                    "repair_kind": "series_path",
                    "current_path": series.path,
                    "target_path": normalized_candidate,
                    "target_root_id": target_root.id if target_root is not None else None,
                    "target_root_path": target_root.path if target_root is not None else None,
                },
            }
        )
        repaired_series_ids.add(int(series.id))

    for series in all_series:
        if int(series.id) in repaired_series_ids:
            continue

        expected_root = resolve_enabled_root_for_path(series.path, enabled_roots)
        current_root_missing = (
            series.library_root_id is not None and series.library_root_id not in root_by_id
        )
        if expected_root is None:
            if current_root_missing:
                findings.append(
                    {
                        "finding_id": f"referential-series-root-missing-{series.id}",
                        "check_type": "referential",
                        "record_id": int(series.id),
                        "record_type": "series",
                        "file_path": series.path,
                        "description": (
                            "Series record references a missing library root and cannot be "
                            "repaired automatically from its current path."
                        ),
                        "suggested_action": "skip",
                        "allowed_actions": ["skip"],
                        "context": {},
                    }
                )
            continue

        if series.library_root_id == expected_root.id:
            continue

        findings.append(
            {
                "finding_id": f"referential-series-root-{series.id}",
                "check_type": "referential",
                "record_id": int(series.id),
                "record_type": "series",
                "file_path": series.path,
                "description": "Series library root does not match the path on disk.",
                "suggested_action": "repair",
                "allowed_actions": ["repair", "skip"],
                "context": {
                    "repair_kind": "series_root_id",
                    "target_root_id": expected_root.id,
                    "target_root_path": expected_root.path,
                },
            }
        )

    library_file_rows = (
        await session.execute(
            select(LibraryFile, Issue.series_id)
            .outerjoin(Issue, LibraryFile.issue_id == Issue.id)
            .order_by(LibraryFile.id.asc())
        )
    ).all()
    for library_file, series_id in library_file_rows:
        if int(library_file.id) in broken_issue_file_ids:
            continue
        if series_id is not None and int(series_id) in repaired_series_ids:
            continue

        expected_root = resolve_enabled_root_for_path(library_file.file_path, enabled_roots)
        current_root_missing = library_file.library_root_id not in root_by_id
        if expected_root is None:
            if current_root_missing:
                findings.append(
                    {
                        "finding_id": f"referential-library-file-root-missing-{library_file.id}",
                        "check_type": "referential",
                        "record_id": int(library_file.id),
                        "record_type": "library_file",
                        "file_path": library_file.file_path,
                        "description": (
                            "Library file references a missing library root and cannot be "
                            "repaired automatically from its current path."
                        ),
                        "suggested_action": "skip",
                        "allowed_actions": ["skip"],
                        "context": {},
                    }
                )
            continue

        if library_file.library_root_id == expected_root.id:
            continue

        findings.append(
            {
                "finding_id": f"referential-library-file-root-{library_file.id}",
                "check_type": "referential",
                "record_id": int(library_file.id),
                "record_type": "library_file",
                "file_path": library_file.file_path,
                "description": "Library file library root does not match the file path on disk.",
                "suggested_action": "repair",
                "allowed_actions": ["repair", "skip"],
                "context": {
                    "repair_kind": "library_file_root_id",
                    "target_root_id": expected_root.id,
                    "target_root_path": expected_root.path,
                },
            }
        )

    return findings


async def apply_db_check_repair(
    session: AsyncSession,
    item_data: dict[str, Any],
) -> None:
    """Apply one repair/reindex action selected in DB Check preview."""
    operation = str(item_data.get("operation", "") or "")
    context = _context_dict(item_data)
    repair_kind = str(context.get("repair_kind", "") or "")

    if operation == "repair" and repair_kind == "series_path":
        record_id = _int_or_none(item_data.get("record_id"))
        target_path = normalize_library_path(context.get("target_path"))
        if record_id is not None and target_path:
            await repair_series_path(session, series_id=record_id, target_path=target_path)
        return

    if operation == "repair" and repair_kind == "series_root_id":
        record_id = _int_or_none(item_data.get("record_id"))
        target_root_id = _int_or_none(context.get("target_root_id"))
        if record_id is not None and target_root_id is not None:
            await repair_series_root_id(session, series_id=record_id, target_root_id=target_root_id)
        return

    if operation == "repair" and repair_kind == "library_file_root_id":
        record_id = _int_or_none(item_data.get("record_id"))
        target_root_id = _int_or_none(context.get("target_root_id"))
        if record_id is not None and target_root_id is not None:
            await repair_library_file_root_id(
                session,
                library_file_id=record_id,
                target_root_id=target_root_id,
            )
        return

    if operation == "reindex":
        target_root_path = normalize_library_path(
            context.get("target_root_path") or item_data.get("file_path")
        )
        if target_root_path:
            await reindex_library_root(session, target_root_path=target_root_path)


async def register_stale_library_file(
    session: AsyncSession,
    *,
    file_path_str: str,
) -> dict[str, Any] | None:
    """Register a stale library file or describe why it could not be resolved."""
    file_path = Path(file_path_str)
    if not file_path.exists():
        return {
            "file_path": file_path_str,
            "folder": str(file_path.parent),
            "reason": "File no longer exists on disk.",
        }

    existing = await session.execute(
        select(LibraryFile).where(LibraryFile.file_path == file_path_str)
    )
    if existing.scalar_one_or_none() is not None:
        return None

    ext = file_path.suffix.lower().lstrip(".")
    file_format = _FORMAT_MAP.get(ext)
    if file_format is None:
        return {
            "file_path": file_path_str,
            "folder": str(file_path.parent),
            "reason": f"Unsupported file format: .{ext}",
        }

    enabled_roots = await _load_enabled_roots(session)
    library_root = resolve_enabled_root_for_path(file_path_str, enabled_roots)
    if library_root is None:
        return {
            "file_path": file_path_str,
            "folder": str(file_path.parent),
            "reason": "File is not inside any configured library root.",
        }

    parent_folder = normalize_library_path(file_path.parent)
    if parent_folder is None:
        return {
            "file_path": file_path_str,
            "folder": str(file_path.parent),
            "reason": "File path could not be normalized.",
        }

    series_result = await session.execute(select(Series).where(Series.path == parent_folder))
    series = series_result.scalar_one_or_none()
    if series is None:
        all_series = await session.execute(
            select(Series).where(Series.library_root_id == library_root.id)
        )
        folder_name = file_path.parent.name
        for candidate in all_series.scalars().all():
            if candidate.path and Path(candidate.path).name == folder_name:
                series = candidate
                break

    parsed = parse_release_title(file_path.stem)
    issue_id: int | None = None
    confidence = MatchConfidence.UNMATCHED

    if series is not None and parsed and parsed.issue_number is not None:
        issue_filters = [Issue.series_id == series.id]
        if parsed.issue_number_text is not None:
            issue_filters.append(Issue.issue_number_text == parsed.issue_number_text)
        else:
            issue_filters.append(Issue.issue_number == parsed.issue_number)
        issue_result = await session.execute(select(Issue).where(*issue_filters).limit(2))
        issue_candidates = list(issue_result.scalars().all())
        issue = issue_candidates[0] if len(issue_candidates) == 1 else None
        if issue is not None:
            issue_id = issue.id
            confidence = MatchConfidence.HIGH
        else:
            confidence = MatchConfidence.LOW
    elif series is not None:
        confidence = MatchConfidence.LOW

    if series is None:
        return {
            "file_path": file_path_str,
            "folder": parent_folder,
            "reason": f"No matching series found for folder: {file_path.parent.name}",
        }

    stat = file_path.stat()
    library_file = LibraryFile(
        file_path=file_path_str,
        file_name=file_path.name,
        file_size=stat.st_size,
        file_format=file_format,
        file_modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        match_confidence=confidence,
        parsed_series=parsed.series_name if parsed else None,
        parsed_issue_number=parsed.issue_number if parsed else None,
        parsed_year=parsed.year if parsed else None,
        issue_id=issue_id,
        library_root_id=library_root.id,
    )
    session.add(library_file)
    return None


async def repair_series_path(
    session: AsyncSession,
    *,
    series_id: int,
    target_path: str,
) -> None:
    """Repair a stale Series.path and cascade descendant LibraryFile paths."""
    series = await session.get(Series, series_id)
    if series is None:
        return

    enabled_roots = await _load_enabled_roots(session)
    target_root = resolve_enabled_root_for_path(target_path, enabled_roots)
    old_path = series.path
    series.path = target_path
    if target_root is not None:
        series.library_root_id = target_root.id

    library_files = list(
        (
            await session.execute(
                select(LibraryFile)
                .join(Issue, LibraryFile.issue_id == Issue.id)
                .where(Issue.series_id == series_id)
            )
        )
        .scalars()
        .all()
    )
    for library_file in library_files:
        next_path = _resolve_repaired_library_file_path(
            current_path=library_file.file_path,
            old_series_path=old_path,
            target_series_path=target_path,
        )
        if next_path is not None:
            library_file.file_path = next_path
            library_file.file_name = Path(next_path).name
        if target_root is not None:
            library_file.library_root_id = target_root.id
        await refresh_library_file_filesystem_fields(library_file)


async def repair_series_root_id(
    session: AsyncSession,
    *,
    series_id: int,
    target_root_id: int,
) -> None:
    """Repair Series.library_root_id when the path maps to a different root."""
    series = await session.get(Series, series_id)
    if series is not None:
        series.library_root_id = target_root_id


async def repair_library_file_root_id(
    session: AsyncSession,
    *,
    library_file_id: int,
    target_root_id: int,
) -> None:
    """Repair LibraryFile.library_root_id when the path maps to a different root."""
    library_file = await session.get(LibraryFile, library_file_id)
    if library_file is None:
        return
    library_file.library_root_id = target_root_id
    await refresh_library_file_filesystem_fields(library_file)


async def reindex_library_root(
    session: AsyncSession,
    *,
    target_root_path: str,
) -> int:
    """Refresh tracked file metadata and parsed fields for one library root."""
    enabled_roots = await _load_enabled_roots(session)
    target_root = resolve_enabled_root_for_path(target_root_path, enabled_roots)
    root_prefix = normalize_library_path(target_root_path)
    if root_prefix is None:
        return 0

    tracked_files = list(
        (
            await session.execute(
                select(LibraryFile).where(
                    (LibraryFile.file_path == root_prefix)
                    | (LibraryFile.file_path.like(f"{root_prefix}/%"))
                )
            )
        )
        .scalars()
        .all()
    )

    refreshed = 0
    for library_file in tracked_files:
        changed = await refresh_library_file_metadata(
            library_file,
            enabled_roots=enabled_roots,
            fallback_root=target_root,
        )
        if changed:
            refreshed += 1
    return refreshed


async def refresh_library_file_metadata(
    library_file: LibraryFile,
    *,
    enabled_roots: Sequence[LibraryRoot],
    fallback_root: LibraryRoot | None = None,
) -> bool:
    """Refresh tracked metadata for one LibraryFile from the filesystem."""
    file_path = Path(library_file.file_path)
    if not file_path.exists() or not file_path.is_file():
        return False

    stat = await asyncio.to_thread(file_path.stat)
    library_file.file_path = str(file_path.resolve(strict=False))
    library_file.file_name = file_path.name
    library_file.file_size = stat.st_size
    library_file.file_modified_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    extension = file_path.suffix.lower().lstrip(".")
    if extension in _FORMAT_MAP:
        library_file.file_format = _FORMAT_MAP[extension]

    parsed = parse_release_title(file_path.name)
    library_file.parsed_series = parsed.series_name if parsed is not None else None
    library_file.parsed_issue_number = parsed.issue_number if parsed is not None else None
    library_file.parsed_year = parsed.year if parsed is not None else None
    library_file.parsed_publisher = None
    library_file.has_comicinfo = False

    comicinfo = await asyncio.to_thread(read_comicinfo, file_path)
    if comicinfo is not None:
        library_file.has_comicinfo = True
        if comicinfo.series_name:
            library_file.parsed_series = comicinfo.series_name
        issue_number = normalize_issue_number(comicinfo.issue_number)
        if issue_number is not None:
            library_file.parsed_issue_number = issue_number
        if comicinfo.volume_year is not None:
            library_file.parsed_year = comicinfo.volume_year
        if comicinfo.publisher:
            library_file.parsed_publisher = comicinfo.publisher

    resolved_root = resolve_enabled_root_for_path(library_file.file_path, enabled_roots)
    if resolved_root is None:
        resolved_root = fallback_root
    if resolved_root is not None:
        library_file.library_root_id = resolved_root.id
    return True


async def refresh_library_file_filesystem_fields(library_file: LibraryFile) -> None:
    """Refresh lightweight filesystem-backed fields after a repair."""
    file_path = Path(library_file.file_path)
    if not file_path.exists() or not file_path.is_file():
        return

    stat = await asyncio.to_thread(file_path.stat)
    library_file.file_name = file_path.name
    library_file.file_size = stat.st_size
    library_file.file_modified_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    extension = file_path.suffix.lower().lstrip(".")
    if extension in _FORMAT_MAP:
        library_file.file_format = _FORMAT_MAP[extension]


async def _load_enabled_roots(session: AsyncSession) -> list[LibraryRoot]:
    result = await session.execute(
        select(LibraryRoot).where(LibraryRoot.enabled.is_(True)).order_by(LibraryRoot.id.asc())
    )
    return list(result.scalars().all())


async def _load_series_parent_candidates(session: AsyncSession) -> dict[int, Counter[str]]:
    rows = await session.execute(
        select(Issue.series_id, LibraryFile.file_path)
        .join(LibraryFile, LibraryFile.issue_id == Issue.id)
        .where(Issue.series_id.is_not(None))
    )
    parents: dict[int, Counter[str]] = defaultdict(Counter)
    for series_id, file_path in rows.all():
        if series_id is None or not file_path:
            continue
        path = Path(file_path)
        if not path.exists():
            continue
        normalized_parent = normalize_library_path(path.parent)
        if normalized_parent is None:
            continue
        parents[int(series_id)][normalized_parent] += 1
    return parents


def _infer_series_candidate_path(
    series: Series,
    parent_counts: Counter[str] | None,
) -> str | None:
    if not parent_counts:
        return None

    if len(parent_counts) == 1:
        return next(iter(parent_counts))

    basename = Path(series.path).name if series.path else None
    if basename:
        matching_parents = [
            parent_path for parent_path in parent_counts if Path(parent_path).name == basename
        ]
        if len(matching_parents) == 1:
            return matching_parents[0]

    ordered = parent_counts.most_common(2)
    if len(ordered) == 1:
        return ordered[0][0]
    if ordered[0][1] > ordered[1][1]:
        return ordered[0][0]
    return None


def _context_dict(item_data: dict[str, Any]) -> dict[str, Any]:
    raw_context = item_data.get("context")
    if isinstance(raw_context, dict):
        return raw_context
    return {}


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _resolve_repaired_library_file_path(
    *,
    current_path: str,
    old_series_path: str | None,
    target_series_path: str,
) -> str | None:
    current = Path(current_path)
    normalized_current = normalize_library_path(current)
    normalized_old = normalize_library_path(old_series_path)
    normalized_target = normalize_library_path(target_series_path)
    if normalized_current is None or normalized_target is None:
        return None

    if current.exists() and _path_has_prefix(normalized_current, normalized_target):
        return normalized_current

    if normalized_old is not None and _path_has_prefix(normalized_current, normalized_old):
        suffix = normalized_current[len(normalized_old) :]
        candidate = f"{normalized_target}{suffix}"
        if Path(candidate).exists() or not current.exists():
            return candidate

    candidate_by_name = str(Path(target_series_path) / current.name)
    if Path(candidate_by_name).exists():
        return normalize_library_path(candidate_by_name)

    if current.exists():
        return normalized_current
    return None


class _DirectoryIndex:
    """Filesystem directory index used to resolve stale series folders."""

    def __init__(
        self,
        *,
        by_name: dict[str, list[str]],
        by_comicvine_id: dict[int, list[str]],
    ) -> None:
        self._by_name = by_name
        self._by_comicvine_id = by_comicvine_id

    @classmethod
    def build(cls, roots: Sequence[LibraryRoot]) -> _DirectoryIndex:
        by_name: dict[str, list[str]] = defaultdict(list)
        by_comicvine_id: dict[int, list[str]] = defaultdict(list)

        for root in roots:
            root_path = Path(root.path)
            if not root_path.exists() or not root_path.is_dir():
                continue

            for current_dir, dir_names, _file_names in os.walk(root_path):
                dir_names[:] = [
                    name
                    for name in dir_names
                    if not name.startswith(".") and name.lower() not in _IGNORED_SCAN_DIR_NAMES
                ]
                current_path = Path(current_dir)
                if current_path == root_path:
                    continue

                normalized_current = normalize_library_path(current_path)
                if normalized_current is None:
                    continue
                by_name[current_path.name].append(normalized_current)

                match = _CV_SUFFIX_RE.search(current_path.name)
                if match is not None:
                    by_comicvine_id[int(match.group("comicvine_id"))].append(normalized_current)

        return cls(by_name=dict(by_name), by_comicvine_id=dict(by_comicvine_id))

    def find_candidate(self, series: Series) -> str | None:
        if series.comicvine_id is not None:
            comicvine_matches = self._by_comicvine_id.get(int(series.comicvine_id), [])
            if len(comicvine_matches) == 1:
                return comicvine_matches[0]

        if series.path:
            basename_matches = self._by_name.get(Path(series.path).name, [])
            if len(basename_matches) == 1:
                return basename_matches[0]

        return None
