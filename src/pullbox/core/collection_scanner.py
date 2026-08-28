"""File system collection scanner for comic import.

Walks a directory tree and extracts series candidates from folder names
and filenames. Yields DiscoveredSeries objects progressively as an async
generator, enabling SSE streaming during long-running scans.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

import structlog

from pullbox.core.collection_scan_grouping import (
    _has_strong_file_identity,
    _is_low_signal_file_series_name,
    _is_scan_candidate_file,
    _non_standard_group_discriminator,
    _normalize_series_identity,
    _should_collapse_to_folder_identity,
    _source_issue_type_for_group,
    _strip_year_volume_tokens,
    _type_qualified_issue_like_label,
)
from pullbox.core.source_metadata import MetadataSignal, SourceMetadata, SourceMetadataExtractor
from pullbox.models.issue import IssueType

logger = structlog.get_logger(__name__)

SERIES_SCAN_WORKERS = 4
ARCHIVE_READ_CONCURRENCY = 8
SCAN_PROGRESS_EMIT_INTERVAL_SECONDS = 0.25

# Regex: folder name with a year token, optionally followed by release tags
_FOLDER_YEAR_RE = re.compile(r"^(.+?)\s*[\[(](\d{4})[\])](?:\s*(?:\([^)]*\)|\[[^\]]*\]))*\s*$")
_PULLBOX_FOLDER_CV_ID_RE = re.compile(
    r"^(?P<name>.+?)\s+\((?P<year>(?:19|20)\d{2})\)\s+(?P<cv_id>\d{4,})\s*$"
)

# Comic file extensions (lowercase, with dot)
COMIC_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".cbz",
        ".cbr",
        ".cb7",
        ".cbt",
        ".pdf",
        ".epub",
    }
)

# Directories to skip during scan
IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "__MACOSX",
        ".Thumbs",
        "@eaDir",
        "#recycle",
    }
)


@dataclass
class DiscoveredFile:
    """A single comic file discovered during a scan.

    Pure data — no database interaction.
    """

    file_path: str
    file_name: str
    file_size: int
    file_format: str  # extension without dot (cbz, cbr, etc.)
    parsed_series: str | None
    parsed_issue_number: float | None
    parsed_year: int | None
    parsed_publisher: str | None
    has_comicinfo: bool
    comicvine_issue_id: int | None
    issue_number_raw: str | None  # raw string before float conversion
    issue_type: IssueType = IssueType.ISSUE
    comicvine_series_id: int | None = None
    series_status: str | None = None
    issue_count_hint: int | None = None
    metadata_signals: dict[str, str] = field(default_factory=dict)
    metadata_diagnostics: dict[str, object] = field(default_factory=dict)


@dataclass
class DiscoveredSeries:
    """A series candidate extracted from a file-system scan.

    This is a pure data object — no database interaction.
    All paths are absolute strings.
    """

    raw_series_name: str
    raw_year: int | None
    raw_publisher: str | None
    file_count: int
    sample_paths: list[str]
    source_folder: str
    source_folder_relative: str
    files: list[DiscoveredFile] = field(default_factory=list)
    has_files: bool = True
    mylar3_cv_id: int | None = None
    folder_cv_id: int | None = None
    comicinfo_cv_id: int | None = None
    comicinfo_source: str | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ScanInventory:
    """Lightweight filesystem totals gathered before materialization."""

    directory_count: int
    file_count: int


class CollectionScanner:
    """Walks a directory tree and extracts series candidates.

    Supports any folder depth and naming convention. Uses folder name
    parsing as the primary extraction strategy.

    Args:
        min_file_count: Minimum comic files to consider a folder a series.
        max_sample_paths: Max file paths to store per series.
        progress_callback: Optional async callable(files_scanned, dirs_scanned).
    """

    def __init__(
        self,
        min_file_count: int = 1,
        max_sample_paths: int = 5,
        progress_callback: Callable[[int, int], Awaitable[None]] | None = None,
        file_progress_callback: Callable[[int], Awaitable[None]] | None = None,
        inventory_progress_callback: Callable[[int, int], Awaitable[None]] | None = None,
        extensions: frozenset[str] | None = None,
    ) -> None:
        self._min_file_count = min_file_count
        self._max_sample_paths = max_sample_paths
        self._progress_callback = progress_callback
        self._file_progress_callback = file_progress_callback
        self._inventory_progress_callback = inventory_progress_callback
        self._extensions = extensions or COMIC_EXTENSIONS

    async def inventory(self, root_path: str | Path) -> ScanInventory:
        """Count candidate directories and comic files before the main scan."""
        root = Path(root_path).resolve()
        if not root.is_dir():
            msg = f"Scan root is not a directory: {root}"
            raise ValueError(msg)

        if self._inventory_progress_callback is None:
            directory_count, file_count = await asyncio.to_thread(self._inventory_tree, root)
            return ScanInventory(directory_count=directory_count, file_count=file_count)

        loop = asyncio.get_running_loop()
        progress_queue: asyncio.Queue[tuple[int, int] | None] = asyncio.Queue()

        async def drain_inventory_progress() -> None:
            last_emitted: tuple[int, int] | None = None
            while True:
                counts = await progress_queue.get()
                if counts is None:
                    break
                if counts is not None and counts != last_emitted:
                    last_emitted = counts
                    progress_callback = self._inventory_progress_callback
                    if progress_callback is not None:
                        await progress_callback(*counts)

        def report_inventory_progress(directory_count: int, file_count: int) -> None:
            loop.call_soon_threadsafe(
                progress_queue.put_nowait,
                (directory_count, file_count),
            )

        drain_task = asyncio.create_task(drain_inventory_progress())
        try:
            directory_count, file_count = await asyncio.to_thread(
                self._inventory_tree,
                root,
                report_inventory_progress,
            )
        finally:
            loop.call_soon_threadsafe(progress_queue.put_nowait, None)
            await drain_task

        return ScanInventory(directory_count=directory_count, file_count=file_count)

    async def scan(self, root_path: str | Path) -> AsyncGenerator[DiscoveredSeries, None]:
        """Async generator yielding DiscoveredSeries as they are found.

        Strategy:
        1. Walk the tree collecting comic files per directory.
        2. Identify series directories — leaf dirs containing comic files.
        3. Extract raw_series_name, raw_year, raw_publisher from folder names.
        4. Build DiscoveredFile objects for each comic file (with filename
           parsing and ComicInfo extraction).
        5. Yield a DiscoveredSeries for each identified series directory.
        """
        root = Path(root_path).resolve()
        if not root.is_dir():
            msg = f"Scan root is not a directory: {root}"
            raise ValueError(msg)

        log = logger.bind(scan_root=str(root))
        log.info("import_scan_started")

        walk_progress_queue: asyncio.Queue[tuple[int, int] | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        async def drain_walk_progress() -> None:
            last_emitted: tuple[int, int] | None = None
            while True:
                counts = await walk_progress_queue.get()
                if counts is None:
                    break
                if counts is not None and counts != last_emitted:
                    last_emitted = counts
                    file_count, dir_count = counts[1], counts[0]
                    if self._progress_callback:
                        await self._progress_callback(file_count, dir_count)
                    if self._inventory_progress_callback:
                        await self._inventory_progress_callback(dir_count, file_count)

        def report_walk_progress(directory_count: int, file_count: int) -> None:
            loop.call_soon_threadsafe(
                walk_progress_queue.put_nowait,
                (directory_count, file_count),
            )

        walk_progress_task = asyncio.create_task(drain_walk_progress())
        try:
            # Collect all directories and their comic files in one filesystem pass.
            dir_files, dirs_scanned, files_scanned = await asyncio.to_thread(
                self._walk_tree_with_counts,
                root,
                report_walk_progress,
            )
        finally:
            loop.call_soon_threadsafe(walk_progress_queue.put_nowait, None)
            await walk_progress_task

        # Identify series directories (leaf dirs with comics, not parents of other series dirs).
        series_dirs = self._identify_series_dirs(dir_files)

        # Limit concurrent archive reads and per-series materialization work.
        comicinfo_sem = asyncio.Semaphore(ARCHIVE_READ_CONCURRENCY)
        series_worker_sem = asyncio.Semaphore(SERIES_SCAN_WORKERS)

        async def process_bucket(
            series_dir: Path,
            comic_files: list[Path],
            *,
            folder_name: str,
            folder_year: int | None,
            folder_publisher: str | None,
            folder_cv_id: int | None,
            allow_weak_file_identity: bool,
        ) -> list[DiscoveredSeries]:
            async with series_worker_sem:
                discovered_files = await self._build_discovered_files(
                    comic_files,
                    comicinfo_sem,
                    folder_publisher=folder_publisher,
                    allow_weak_file_identity=allow_weak_file_identity,
                )
                return self._build_series_candidates(
                    source_dir=series_dir,
                    root=root,
                    folder_name=folder_name,
                    folder_year=folder_year,
                    folder_publisher=folder_publisher,
                    folder_cv_id=folder_cv_id,
                    discovered_files=discovered_files,
                    allow_weak_file_identity=allow_weak_file_identity,
                )

        tasks: list[asyncio.Task[list[DiscoveredSeries]]] = []
        for series_dir in sorted(series_dirs):
            comic_files = dir_files[series_dir]
            if len(comic_files) < self._min_file_count:
                continue

            name, year, folder_cv_id = self._extract_folder_identity(series_dir.name)
            publisher = self._infer_publisher_from_hierarchy(series_dir, root)
            tasks.append(
                asyncio.create_task(
                    process_bucket(
                        series_dir,
                        comic_files,
                        folder_name=name,
                        folder_year=year,
                        folder_publisher=publisher,
                        folder_cv_id=folder_cv_id,
                        allow_weak_file_identity=series_dir == root,
                    )
                )
            )

        # Handle loose files in non-leaf directories (e.g. root folder).
        loose_dirs = set(dir_files.keys()) - set(series_dirs)
        loose_series_count = 0
        for loose_dir in sorted(loose_dirs):
            loose_files = dir_files[loose_dir]
            tasks.append(
                asyncio.create_task(
                    process_bucket(
                        loose_dir,
                        loose_files,
                        folder_name=loose_dir.name if loose_dir.name else "(root)",
                        folder_year=None,
                        folder_publisher=None,
                        folder_cv_id=None,
                        allow_weak_file_identity=loose_dir == root,
                    )
                )
            )

        for task in asyncio.as_completed(tasks):
            candidates = await task
            for candidate in candidates:
                if len(candidate.files) < self._min_file_count:
                    continue
                if candidate.source_folder_relative == "(root)" or candidate.source_folder == str(
                    root
                ):
                    loose_series_count += 1
                yield candidate

        total_series = len(series_dirs) + loose_series_count
        log.info(
            "import_scan_completed",
            series_count=total_series,
            files_scanned=files_scanned,
            dirs_scanned=dirs_scanned,
            loose_series=loose_series_count,
        )

    async def scan_files(self, file_paths: list[str]) -> list[DiscoveredSeries]:
        """Build DiscoveredSeries from explicit file paths (no directory walk).

        Groups files by parent directory, treating each directory as a
        series candidate. Loose files (in different directories) each
        get their own group. Used by the DB check "Import Unresolvable"
        flow to import specific files without scanning entire trees.
        """
        log = logger.bind(file_count=len(file_paths))
        log.info("import_scan_files_started")

        # Group files by parent directory
        dir_files: dict[Path, list[Path]] = {}
        for fp_str in file_paths:
            fp = Path(fp_str)
            if fp.exists() and fp.is_file() and _is_scan_candidate_file(fp, self._extensions):
                dir_files.setdefault(fp.parent, []).append(fp)

        comicinfo_sem = asyncio.Semaphore(ARCHIVE_READ_CONCURRENCY)
        discovered_list: list[DiscoveredSeries] = []

        for series_dir, comic_files in sorted(dir_files.items()):
            name, year, folder_cv_id = self._extract_folder_identity(series_dir.name)
            discovered_files = await self._build_discovered_files(
                comic_files,
                comicinfo_sem,
                allow_weak_file_identity=True,
            )
            if not discovered_files:
                continue

            publisher: str | None = None
            discovered_list.extend(
                self._build_series_candidates(
                    source_dir=series_dir,
                    root=None,
                    folder_name=name,
                    folder_year=year,
                    folder_publisher=publisher,
                    folder_cv_id=folder_cv_id,
                    discovered_files=discovered_files,
                    allow_weak_file_identity=True,
                )
            )

        log.info(
            "import_scan_files_completed",
            series_count=len(discovered_list),
            files_found=sum(s.file_count for s in discovered_list),
        )
        return discovered_list

    async def _build_discovered_files(
        self,
        comic_files: list[Path],
        sem: asyncio.Semaphore,
        *,
        folder_publisher: str | None = None,
        allow_weak_file_identity: bool = False,
    ) -> list[DiscoveredFile]:
        """Build DiscoveredFile objects for a list of comic file paths.

        Parses filenames synchronously and extracts ComicInfo in parallel
        with a concurrency limit via the provided semaphore.
        """
        extractor = SourceMetadataExtractor()
        sidecar_data = extractor.read_sidecars(comic_files[0].parent) if comic_files else None

        async def _process_one(fpath: Path, *, is_series_sample: bool) -> DiscoveredFile:
            file_name = fpath.name
            file_format = fpath.suffix.lstrip(".").lower()

            # Get file size (sync but fast — just a stat call)
            try:
                file_size = fpath.stat().st_size
            except OSError:
                file_size = 0

            initial_metadata = extractor.from_path(
                fpath,
                include_archive_comicinfo=False,
                sidecar_data=sidecar_data,
            )
            should_load_archive_metadata = self._should_load_archive_metadata(
                initial_metadata,
                allow_weak_file_identity=allow_weak_file_identity,
            )
            # Preserve series-level ComicInfo overrides when the folder itself is
            # the only candidate source for publisher/title and there is no
            # stronger sidecar or embedded CV identity yet. We only do this for
            # one representative file per series bucket.
            if (
                not should_load_archive_metadata
                and is_series_sample
                and initial_metadata.comicvine_series_id is None
                and initial_metadata.comicvine_issue_id is None
                and (
                    folder_publisher is not None
                    or (initial_metadata.publisher is None and len(comic_files) == 1)
                )
            ):
                should_load_archive_metadata = True
            if should_load_archive_metadata:
                async with sem:
                    metadata = await asyncio.to_thread(
                        extractor.from_path,
                        fpath,
                        sidecar_data=sidecar_data,
                    )
            else:
                metadata = initial_metadata

            parsed = metadata.parsed_release
            issue_number_raw: str | None = None
            if parsed is not None and parsed.issue_number is not None:
                issue_number_raw = (
                    str(int(parsed.issue_number))
                    if parsed.issue_number == int(parsed.issue_number)
                    else str(parsed.issue_number)
                )
            elif metadata.issue_number is not None:
                issue_number_raw = (
                    str(int(metadata.issue_number))
                    if metadata.issue_number == int(metadata.issue_number)
                    else str(metadata.issue_number)
                )

            return DiscoveredFile(
                file_path=str(fpath),
                file_name=file_name,
                file_size=file_size,
                file_format=file_format,
                parsed_series=metadata.series_name,
                parsed_issue_number=metadata.issue_number,
                parsed_year=metadata.year,
                parsed_publisher=metadata.publisher,
                has_comicinfo=bool(metadata.diagnostics.get("has_comicinfo")),
                comicvine_issue_id=metadata.comicvine_issue_id,
                issue_number_raw=issue_number_raw,
                issue_type=metadata.issue_type,
                comicvine_series_id=metadata.comicvine_series_id,
                series_status=metadata.series_status,
                issue_count_hint=metadata.issue_count_hint,
                metadata_signals={
                    key: value.value if isinstance(value, MetadataSignal) else str(value)
                    for key, value in metadata.signals.items()
                },
                metadata_diagnostics=dict(metadata.diagnostics),
            )

        sorted_files = sorted(comic_files)
        tasks = [
            _process_one(f, is_series_sample=index == 0) for index, f in enumerate(sorted_files)
        ]
        results: list[DiscoveredFile] = []
        for task in asyncio.as_completed(tasks):
            results.append(await task)
            if self._file_progress_callback is not None:
                await self._file_progress_callback(1)
        return sorted(results, key=lambda df: df.file_name)

    def _should_load_archive_metadata(
        self,
        metadata: SourceMetadata,
        *,
        allow_weak_file_identity: bool,
    ) -> bool:
        """Return True when scan should pay the archive-read cost up front."""
        if allow_weak_file_identity:
            return True
        if not metadata.series_name or _is_low_signal_file_series_name(metadata.series_name):
            return True
        if metadata.issue_number is not None:
            return False
        return metadata.comicvine_issue_id is None and metadata.comicvine_series_id is None

    def _identity_for_file(
        self,
        discovered_file: DiscoveredFile,
        *,
        allow_weak_file_identity: bool = False,
    ) -> tuple[str, int | None] | None:
        """Return the strongest available grouping identity for a discovered file."""
        if discovered_file.parsed_series:
            if _is_low_signal_file_series_name(discovered_file.parsed_series):
                return None
            if not allow_weak_file_identity and not _has_strong_file_identity(discovered_file):
                return None
            return (
                _normalize_series_identity(discovered_file.parsed_series),
                discovered_file.parsed_year,
            )
        return None

    def _build_series_candidates(
        self,
        *,
        source_dir: Path,
        root: Path | None,
        folder_name: str,
        folder_year: int | None,
        folder_publisher: str | None,
        folder_cv_id: int | None,
        discovered_files: list[DiscoveredFile],
        allow_weak_file_identity: bool = False,
    ) -> list[DiscoveredSeries]:
        """Split a folder or explicit file group into one or more series candidates."""
        classified: dict[tuple[str, int | None, str | None], list[DiscoveredFile]] = {}
        unclassified: list[DiscoveredFile] = []
        labels: dict[
            tuple[str, int | None, str | None],
            tuple[str, int | None, str | None],
        ] = {}
        folder_identity = _normalize_series_identity(folder_name)

        for discovered_file in discovered_files:
            identity = self._identity_for_file(
                discovered_file,
                allow_weak_file_identity=allow_weak_file_identity,
            )
            if identity is None:
                unclassified.append(discovered_file)
                continue

            collapse_to_folder = bool(
                discovered_file.parsed_series
                and folder_identity
                and _should_collapse_to_folder_identity(
                    discovered_file.parsed_series,
                    folder_name,
                )
            )
            type_discriminator = _non_standard_group_discriminator(discovered_file.issue_type)
            if collapse_to_folder:
                # Once we decide a filename only differs from the folder by
                # low-signal release noise, keep the file-level parsed series in
                # sync with the folder identity. Downstream semantic matching
                # uses ``parsed_series`` again when resolving file-to-issue
                # matches, and leaving a value like "Saga dup" in place can
                # incorrectly turn an otherwise valid conflict candidate into a
                # file-level no-match.
                discovered_file.parsed_series = folder_name
                if discovered_file.parsed_year is None:
                    discovered_file.parsed_year = folder_year
                identity = (folder_identity, discovered_file.parsed_year or folder_year)

            identity_key = (identity[0], identity[1], type_discriminator)

            classified.setdefault(identity_key, []).append(discovered_file)
            label_name = discovered_file.parsed_series or folder_name
            if collapse_to_folder:
                label_name = folder_name
            elif discovered_file.parsed_series:
                parsed_label = _normalize_series_identity(
                    _strip_year_volume_tokens(discovered_file.parsed_series)
                )
                folder_label = _normalize_series_identity(folder_name)
                if folder_label and parsed_label == folder_label:
                    label_name = folder_name
            if type_discriminator is not None:
                label_name = _type_qualified_issue_like_label(
                    label_name,
                    discovered_file.issue_type,
                )
            labels.setdefault(
                identity_key,
                (
                    label_name,
                    discovered_file.parsed_year or folder_year,
                    discovered_file.parsed_publisher or folder_publisher,
                ),
            )

        grouped_candidates: list[tuple[tuple[str, int | None, str | None], list[DiscoveredFile]]]
        if not classified:
            grouped_candidates = [
                ((folder_name, folder_year, folder_publisher), list(discovered_files))
            ]
            diagnostics = {
                "bucket_kind": "folder_fallback",
                "mixed_bucket": False,
            }
        else:
            grouped_candidates = []
            for classified_identity, files in classified.items():
                grouped_candidates.append((labels[classified_identity], list(files)))

            diagnostics = {
                "bucket_kind": "metadata_grouped",
                "mixed_bucket": len(grouped_candidates) > 1,
                "parsed_series_names": sorted(
                    {label[0] for label, _files in grouped_candidates if label[0]}
                ),
            }

            if unclassified:
                if len(grouped_candidates) == 1:
                    grouped_candidates[0][1].extend(unclassified)
                else:
                    grouped_candidates.append(
                        ((folder_name, folder_year, folder_publisher), list(unclassified))
                    )
                    diagnostics["bucket_kind"] = "mixed_fallback"
                    diagnostics["unclassified_file_count"] = len(unclassified)

        series_candidates: list[DiscoveredSeries] = []
        for (raw_series_name, raw_year, raw_publisher), files in sorted(
            grouped_candidates,
            key=lambda item: (item[0][0].lower(), item[0][1] or 0),
        ):
            sample = sorted(df.file_path for df in files[: self._max_sample_paths])
            try:
                rel = str(source_dir.relative_to(root)) if root is not None else source_dir.name
            except ValueError:
                rel = source_dir.name
            if rel == ".":
                rel = "(root)"

            comicinfo_cv_id: int | None = None
            comicinfo_source: str | None = None
            series_status: str | None = None
            issue_count_hint: int | None = None
            for discovered_file in files:
                if discovered_file.comicvine_series_id is not None:
                    comicinfo_cv_id = discovered_file.comicvine_series_id
                    comicinfo_source = discovered_file.file_path
                    break
            source_issue_type = _source_issue_type_for_group(files)
            for discovered_file in files:
                if series_status is None and discovered_file.series_status:
                    series_status = discovered_file.series_status
                if issue_count_hint is None and discovered_file.issue_count_hint is not None:
                    issue_count_hint = discovered_file.issue_count_hint

            series_candidates.append(
                DiscoveredSeries(
                    raw_series_name=raw_series_name,
                    raw_year=raw_year,
                    raw_publisher=raw_publisher,
                    file_count=len(files),
                    sample_paths=sample,
                    source_folder=str(source_dir),
                    source_folder_relative=rel,
                    files=sorted(files, key=lambda df: df.file_name),
                    folder_cv_id=folder_cv_id,
                    comicinfo_cv_id=comicinfo_cv_id,
                    comicinfo_source=comicinfo_source,
                    diagnostics={
                        **dict(diagnostics),
                        "source_issue_type": (
                            source_issue_type.value if source_issue_type is not None else None
                        ),
                        "series_status": series_status,
                        "issue_count_hint": issue_count_hint,
                        **(
                            {
                                "folder_identity": {
                                    "kind": "pullbox_series_folder",
                                    "comicvine_series_id": folder_cv_id,
                                }
                            }
                            if folder_cv_id is not None
                            else {}
                        ),
                    },
                )
            )

        return series_candidates

    def _inventory_tree(
        self,
        root: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> tuple[int, int]:
        """Walk the tree and count visited directories plus supported comic files."""
        directory_count = 0
        file_count = 0
        last_emit_at = time.monotonic()

        def _on_error(exc: OSError) -> None:
            logger.warning("import_scan_walk_error", path=str(root), error=str(exc))

        for dirpath, dirnames, filenames in root.walk(on_error=_on_error):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
            dirnames.sort()
            filenames.sort()
            directory_count += 1
            file_count += sum(
                1
                for fname in filenames
                if _is_scan_candidate_file(dirpath / fname, self._extensions)
            )
            if progress_callback is not None:
                now = time.monotonic()
                if (
                    directory_count == 1
                    or directory_count % 100 == 0
                    or (now - last_emit_at) >= 0.2
                ):
                    progress_callback(directory_count, file_count)
                    last_emit_at = now

        if progress_callback is not None:
            progress_callback(directory_count, file_count)

        return directory_count, file_count

    def _walk_tree(self, root: Path) -> dict[Path, list[Path]]:
        """Walk the directory tree and collect comic files per directory.

        Runs synchronously in a thread pool.
        """
        dir_files, _directory_count, _file_count = self._walk_tree_with_counts(root)
        return dir_files

    def _walk_tree_with_counts(
        self,
        root: Path,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> tuple[dict[Path, list[Path]], int, int]:
        """Walk the directory tree once and return comic files plus live totals."""
        dir_files: dict[Path, list[Path]] = {}
        directory_count = 0
        file_count = 0
        last_emit_at = time.monotonic()

        def _on_error(exc: OSError) -> None:
            logger.warning("import_scan_walk_error", path=str(root), error=str(exc))

        for dirpath, dirnames, filenames in root.walk(on_error=_on_error):
            # Prune ignored directories
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
            dirnames.sort()
            filenames.sort()
            directory_count += 1

            comics: list[Path] = []
            for fname in filenames:
                fpath = dirpath / fname
                if _is_scan_candidate_file(fpath, self._extensions):
                    comics.append(fpath)
            file_count += len(comics)

            if comics:
                dir_files[dirpath] = comics

            if progress_callback is not None:
                now = time.monotonic()
                if (
                    directory_count == 1
                    or directory_count % 100 == 0
                    or (now - last_emit_at) >= SCAN_PROGRESS_EMIT_INTERVAL_SECONDS
                ):
                    progress_callback(directory_count, file_count)
                    last_emit_at = now

        if progress_callback is not None:
            progress_callback(directory_count, file_count)

        return dir_files, directory_count, file_count

    def _identify_series_dirs(self, dir_files: dict[Path, list[Path]]) -> list[Path]:
        """Identify leaf directories that should be treated as series roots.

        A directory is a series root if it contains comic files and none
        of its subdirectories also contain comic files.
        """
        all_dirs = set(dir_files.keys())
        series_dirs: list[Path] = []

        for d in all_dirs:
            # Check if any child directory also has comics
            has_child_with_comics = any(
                other != d and _is_descendant(other, d) for other in all_dirs
            )
            if not has_child_with_comics:
                series_dirs.append(d)

        return series_dirs

    def _extract_from_folder_name(self, folder_name: str) -> tuple[str, int | None]:
        """Extract (series_name, year) from a folder name.

        Handles:
        - "Batman (2016)"                   -> ("Batman", 2016)
        - "Batman"                          -> ("Batman", None)
        - "Batman Vol. 2 (2011)"            -> ("Batman Vol. 2", 2011)
        - "The Amazing Spider-Man  (2015)"  -> ("The Amazing Spider-Man", 2015)
        """
        m = _FOLDER_YEAR_RE.match(folder_name)
        if m:
            name = m.group(1).strip()
            year = int(m.group(2))
        else:
            name = folder_name.strip()
            year = None

            # Some staging or intermediate directories include extra release
            # suffixes after the year token, e.g. "(2014) (Digital) ... .#16".
            # Fall back to the filename parser in those cases so the scan logs
            # and CV matching still get a usable raw series/year signal.
            parsed = SourceMetadataExtractor().from_release_title(folder_name).parsed_release
            if parsed and parsed.series_name:
                name = parsed.series_name.strip()
                year = parsed.year

        # Normalize multiple spaces
        name = re.sub(r"\s{2,}", " ", name)
        # Strip trailing punctuation while preserving interior dots like "Vol."
        name = re.sub(r"[.,;:!]+$", "", name).strip()

        return name, year

    def _extract_folder_identity(self, folder_name: str) -> tuple[str, int | None, int | None]:
        """Extract a trusted ID from Pullbox's exact trailing folder-name contract."""
        match = _PULLBOX_FOLDER_CV_ID_RE.match(folder_name)
        if match is None:
            name, year = self._extract_from_folder_name(folder_name)
            return name, year, None
        return (
            re.sub(r"\s{2,}", " ", match.group("name")).strip(),
            int(match.group("year")),
            int(match.group("cv_id")),
        )

    def _infer_publisher_from_hierarchy(self, series_dir: Path, root: Path) -> str | None:
        """Infer publisher from the folder one level above the series folder.

        Returns publisher name if the parent folder is not the root and
        doesn't look like a year or generic container.
        """
        parent = series_dir.parent
        if parent in (root, series_dir):
            return None

        parent_name = parent.name

        # Skip if parent looks like a year
        if re.match(r"^\d{4}$", parent_name):
            return None

        # Skip generic container names
        generic = {"comics", "collection", "books", "downloads", "media", "library"}
        if parent_name.lower() in generic:
            return None

        # Skip very short names (likely drive letters or single chars)
        if len(parent_name) <= 2:
            return None

        # If parent's parent is the root, treat parent as publisher
        if parent.parent == root:
            return parent_name

        # For deeper hierarchies, check if grandparent is root
        # (publisher is typically one level below root)
        grandparent = parent.parent
        if grandparent == root:
            return parent_name

        return None


def _is_descendant(child: Path, parent: Path) -> bool:
    """Check if child is a descendant of parent."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
