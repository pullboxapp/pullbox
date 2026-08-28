"""Short-session reader source resolution and bounded revisioned page delivery."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import anyio
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from pullbox.core.library_root_resolution import resolve_path_inside_roots
from pullbox.core.page_sources import (
    SUPPORTED_READER_FORMATS,
    PageSource,
    PageSourceError,
    PageSourceErrorCode,
    ReaderResourceLimits,
    open_page_source,
)
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

_MEDIA_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}
_T = TypeVar("_T")


class StaleReaderRevisionError(Exception):
    """Raised when a page URL references a replaced source revision."""


class ReaderWorkerBusyError(Exception):
    """Raised when bounded reader workers remain saturated past the wait budget."""


@dataclass(frozen=True, slots=True)
class ReaderCacheDiagnostics:
    """Path-free cache and worker facts safe for private diagnostics."""

    cache_file_count: int
    cache_bytes: int
    max_cache_bytes: int
    open_source_count: int
    max_open_sources: int
    max_workers: int


@dataclass(frozen=True, slots=True)
class ReaderCacheClearResult:
    """Result of deleting generated cache files without touching source comics."""

    files_removed: int
    bytes_removed: int


@dataclass(frozen=True, slots=True)
class ReaderSourceRecord:
    """Database-only reader source facts detached before filesystem work."""

    issue_id: int
    issue_title: str | None
    issue_number: str
    issue_number_value: float
    series_id: int
    series_title: str
    library_file_id: int
    file_path: str
    root_path: str
    file_format: FileFormat
    stored_file_hash: str | None


@dataclass(frozen=True, slots=True)
class ResolvedReaderSource:
    """Contained live source path and revision used outside a DB session."""

    issue_id: int
    issue_title: str | None
    issue_number: str
    issue_number_value: float
    series_id: int
    series_title: str
    library_file_id: int
    path: Path
    file_format: FileFormat
    revision: str


@dataclass(frozen=True, slots=True)
class ReaderManifest:
    """Format-neutral manifest details produced by the content service."""

    issue_id: int
    title: str
    issue_label: str
    format: FileFormat
    page_count: int
    revision: str


@dataclass(frozen=True, slots=True)
class ReaderPageFile:
    """One immutable revisioned cache file ready for streaming."""

    path: Path
    media_type: str
    etag: str


async def load_reader_source_record(session: AsyncSession, issue_id: int) -> ReaderSourceRecord:
    """Load reader metadata only; perform no filesystem or archive work."""
    result = await session.execute(
        select(Issue)
        .options(
            joinedload(Issue.series),
            joinedload(Issue.library_file).joinedload(LibraryFile.library_root),
        )
        .where(Issue.id == issue_id)
    )
    issue = result.unique().scalar_one_or_none()
    if issue is None or issue.status is not IssueStatus.OWNED or issue.library_file is None:
        raise PageSourceError(
            PageSourceErrorCode.MISSING_FILE,
            "This issue does not have a readable downloaded file.",
        )
    library_file = issue.library_file
    if library_file.file_format not in SUPPORTED_READER_FORMATS:
        raise PageSourceError(
            PageSourceErrorCode.UNSUPPORTED_FORMAT,
            "This downloaded format is not supported by the reader.",
        )
    return ReaderSourceRecord(
        issue_id=issue.id,
        issue_title=issue.title,
        issue_number=f"{issue.issue_number:g}",
        issue_number_value=issue.issue_number,
        series_id=issue.series_id,
        series_title=issue.series.title,
        library_file_id=library_file.id,
        file_path=library_file.file_path,
        root_path=library_file.library_root.path,
        file_format=library_file.file_format,
        stored_file_hash=library_file.file_hash,
    )


def resolve_reader_source(record: ReaderSourceRecord) -> ResolvedReaderSource:
    """Contain and stat a reader file after its database session is closed."""
    try:
        path = resolve_path_inside_roots(
            record.file_path,
            (record.root_path,),
            require_file=True,
        )
        stat = path.stat()
    except (OSError, ValueError) as exc:
        raise PageSourceError(
            PageSourceErrorCode.MISSING_FILE,
            "This downloaded file is no longer available.",
        ) from exc
    revision_input = (
        f"reader-v1:{record.library_file_id}:{stat.st_size}:{stat.st_mtime_ns}:"
        f"{record.stored_file_hash or ''}"
    )
    revision = hashlib.sha256(revision_input.encode()).hexdigest()[:32]
    return ResolvedReaderSource(
        issue_id=record.issue_id,
        issue_title=record.issue_title,
        issue_number=record.issue_number,
        issue_number_value=record.issue_number_value,
        series_id=record.series_id,
        series_title=record.series_title,
        library_file_id=record.library_file_id,
        path=path,
        file_format=record.file_format,
        revision=revision,
    )


class ReaderContentService:
    """Off-event-loop page indexing/rendering with bounded source and disk caches."""

    def __init__(
        self,
        *,
        cache_dir: Path,
        limits: ReaderResourceLimits | None = None,
        max_open_sources: int = 8,
        max_cache_bytes: int = 512 * 1024 * 1024,
        max_workers: int = 2,
        worker_wait_seconds: float = 2.0,
    ) -> None:
        self._cache_dir = cache_dir
        self._limits = limits or ReaderResourceLimits()
        self._max_open_sources = max(1, max_open_sources)
        self._max_cache_bytes = max(1, max_cache_bytes)
        self._worker_slots = asyncio.Semaphore(max(1, max_workers))
        self._max_workers = max(1, max_workers)
        self._worker_wait_seconds = max(0.001, worker_wait_seconds)
        self._sources: OrderedDict[str, PageSource] = OrderedDict()
        self._source_locks: dict[str, asyncio.Lock] = {}
        self._coordination_lock = asyncio.Lock()

    async def get_diagnostics(self) -> ReaderCacheDiagnostics:
        """Return cache usage and configured bounds without filesystem paths."""
        async with self._coordination_lock:
            open_source_count = len(self._sources)
        file_count, cache_bytes = await anyio.to_thread.run_sync(self._cache_usage)
        return ReaderCacheDiagnostics(
            cache_file_count=file_count,
            cache_bytes=cache_bytes,
            max_cache_bytes=self._max_cache_bytes,
            open_source_count=open_source_count,
            max_open_sources=self._max_open_sources,
            max_workers=self._max_workers,
        )

    async def clear_cache(self) -> ReaderCacheClearResult:
        """Delete only generated reader cache entries without following symlinks."""
        files_removed, bytes_removed = await anyio.to_thread.run_sync(self._clear_cache_files)
        return ReaderCacheClearResult(
            files_removed=files_removed,
            bytes_removed=bytes_removed,
        )

    async def get_manifest(self, source: ResolvedReaderSource) -> ReaderManifest:
        """Build a warm-cache-friendly manifest without retaining a DB session."""
        page_source = await self.get_page_source(source)
        title = source.issue_title or f"{source.series_title} #{source.issue_number}"
        return ReaderManifest(
            issue_id=source.issue_id,
            title=title,
            issue_label=f"{source.series_title} #{source.issue_number}",
            format=source.file_format,
            page_count=len(page_source.pages),
            revision=source.revision,
        )

    async def get_page_source(self, source: ResolvedReaderSource) -> PageSource:
        """Return one cached canonical page source, opening it once per revision."""
        lock = await self._source_lock(source.revision)
        async with lock:
            async with self._coordination_lock:
                cached = self._sources.get(source.revision)
                if cached is not None:
                    self._sources.move_to_end(source.revision)
                    return cached
            opened = await self._run_worker(
                lambda: anyio.to_thread.run_sync(
                    lambda: open_page_source(
                        source.path,
                        declared_format=source.file_format,
                        limits=self._limits,
                    ),
                    abandon_on_cancel=False,
                )
            )
            async with self._coordination_lock:
                self._sources[source.revision] = opened
                self._sources.move_to_end(source.revision)
                while len(self._sources) > self._max_open_sources:
                    expired_revision, _ = self._sources.popitem(last=False)
                    expired_lock = self._source_locks.get(expired_revision)
                    if expired_lock is not None and not expired_lock.locked():
                        self._source_locks.pop(expired_revision, None)
            return opened

    async def get_page(
        self,
        source: ResolvedReaderSource,
        *,
        page_index: int,
        revision: str,
    ) -> ReaderPageFile:
        """Return a single-flight immutable cache file for one page."""
        if revision != source.revision:
            raise StaleReaderRevisionError
        page_source = await self.get_page_source(source)
        if page_index < 0 or page_index >= len(page_source.pages):
            raise PageSourceError(
                PageSourceErrorCode.PAGE_OUT_OF_RANGE,
                "The requested comic page is outside the available range.",
            )
        descriptor = page_source.pages[page_index]
        suffix = _MEDIA_SUFFIXES.get(descriptor.media_type, ".bin")
        target = (
            self._cache_dir / source.revision[:2] / source.revision / f"{page_index:06d}{suffix}"
        )
        etag = f'"reader-{source.revision}-{page_index}"'
        if await anyio.to_thread.run_sync(target.is_file):
            return ReaderPageFile(path=target, media_type=descriptor.media_type, etag=etag)

        lock = await self._source_lock(source.revision)
        async with lock:
            if await anyio.to_thread.run_sync(target.is_file):
                return ReaderPageFile(path=target, media_type=descriptor.media_type, etag=etag)
            payload = await self._run_worker(
                lambda: anyio.to_thread.run_sync(
                    page_source.read_page, page_index, abandon_on_cancel=False
                )
            )
            await self._run_worker(
                lambda: anyio.to_thread.run_sync(
                    self._write_cache_file, target, payload.data, abandon_on_cancel=False
                )
            )
        return ReaderPageFile(path=target, media_type=payload.media_type, etag=etag)

    async def _run_worker(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        try:
            await asyncio.wait_for(
                self._worker_slots.acquire(),
                timeout=self._worker_wait_seconds,
            )
        except TimeoutError as exc:
            raise ReaderWorkerBusyError from exc
        try:
            worker_task = asyncio.ensure_future(operation())
        except BaseException:
            self._worker_slots.release()
            raise

        def _release_worker(completed: asyncio.Future[_T]) -> None:
            self._worker_slots.release()
            if not completed.cancelled():
                completed.exception()

        worker_task.add_done_callback(_release_worker)
        return await asyncio.shield(worker_task)

    async def _source_lock(self, revision: str) -> asyncio.Lock:
        async with self._coordination_lock:
            return self._source_locks.setdefault(revision, asyncio.Lock())

    def _write_cache_file(self, target: Path, data: bytes) -> None:
        if len(data) > self._max_cache_bytes:
            raise PageSourceError(
                PageSourceErrorCode.RESOURCE_LIMIT,
                "This comic page exceeds the configured reader cache limit.",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".reader-page-",
                dir=target.parent,
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, target)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        self._enforce_cache_budget(protected=target)

    def _enforce_cache_budget(self, *, protected: Path | None = None) -> None:
        files: list[tuple[float, int, Path]] = []
        total = 0
        if not self._cache_dir.exists():
            return
        for path in self._cache_dir.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            total += stat.st_size
            files.append((stat.st_mtime, stat.st_size, path))
        if total <= self._max_cache_bytes:
            return
        for _mtime, size, path in sorted(files):
            if protected is not None and path == protected:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            total -= size
            if total <= self._max_cache_bytes:
                break

    def _cache_usage(self) -> tuple[int, int]:
        file_count = 0
        cache_bytes = 0
        if not self._cache_dir.exists():
            return file_count, cache_bytes
        for path in self._cache_dir.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            file_count += 1
            cache_bytes += size
        return file_count, cache_bytes

    def _clear_cache_files(self) -> tuple[int, int]:
        files_removed = 0
        bytes_removed = 0
        if not self._cache_dir.exists():
            return files_removed, bytes_removed
        paths = sorted(
            self._cache_dir.rglob("*"),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for path in paths:
            try:
                if path.is_symlink():
                    path.unlink(missing_ok=True)
                    continue
                if path.is_file():
                    size = path.stat().st_size
                    path.unlink(missing_ok=True)
                    files_removed += 1
                    bytes_removed += size
                elif path.is_dir():
                    path.rmdir()
            except OSError:
                continue
        return files_removed, bytes_removed
