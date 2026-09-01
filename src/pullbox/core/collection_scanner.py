"""File system collection scanner for comic import.

Walks a directory tree and extracts series candidates from folder names
and filenames. Yields DiscoveredSeries objects progressively as an async
generator, enabling SSE streaming during long-running scans.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sqlite3
import tempfile
import threading
import time
from contextlib import aclosing, closing, suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable

import structlog

from pullbox.core.collection_scan_grouping import (
    _has_strong_file_identity,
    _is_low_signal_file_series_name,
    _is_scan_candidate_file,
    _non_standard_group_discriminator,
    _normalize_series_identity,
    _should_collapse_to_folder_identity,
    _should_use_folder_identity_for_issue_title_file,
    _source_issue_type_for_group,
    _strip_year_volume_tokens,
    _type_qualified_issue_like_label,
)
from pullbox.core.exceptions import ConfigurationError
from pullbox.core.issue_numbers import format_issue_number
from pullbox.core.library_file_ownership import build_file_identity_signature
from pullbox.core.library_layout import (
    ImportLayoutMode,
    SourceLayoutMatch,
    SourceLayoutSpec,
    compile_source_layout,
    resolve_source_layout_spec,
)
from pullbox.core.naming_type_detection import detect_issue_type
from pullbox.core.release_parser import normalize_issue_number
from pullbox.core.source_metadata import MetadataSignal, SourceMetadata, SourceMetadataExtractor
from pullbox.models.issue import IssueType

logger = structlog.get_logger(__name__)

SERIES_SCAN_WORKERS = 4
ARCHIVE_READ_CONCURRENCY = 8
SCAN_ACTIVE_PATH_BUDGET = ARCHIVE_READ_CONCURRENCY * 2
SCAN_BUCKET_PAGE_SIZE = SERIES_SCAN_WORKERS
SCAN_SQL_READ_PAGE_SIZE = 256
SCAN_PROGRESS_EMIT_INTERVAL_SECONDS = 0.25
SCAN_CANCELLATION_POLL_INTERVAL_SECONDS = 0.05

_EXACT_METADATA_SIGNALS = frozenset(
    {
        MetadataSignal.COMICINFO,
        MetadataSignal.MYLAR3,
        MetadataSignal.PULLBOX_FOLDER,
        MetadataSignal.SIDECAR,
    }
)

# Regex: folder name with a year token, optionally followed by release tags
_FOLDER_YEAR_RE = re.compile(r"^(.+?)\s*[\[(](\d{4})[\])](?:\s*(?:\([^)]*\)|\[[^\]]*\]))*\s*$")
_PULLBOX_FOLDER_CV_ID_RE = re.compile(
    r"^(?P<name>.+?)\s+\((?P<year>(?:\d{4}|Unknown Year))\)\s+"
    r"(?:(?P<bare_cv_id>\d{4,})|\[cv-(?P<bracketed_cv_id>[1-9]\d*)\])\s*$",
    re.IGNORECASE,
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


def _validate_spool_path_text(value: str) -> None:
    """Fail closed when SQLite TEXT cannot losslessly represent a source path."""
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        msg = "Scan source path cannot be represented safely in the inventory spool"
        raise ConfigurationError(msg) from exc


def _directory_sort_key(value: str, *, windows: bool | None = None) -> str:
    """Match legacy Path ordering on the active platform."""
    use_windows_order = os.name == "nt" if windows is None else windows
    return value.casefold() if use_windows_order else value


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
    source_signature: dict[str, int | str] = field(default_factory=dict)
    source_folder_cohort_key: str | None = None
    source_ordinal: int | None = None


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


@dataclass(frozen=True, slots=True)
class _SpoolBuildResult:
    directory_count: int
    file_count: int
    leaf_directory_count: int
    active_path_high_water: int
    spool_bytes: int


@dataclass(frozen=True, slots=True)
class _SpoolBucket:
    scan_order: int
    relative_directory: str
    file_count: int
    is_leaf: bool


class _ScanInventorySpool:
    """Private disk-backed inventory containing root-relative paths only."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="pullbox-scan-")
        self._directory_path = Path(self._temporary_directory.name)
        self._directory_path.chmod(0o700)
        self._database_path = self._directory_path / "inventory.sqlite3"

    def close(self) -> None:
        """Remove the private spool after every scan outcome."""
        self._temporary_directory.cleanup()

    def build(
        self,
        progress_callback: Callable[[int, int], None] | None,
        cancellation_event: threading.Event,
        extensions: frozenset[str],
    ) -> _SpoolBuildResult:
        """Walk once, stage relative paths, and freeze global source ordinals."""
        directory_count = 0
        file_count = 0
        active_path_high_water = 0
        last_emit_at = time.monotonic()
        progress_handler_installed = False

        connection = sqlite3.connect(self._database_path)
        try:
            # The containing directory is already private, and the database itself
            # is restricted before any inventory rows are written.
            self._database_path.chmod(0o600)
            connection.execute("PRAGMA journal_mode = OFF")
            connection.execute("PRAGMA synchronous = OFF")
            connection.execute("PRAGMA temp_store = FILE")
            connection.execute("PRAGMA cache_size = -2048")
            connection.executescript(
                """
                CREATE TABLE buckets (
                    relative_directory TEXT PRIMARY KEY,
                    sort_relative_directory TEXT NOT NULL,
                    file_count INTEGER NOT NULL,
                    is_leaf INTEGER NOT NULL
                );
                CREATE TABLE staged_files (
                    relative_directory TEXT NOT NULL,
                    relative_path TEXT PRIMARY KEY,
                    folded_relative_path TEXT NOT NULL
                );
                """
            )

            def on_error(exc: OSError) -> None:
                logger.warning("import_scan_walk_error", path=str(self._root), error=str(exc))

            for dirpath, dirnames, filenames in self._root.walk(on_error=on_error):
                if cancellation_event.is_set():
                    break
                dirnames[:] = [
                    name
                    for name in dirnames
                    if name not in IGNORE_DIRS and not name.startswith(".")
                ]
                dirnames.sort()
                filenames.sort()
                directory_count += 1

                relative_directory_path = dirpath.relative_to(self._root)
                relative_directory = (
                    ""
                    if relative_directory_path == Path(".")
                    else relative_directory_path.as_posix()
                )
                bucket_file_count = 0
                for index, filename in enumerate(filenames):
                    if index % 128 == 0 and cancellation_event.is_set():
                        break
                    file_path = dirpath / filename
                    if not _is_scan_candidate_file(file_path, extensions):
                        continue
                    relative_path = file_path.relative_to(self._root).as_posix()
                    _validate_spool_path_text(relative_path)
                    connection.execute(
                        """
                        INSERT INTO staged_files (
                            relative_directory,
                            relative_path,
                            folded_relative_path
                        ) VALUES (?, ?, ?)
                        """,
                        (relative_directory, relative_path, relative_path.casefold()),
                    )
                    bucket_file_count += 1
                    file_count += 1

                if cancellation_event.is_set():
                    break
                active_path_high_water = max(active_path_high_water, bucket_file_count)
                if bucket_file_count:
                    _validate_spool_path_text(relative_directory)
                    connection.execute(
                        """
                        INSERT INTO buckets (
                            relative_directory,
                            sort_relative_directory,
                            file_count,
                            is_leaf
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            relative_directory,
                            _directory_sort_key(relative_directory),
                            bucket_file_count,
                            1,
                        ),
                    )
                    ancestor = dirpath.parent
                    while True:
                        try:
                            relative_ancestor_path = ancestor.relative_to(self._root)
                        except ValueError:
                            break
                        relative_ancestor = (
                            ""
                            if relative_ancestor_path == Path(".")
                            else relative_ancestor_path.as_posix()
                        )
                        connection.execute(
                            "UPDATE buckets SET is_leaf = ? WHERE relative_directory = ?",
                            (0, relative_ancestor),
                        )
                        if ancestor == self._root:
                            break
                        ancestor = ancestor.parent

                if progress_callback is not None:
                    now = time.monotonic()
                    if (
                        directory_count == 1
                        or (now - last_emit_at) >= SCAN_PROGRESS_EMIT_INTERVAL_SECONDS
                    ):
                        progress_callback(directory_count, file_count)
                        last_emit_at = now

            if progress_callback is not None:
                progress_callback(directory_count, file_count)

            leaf_directory_count = 0
            if cancellation_event.is_set():
                connection.rollback()
            else:
                connection.commit()
                connection.set_progress_handler(
                    lambda: 1 if cancellation_event.is_set() else 0,
                    1_000,
                )
                progress_handler_installed = True
                connection.executescript(
                    """
                    CREATE TABLE inventory_files (
                        relative_directory TEXT NOT NULL,
                        relative_path TEXT PRIMARY KEY,
                        source_ordinal INTEGER NOT NULL
                    );
                    INSERT INTO inventory_files (
                        relative_directory,
                        relative_path,
                        source_ordinal
                    )
                    SELECT
                        relative_directory,
                        relative_path,
                        ROW_NUMBER() OVER (
                            ORDER BY folded_relative_path, relative_path
                        )
                    FROM staged_files;
                    CREATE INDEX inventory_files_by_bucket
                        ON inventory_files (relative_directory, relative_path);

                    CREATE TABLE ordered_buckets (
                        scan_order INTEGER PRIMARY KEY,
                        relative_directory TEXT NOT NULL UNIQUE,
                        file_count INTEGER NOT NULL,
                        is_leaf INTEGER NOT NULL
                    );
                    INSERT INTO ordered_buckets (
                        scan_order,
                        relative_directory,
                        file_count,
                        is_leaf
                    )
                    SELECT
                        ROW_NUMBER() OVER (
                            ORDER BY CASE WHEN is_leaf = 1 THEN 0 ELSE 1 END,
                                     sort_relative_directory,
                                     relative_directory
                        ),
                        relative_directory,
                        file_count,
                        is_leaf
                    FROM buckets;

                    DROP TABLE staged_files;
                    DROP TABLE buckets;
                    """
                )
                row = connection.execute(
                    "SELECT COUNT(*) FROM ordered_buckets WHERE is_leaf = ?",
                    (1,),
                ).fetchone()
                leaf_directory_count = int(row[0]) if row is not None else 0
                connection.commit()
        finally:
            try:
                if progress_handler_installed:
                    connection.set_progress_handler(None, 0)
            finally:
                connection.close()

        spool_bytes = self._database_path.stat().st_size
        return _SpoolBuildResult(
            directory_count=directory_count,
            file_count=file_count,
            leaf_directory_count=leaf_directory_count,
            active_path_high_water=active_path_high_water,
            spool_bytes=spool_bytes,
        )

    def load_bucket_page(
        self,
        *,
        after_order: int,
        limit: int,
        cancellation_event: threading.Event | None = None,
    ) -> list[_SpoolBucket]:
        """Load one bounded page of bucket descriptors in legacy scan order."""
        with closing(sqlite3.connect(self._database_path)) as connection:
            if cancellation_event is not None:
                connection.set_progress_handler(
                    lambda: 1 if cancellation_event.is_set() else 0,
                    1_000,
                )
            try:
                rows = connection.execute(
                    """
                    SELECT scan_order, relative_directory, file_count, is_leaf
                    FROM ordered_buckets
                    WHERE scan_order > ?
                    ORDER BY scan_order
                    LIMIT ?
                    """,
                    (after_order, limit),
                ).fetchall()
            finally:
                if cancellation_event is not None:
                    connection.set_progress_handler(None, 0)
        return [
            _SpoolBucket(
                scan_order=int(row[0]),
                relative_directory=str(row[1]),
                file_count=int(row[2]),
                is_leaf=bool(row[3]),
            )
            for row in rows
        ]

    def load_bucket_files(
        self,
        relative_directory: str,
        cancellation_event: threading.Event | None = None,
    ) -> list[tuple[Path, int]]:
        """Materialize only one active bucket from root-relative spool rows."""
        materialized: list[tuple[Path, int]] = []
        with closing(sqlite3.connect(self._database_path)) as connection:
            if cancellation_event is not None:
                connection.set_progress_handler(
                    lambda: 1 if cancellation_event.is_set() else 0,
                    1_000,
                )
            try:
                cursor = connection.execute(
                    """
                    SELECT relative_path, source_ordinal
                    FROM inventory_files
                    WHERE relative_directory = ?
                    ORDER BY relative_path
                    """,
                    (relative_directory,),
                )
                while True:
                    if cancellation_event is not None and cancellation_event.is_set():
                        msg = "Inventory spool read interrupted"
                        raise sqlite3.OperationalError(msg)
                    page = cast(
                        "list[tuple[str, int]]",
                        cursor.fetchmany(SCAN_SQL_READ_PAGE_SIZE),
                    )
                    if not page:
                        break
                    for relative_path_value, source_ordinal in page:
                        relative_path = PurePosixPath(relative_path_value)
                        if relative_path.is_absolute() or ".." in relative_path.parts:
                            msg = "Inventory spool contained a non-relative source path"
                            raise ValueError(msg)
                        materialized.append(
                            (
                                self._root.joinpath(*relative_path.parts),
                                source_ordinal,
                            )
                        )
            finally:
                if cancellation_event is not None:
                    connection.set_progress_handler(None, 0)
        return materialized


class _CancellationBridge:
    """Poll cancellation inline while the scanner owns the current ``anext`` call."""

    def __init__(self, cancellation_check: Callable[[], Awaitable[None]] | None) -> None:
        self.event = threading.Event()
        self._cancellation_check = cancellation_check
        self._check_lock = asyncio.Lock()
        self._last_check_at = 0.0

    async def checkpoint(self) -> None:
        """Run the callback serially on the consumer's current scanner turn."""
        if self._cancellation_check is None or self.event.is_set():
            return
        async with self._check_lock:
            if self.event.is_set():
                return
            now = asyncio.get_running_loop().time()
            if now - self._last_check_at < SCAN_CANCELLATION_POLL_INTERVAL_SECONDS:
                return
            self._last_check_at = now
            try:
                await self._cancellation_check()
            except BaseException:
                self.event.set()
                raise

    async def run_blocking[BlockingResult](
        self,
        call: Callable[[], BlockingResult],
    ) -> BlockingResult:
        """Run thread work while prioritizing cooperative callback failures."""
        blocking_task = asyncio.create_task(asyncio.to_thread(call))
        try:
            if self._cancellation_check is None:
                return await asyncio.shield(blocking_task)
            while not blocking_task.done():
                done, _pending = await asyncio.wait(
                    {blocking_task},
                    timeout=SCAN_CANCELLATION_POLL_INTERVAL_SECONDS,
                )
                if done:
                    break
                await self.checkpoint()
            return blocking_task.result()
        except asyncio.CancelledError:
            self.event.set()
            await self._drain_blocking_task(blocking_task)
            raise
        except BaseException:
            self.event.set()
            await self._drain_blocking_task(blocking_task)
            raise

    @staticmethod
    async def _drain_blocking_task(blocking_task: asyncio.Task[object]) -> None:
        """Retrieve a worker outcome despite any repeated caller cancellation."""
        while not blocking_task.done():
            try:
                await asyncio.shield(blocking_task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        with suppress(BaseException):
            blocking_task.result()


class _CoalescingProgressMailbox:
    """Deliver only the latest pending progress snapshot in a bounded queue."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.queue: asyncio.Queue[tuple[int, int] | None] = asyncio.Queue(maxsize=1)

    def publish(self, directory_count: int, file_count: int) -> None:
        """Coalesce a thread-produced snapshot on the owning event loop."""
        self._loop.call_soon_threadsafe(
            self._offer,
            (directory_count, file_count),
        )

    def _offer(self, counts: tuple[int, int]) -> None:
        if self.queue.full():
            with suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
        self.queue.put_nowait(counts)

    async def finish(self, drain_task: asyncio.Task[None]) -> None:
        """Preserve the last snapshot, then stop and retrieve the drain task."""
        sentinel_task: asyncio.Task[None] | None = None
        try:
            # All publishers have stopped before finish is called. Yield once so
            # scheduled thread-safe callbacks run before the sentinel is added.
            await asyncio.sleep(0)
            if drain_task.done():
                await drain_task
                return

            sentinel_task = asyncio.create_task(self.queue.put(None))
            done, _pending = await asyncio.wait(
                {sentinel_task, drain_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if drain_task in done and not sentinel_task.done():
                sentinel_task.cancel()
                await asyncio.gather(sentinel_task, return_exceptions=True)
            else:
                await sentinel_task
            await drain_task
        except BaseException:
            cleanup_tasks = [drain_task]
            if sentinel_task is not None:
                cleanup_tasks.append(sentinel_task)
            for task in cleanup_tasks:
                if not task.done():
                    task.cancel()
            cleanup = asyncio.gather(*cleanup_tasks, return_exceptions=True)
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise


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
        cancellation_check: Callable[[], Awaitable[None]] | None = None,
        extensions: frozenset[str] | None = None,
        source_layout: SourceLayoutSpec | None = None,
    ) -> None:
        self._min_file_count = min_file_count
        self._max_sample_paths = max_sample_paths
        self._progress_callback = progress_callback
        self._file_progress_callback = file_progress_callback
        self._inventory_progress_callback = inventory_progress_callback
        self._cancellation_check = cancellation_check
        self._extensions = extensions or COMIC_EXTENSIONS
        self._source_layout = resolve_source_layout_spec(source_layout or SourceLayoutSpec())
        self._compiled_source_layout = (
            None
            if self._source_layout.mode == ImportLayoutMode.AUTO
            else compile_source_layout(self._source_layout)
        )
        self._retained_inventory_path_high_water = 0
        self._active_file_task_count = 0
        self._active_file_task_high_water = 0
        self._inventory_spool_bytes = 0

    @property
    def retained_inventory_path_high_water(self) -> int:
        """Largest in-memory filesystem inventory retained by this scanner."""
        return self._retained_inventory_path_high_water

    @property
    def active_file_task_high_water(self) -> int:
        """Largest number of per-file materialization tasks active at once."""
        return self._active_file_task_high_water

    @property
    def inventory_spool_bytes(self) -> int:
        """Size of the last folder scan's private inventory spool."""
        return self._inventory_spool_bytes

    async def inventory(self, root_path: str | Path) -> ScanInventory:
        """Count candidate directories and comic files before the main scan."""
        root = Path(root_path).resolve()
        if not root.is_dir():
            msg = f"Scan root is not a directory: {root}"
            raise ValueError(msg)

        cancellation_bridge = _CancellationBridge(self._cancellation_check)
        cancellation_event = cancellation_bridge.event
        progress_callback = self._inventory_progress_callback
        if progress_callback is None:
            directory_count, file_count = await cancellation_bridge.run_blocking(
                partial(
                    self._inventory_tree,
                    root,
                    None,
                    cancellation_event,
                )
            )
            return ScanInventory(directory_count=directory_count, file_count=file_count)

        mailbox = _CoalescingProgressMailbox(asyncio.get_running_loop())

        async def drain_inventory_progress() -> None:
            try:
                last_emitted: tuple[int, int] | None = None
                while True:
                    counts = await mailbox.queue.get()
                    if counts is None:
                        break
                    if counts != last_emitted:
                        last_emitted = counts
                        await progress_callback(*counts)
            except BaseException:
                cancellation_event.set()
                raise

        drain_task = asyncio.create_task(drain_inventory_progress())
        try:
            directory_count, file_count = await cancellation_bridge.run_blocking(
                partial(
                    self._inventory_tree,
                    root,
                    mailbox.publish,
                    cancellation_event,
                )
            )
        finally:
            await mailbox.finish(drain_task)

        return ScanInventory(directory_count=directory_count, file_count=file_count)

    async def scan(self, root_path: str | Path) -> AsyncGenerator[DiscoveredSeries, None]:
        """Yield series from a disk inventory with bounded ordinary bucket work.

        A single oversized directory is processed alone, but its paths and
        grouped results still materialize together until chunked grouping lands.
        """
        root = Path(root_path).resolve()
        if not root.is_dir():
            msg = f"Scan root is not a directory: {root}"
            raise ValueError(msg)

        self._retained_inventory_path_high_water = 0
        self._active_file_task_count = 0
        self._active_file_task_high_water = 0
        self._inventory_spool_bytes = 0

        log = logger.bind(scan_root=str(root))
        log.info("import_scan_started")
        cancellation_bridge = _CancellationBridge(self._cancellation_check)
        spool = _ScanInventorySpool(root)
        try:
            build_result = await self._build_spooled_inventory(spool, cancellation_bridge)
            self._retained_inventory_path_high_water = max(
                self._retained_inventory_path_high_water,
                build_result.active_path_high_water,
            )
            self._inventory_spool_bytes = build_result.spool_bytes

            loose_series_count = 0
            async with aclosing(
                self._iter_spooled_bucket_results(
                    spool=spool,
                    root=root,
                    cancellation_bridge=cancellation_bridge,
                )
            ) as bucket_results:
                async for candidates in bucket_results:
                    for candidate in candidates:
                        if len(candidate.files) < self._min_file_count:
                            continue
                        if (
                            candidate.source_folder_relative == "(root)"
                            or candidate.source_folder == str(root)
                        ):
                            loose_series_count += 1
                        yield candidate

            total_series = build_result.leaf_directory_count + loose_series_count
            log.info(
                "import_scan_completed",
                series_count=total_series,
                files_scanned=build_result.file_count,
                dirs_scanned=build_result.directory_count,
                loose_series=loose_series_count,
                inventory_spool_bytes=self._inventory_spool_bytes,
                retained_inventory_path_high_water=self._retained_inventory_path_high_water,
                active_file_task_high_water=self._active_file_task_high_water,
            )
        finally:
            spool.close()

    async def _build_spooled_inventory(
        self,
        spool: _ScanInventorySpool,
        cancellation_bridge: _CancellationBridge,
    ) -> _SpoolBuildResult:
        """Build the disk inventory while preserving live progress and cancellation."""
        cancellation_event = cancellation_bridge.event
        if self._progress_callback is None and self._inventory_progress_callback is None:
            return await cancellation_bridge.run_blocking(
                lambda: spool.build(
                    None,
                    cancellation_event,
                    self._extensions,
                )
            )

        mailbox = _CoalescingProgressMailbox(asyncio.get_running_loop())

        async def drain_progress() -> None:
            try:
                last_emitted: tuple[int, int] | None = None
                while True:
                    counts = await mailbox.queue.get()
                    if counts is None:
                        break
                    if counts != last_emitted:
                        last_emitted = counts
                        directory_count, file_count = counts
                        if self._progress_callback is not None:
                            await self._progress_callback(file_count, directory_count)
                        if self._inventory_progress_callback is not None:
                            await self._inventory_progress_callback(directory_count, file_count)
            except BaseException:
                cancellation_event.set()
                raise

        progress_task = asyncio.create_task(drain_progress())
        try:
            return await cancellation_bridge.run_blocking(
                lambda: spool.build(
                    mailbox.publish,
                    cancellation_event,
                    self._extensions,
                )
            )
        finally:
            await mailbox.finish(progress_task)

    async def _iter_spooled_bucket_results(
        self,
        *,
        spool: _ScanInventorySpool,
        root: Path,
        cancellation_bridge: _CancellationBridge,
    ) -> AsyncGenerator[list[DiscoveredSeries], None]:
        """Bound ordinary bucket work; process an oversized bucket alone."""
        comicinfo_sem = asyncio.Semaphore(ARCHIVE_READ_CONCURRENCY)
        file_task_slots = asyncio.Semaphore(ARCHIVE_READ_CONCURRENCY)
        series_worker_sem = asyncio.Semaphore(SERIES_SCAN_WORKERS)
        pending: dict[asyncio.Task[list[DiscoveredSeries]], int] = {}
        active_path_count = 0
        after_order = 0
        page: list[_SpoolBucket] = []
        page_index = 0
        next_bucket: _SpoolBucket | None = None
        exhausted = False

        async def load_next_bucket() -> _SpoolBucket | None:
            nonlocal after_order, page, page_index
            if page_index >= len(page):
                await cancellation_bridge.checkpoint()
                page = await cancellation_bridge.run_blocking(
                    partial(
                        spool.load_bucket_page,
                        after_order=after_order,
                        limit=SCAN_BUCKET_PAGE_SIZE,
                        cancellation_event=cancellation_bridge.event,
                    )
                )
                page_index = 0
                if not page:
                    return None
                after_order = page[-1].scan_order
            bucket = page[page_index]
            page_index += 1
            return bucket

        async def process_bucket(
            *,
            series_dir: Path,
            comic_files: list[Path],
            source_ordinal_by_path: dict[str, int],
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
                    file_task_slots=file_task_slots,
                    perform_cancellation_checks=False,
                    root=root,
                    folder_publisher=folder_publisher,
                    allow_weak_file_identity=allow_weak_file_identity,
                )
                self._stamp_source_folder_cohort(
                    discovered_files,
                    source_dir=series_dir,
                    root=root,
                    source_ordinal_by_path=source_ordinal_by_path,
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

        try:
            while pending or not exhausted:
                while len(pending) < SERIES_SCAN_WORKERS and not exhausted:
                    if next_bucket is None:
                        next_bucket = await load_next_bucket()
                        if next_bucket is None:
                            exhausted = True
                            break

                    bucket = next_bucket
                    if bucket.is_leaf and bucket.file_count < self._min_file_count:
                        next_bucket = None
                        continue

                    oversized = bucket.file_count > SCAN_ACTIVE_PATH_BUDGET
                    if oversized and pending:
                        break
                    if (
                        not oversized
                        and active_path_count + bucket.file_count > SCAN_ACTIVE_PATH_BUDGET
                    ):
                        break

                    next_bucket = None
                    relative_directory_path = PurePosixPath(bucket.relative_directory)
                    if (
                        relative_directory_path.is_absolute()
                        or ".." in relative_directory_path.parts
                    ):
                        msg = "Inventory spool contained a non-relative source directory"
                        raise ValueError(msg)
                    series_dir = root.joinpath(*relative_directory_path.parts)
                    bucket_files = await cancellation_bridge.run_blocking(
                        partial(
                            spool.load_bucket_files,
                            bucket.relative_directory,
                            cancellation_bridge.event,
                        )
                    )
                    if len(bucket_files) != bucket.file_count:
                        msg = "Inventory spool bucket count changed during materialization"
                        raise ValueError(msg)
                    comic_files = [path for path, _ordinal in bucket_files]
                    source_ordinal_by_path = {str(path): ordinal for path, ordinal in bucket_files}

                    if bucket.is_leaf:
                        folder_name, folder_year, folder_cv_id = self._extract_folder_identity(
                            series_dir.name
                        )
                        folder_publisher = self._infer_publisher_from_hierarchy(series_dir, root)
                    else:
                        folder_name = series_dir.name if series_dir.name else "(root)"
                        folder_year = None
                        folder_cv_id = None
                        folder_publisher = None

                    task = asyncio.create_task(
                        process_bucket(
                            series_dir=series_dir,
                            comic_files=comic_files,
                            source_ordinal_by_path=source_ordinal_by_path,
                            folder_name=folder_name,
                            folder_year=folder_year,
                            folder_publisher=folder_publisher,
                            folder_cv_id=folder_cv_id,
                            allow_weak_file_identity=series_dir == root,
                        )
                    )
                    pending[task] = bucket.file_count
                    active_path_count += bucket.file_count
                    self._retained_inventory_path_high_water = max(
                        self._retained_inventory_path_high_water,
                        active_path_count,
                    )
                    if oversized:
                        break

                if not pending:
                    continue

                # Keep inventory order deterministic even though adjacent
                # buckets are inspected concurrently. Completion timing must
                # not determine the order of the resulting review groups.
                oldest_task = next(iter(pending))
                done, _not_done = await asyncio.wait(
                    (oldest_task,),
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=SCAN_CANCELLATION_POLL_INTERVAL_SECONDS,
                )
                await cancellation_bridge.checkpoint()
                for task in done:
                    active_path_count -= pending.pop(task)
                    yield task.result()
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def scan_files(
        self,
        file_paths: list[str],
        *,
        root_path: str | Path | None = None,
    ) -> list[DiscoveredSeries]:
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
        root = Path(root_path).resolve() if root_path is not None else None
        source_ordinal_by_path = self._build_source_ordinal_index(
            (path for files in dir_files.values() for path in files),
            root=root,
        )

        for series_dir, comic_files in sorted(dir_files.items()):
            name, year, folder_cv_id = self._extract_folder_identity(series_dir.name)
            discovered_files = await self._build_discovered_files(
                comic_files,
                comicinfo_sem,
                root=root,
                allow_weak_file_identity=True,
            )
            if not discovered_files:
                continue
            self._stamp_source_folder_cohort(
                discovered_files,
                source_dir=series_dir,
                root=root,
                source_ordinal_by_path=source_ordinal_by_path,
            )

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

    @staticmethod
    def _relative_source_path(path: Path, *, root: Path | None) -> str:
        """Return a stable non-absolute source path when a scan root is known."""
        if root is not None:
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                pass
            else:
                return "(root)" if relative == "." else relative
        digest = hashlib.sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()
        return f"selected:{digest}"

    @classmethod
    def _build_source_ordinal_index(
        cls,
        paths: Iterable[Path],
        *,
        root: Path | None,
    ) -> dict[str, int]:
        """Assign deterministic one-based ordinals across a scan's source files."""
        path_items = list(paths)
        ordered = sorted(
            path_items,
            key=lambda item: (
                cls._relative_source_path(item, root=root).casefold(),
                cls._relative_source_path(item, root=root),
                str(item),
            ),
        )
        return {str(path): ordinal for ordinal, path in enumerate(ordered, start=1)}

    @classmethod
    def _stamp_source_folder_cohort(
        cls,
        discovered_files: list[DiscoveredFile],
        *,
        source_dir: Path,
        root: Path | None,
        source_ordinal_by_path: dict[str, int],
    ) -> None:
        """Attach the complete pre-split folder cohort to every discovered file."""
        cohort_key = cls._relative_source_path(source_dir, root=root)
        for discovered_file in discovered_files:
            discovered_file.source_folder_cohort_key = cohort_key
            discovered_file.source_ordinal = source_ordinal_by_path.get(discovered_file.file_path)

    async def _build_discovered_files(
        self,
        comic_files: list[Path],
        sem: asyncio.Semaphore,
        *,
        file_task_slots: asyncio.Semaphore | None = None,
        perform_cancellation_checks: bool = True,
        cancellation_checkpoint: Callable[[], Awaitable[None]] | None = None,
        root: Path | None = None,
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

            # Capture the identity used to detect scan-to-execution changes.
            try:
                source_signature = build_file_identity_signature(fpath)
                file_size = int(source_signature["size"])
            except (OSError, ConfigurationError):
                source_signature = {}
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

            parsed_series = metadata.series_name
            parsed_issue_number = metadata.issue_number
            parsed_year = metadata.year
            parsed_publisher = metadata.publisher
            issue_type = metadata.issue_type
            metadata_signals = dict(metadata.signals)
            metadata_diagnostics = dict(metadata.diagnostics)

            layout_match, relative_path = self._match_selected_layout(fpath, root=root)
            if self._compiled_source_layout is not None and relative_path is not None:
                layout_diagnostics: dict[str, object] = {
                    "fit": layout_match is not None,
                    "fallback_used": layout_match is None and self._source_layout.fallback_to_auto,
                    "relative_path": relative_path,
                }
                if layout_match is None and not self._source_layout.fallback_to_auto:
                    layout_diagnostics.update(
                        {
                            "review_required": True,
                            "review_reason": "selected_layout_no_match",
                        }
                    )
                if layout_match is not None and layout_match.issue_title is not None:
                    layout_diagnostics["issue_title"] = layout_match.issue_title
                metadata_diagnostics["source_layout"] = layout_diagnostics

            conflicts: dict[str, dict[str, object]] = {}

            def apply_layout_value(
                field_name: str,
                current: object,
                selected: object | None,
            ) -> object:
                if selected is None:
                    return current
                current_signal = metadata_signals.get(field_name)
                if current is not None and current_signal in _EXACT_METADATA_SIGNALS:
                    if str(current).casefold() != str(selected).casefold():
                        conflicts[field_name] = {
                            "selected": selected,
                            "preserved_signal": current_signal.value,
                        }
                    return current
                metadata_signals[field_name] = MetadataSignal.SOURCE_LAYOUT
                return selected

            layout_issue_number_raw: str | None = None
            if layout_match is not None:
                parsed_series = cast(
                    "str | None",
                    apply_layout_value(
                        "series_name",
                        parsed_series,
                        layout_match.series,
                    ),
                )
                parsed_year = cast(
                    "int | None",
                    apply_layout_value("year", parsed_year, layout_match.year),
                )
                parsed_publisher = cast(
                    "str | None",
                    apply_layout_value(
                        "publisher",
                        parsed_publisher,
                        layout_match.publisher,
                    ),
                )
                if layout_match.issue_number is not None:
                    selected_issue_number = normalize_issue_number(layout_match.issue_number)
                    parsed_issue_number = cast(
                        "float | None",
                        apply_layout_value(
                            "issue_number",
                            parsed_issue_number,
                            selected_issue_number,
                        ),
                    )
                    if metadata_signals.get("issue_number") == MetadataSignal.SOURCE_LAYOUT:
                        layout_issue_number_raw = layout_match.issue_number
                if layout_match.issue_type is not None:
                    selected_issue_type = IssueType(detect_issue_type(layout_match.issue_type))
                    issue_type = cast(
                        "IssueType",
                        apply_layout_value(
                            "issue_type",
                            issue_type,
                            selected_issue_type,
                        ),
                    )

            if conflicts:
                metadata_diagnostics["source_layout_conflicts"] = conflicts

            parsed = metadata.parsed_release
            issue_number_raw: str | None = None
            if layout_issue_number_raw is not None:
                issue_number_raw = layout_issue_number_raw
            elif parsed is not None and parsed.issue_number is not None:
                issue_number_raw = format_issue_number(parsed.issue_number)
            elif parsed_issue_number is not None:
                issue_number_raw = format_issue_number(parsed_issue_number)

            return DiscoveredFile(
                file_path=str(fpath),
                file_name=file_name,
                file_size=file_size,
                file_format=file_format,
                parsed_series=str(parsed_series) if parsed_series is not None else None,
                parsed_issue_number=(
                    float(parsed_issue_number) if parsed_issue_number is not None else None
                ),
                parsed_year=int(parsed_year) if parsed_year is not None else None,
                parsed_publisher=(str(parsed_publisher) if parsed_publisher is not None else None),
                has_comicinfo=bool(metadata.diagnostics.get("has_comicinfo")),
                comicvine_issue_id=metadata.comicvine_issue_id,
                issue_number_raw=issue_number_raw,
                issue_type=IssueType(issue_type),
                comicvine_series_id=metadata.comicvine_series_id,
                series_status=metadata.series_status,
                issue_count_hint=metadata.issue_count_hint,
                metadata_signals={
                    key: value.value if isinstance(value, MetadataSignal) else str(value)
                    for key, value in metadata_signals.items()
                },
                metadata_diagnostics=metadata_diagnostics,
                source_signature=source_signature,
            )

        async def _tracked_process_one(
            fpath: Path,
            *,
            is_series_sample: bool,
        ) -> DiscoveredFile:
            self._active_file_task_count += 1
            self._active_file_task_high_water = max(
                self._active_file_task_high_water,
                self._active_file_task_count,
            )
            try:
                return await _process_one(fpath, is_series_sample=is_series_sample)
            finally:
                self._active_file_task_count -= 1

        sorted_files = sorted(comic_files)
        task_slots = file_task_slots or asyncio.Semaphore(ARCHIVE_READ_CONCURRENCY)
        effective_checkpoint = cancellation_checkpoint
        if effective_checkpoint is None and perform_cancellation_checks:
            effective_checkpoint = self._cancellation_check
        next_index = 0
        pending: set[asyncio.Task[DiscoveredFile]] = set()
        results: list[DiscoveredFile] = []

        async def start_next() -> bool:
            nonlocal next_index
            if next_index >= len(sorted_files):
                return False
            if effective_checkpoint is None:
                await task_slots.acquire()
            else:
                while True:
                    try:
                        await asyncio.wait_for(
                            task_slots.acquire(),
                            timeout=SCAN_CANCELLATION_POLL_INTERVAL_SECONDS,
                        )
                    except TimeoutError:
                        await effective_checkpoint()
                        continue
                    break
            try:
                task = asyncio.create_task(
                    _tracked_process_one(
                        sorted_files[next_index],
                        is_series_sample=next_index == 0,
                    )
                )
            except BaseException:
                task_slots.release()
                raise
            task.add_done_callback(lambda _task: task_slots.release())
            pending.add(task)
            next_index += 1
            return True

        try:
            for _ in range(min(ARCHIVE_READ_CONCURRENCY, len(sorted_files))):
                await start_next()

            while pending:
                done, pending = await asyncio.wait(
                    pending,
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=(
                        SCAN_CANCELLATION_POLL_INTERVAL_SECONDS
                        if effective_checkpoint is not None
                        else None
                    ),
                )
                batch_results: list[DiscoveredFile] = []
                first_error: BaseException | None = None
                for task in done:
                    try:
                        batch_results.append(task.result())
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
                if first_error is not None:
                    raise first_error
                if effective_checkpoint is not None:
                    await effective_checkpoint()
                for discovered_file in batch_results:
                    results.append(discovered_file)
                    if self._file_progress_callback is not None:
                        await self._file_progress_callback(1)
                    await start_next()
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return sorted(results, key=lambda df: df.file_name)

    def _match_selected_layout(
        self,
        path: Path,
        *,
        root: Path | None,
    ) -> tuple[SourceLayoutMatch | None, str | None]:
        """Match one root-relative path against the frozen selected layout."""
        compiled = self._compiled_source_layout
        if compiled is None or root is None:
            return None, None
        try:
            relative_path = path.relative_to(root).as_posix()
        except ValueError:
            return None, None
        return compiled.match(relative_path), relative_path

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
        folder_is_series_boundary = root is None or source_dir != root

        for discovered_file in discovered_files:
            embedded_series_id = discovered_file.comicvine_series_id
            belongs_to_trusted_folder = folder_cv_id is not None and embedded_series_id in {
                None,
                folder_cv_id,
            }
            if belongs_to_trusted_folder or embedded_series_id is not None:
                # A Comic Vine volume ID is a stronger series boundary than an
                # issue filename's publication year or publisher text. Real
                # ComicInfo files commonly omit ``Volume`` while retaining the
                # issue-level ``Year``; grouping on that year splits one series
                # into a candidate per issue year. A trusted folder identity
                # also safely absorbs files whose archive metadata was deferred.
                exact_series_id = folder_cv_id if belongs_to_trusted_folder else embedded_series_id
                identity_key: tuple[str, int | None, str | None] = (
                    f"comicvine:{exact_series_id}",
                    None,
                    None,
                )
                classified.setdefault(identity_key, []).append(discovered_file)

                if belongs_to_trusted_folder:
                    discovered_file.parsed_series = folder_name
                    label_name = folder_name
                    label_year = folder_year
                    label_publisher = folder_publisher or discovered_file.parsed_publisher
                else:
                    label_name = discovered_file.parsed_series or folder_name
                    year_signal = discovered_file.metadata_signals.get("year")
                    label_year = (
                        discovered_file.parsed_year
                        if year_signal in {"comicinfo", "sidecar", "source_layout"}
                        else folder_year
                    )
                    label_publisher = discovered_file.parsed_publisher or folder_publisher

                existing_label = labels.get(identity_key)
                if existing_label is None:
                    labels[identity_key] = (label_name, label_year, label_publisher)
                else:
                    labels[identity_key] = (
                        existing_label[0],
                        existing_label[1] or label_year,
                        existing_label[2] or label_publisher,
                    )
                continue

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
                and (
                    _should_collapse_to_folder_identity(
                        discovered_file.parsed_series,
                        folder_name,
                    )
                    or (
                        folder_is_series_boundary
                        and _should_use_folder_identity_for_issue_title_file(
                            discovered_file,
                            folder_name,
                        )
                    )
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
        cancellation_event: threading.Event | None = None,
    ) -> tuple[int, int]:
        """Walk the tree and count visited directories plus supported comic files."""
        directory_count = 0
        file_count = 0
        last_emit_at = time.monotonic()

        def _on_error(exc: OSError) -> None:
            logger.warning("import_scan_walk_error", path=str(root), error=str(exc))

        for dirpath, dirnames, filenames in root.walk(on_error=_on_error):
            if cancellation_event is not None and cancellation_event.is_set():
                break
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
            dirnames.sort()
            filenames.sort()
            directory_count += 1
            for index, fname in enumerate(filenames):
                if (
                    index % 128 == 0
                    and cancellation_event is not None
                    and cancellation_event.is_set()
                ):
                    break
                if _is_scan_candidate_file(dirpath / fname, self._extensions):
                    file_count += 1
            if cancellation_event is not None and cancellation_event.is_set():
                break
            if progress_callback is not None:
                now = time.monotonic()
                if (
                    directory_count == 1
                    or (now - last_emit_at) >= SCAN_PROGRESS_EMIT_INTERVAL_SECONDS
                ):
                    progress_callback(directory_count, file_count)
                    last_emit_at = now

        if progress_callback is not None:
            progress_callback(directory_count, file_count)

        return directory_count, file_count

    def _identify_series_dirs(self, dir_files: dict[Path, list[Path]]) -> list[Path]:
        """Identify leaf directories that should be treated as series roots.

        A directory is a series root if it contains comic files and none
        of its subdirectories also contain comic files.
        """
        all_dirs = set(dir_files.keys())
        dirs_with_comic_descendants: set[Path] = set()

        # Walk each comic directory's ancestry once instead of comparing every
        # directory with every other directory. Path depth is bounded by the
        # source tree, so classification grows with D * depth rather than D^2.
        for directory in all_dirs:
            ancestor = directory.parent
            while True:
                if ancestor in all_dirs:
                    dirs_with_comic_descendants.add(ancestor)
                parent = ancestor.parent
                if parent == ancestor:
                    break
                ancestor = parent

        return [directory for directory in all_dirs if directory not in dirs_with_comic_descendants]

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
        comicvine_id = match.group("bare_cv_id") or match.group("bracketed_cv_id")
        assert comicvine_id is not None
        year_text = match.group("year")
        return (
            re.sub(r"\s{2,}", " ", match.group("name")).strip(),
            int(year_text) if year_text.isdigit() else None,
            int(comicvine_id),
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
