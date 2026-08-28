"""TDD coverage for reader source resolution, revisions, and bounded page caching."""

from __future__ import annotations

import asyncio
import dataclasses
import gc
import io
import threading
import time
import zipfile
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import psutil
import pytest
from PIL import Image

from pullbox.core.page_sources import PageSourceError, PageSourceErrorCode
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.services.reader_content_service import (
    ReaderContentService,
    ReaderSourceRecord,
    ReaderWorkerBusyError,
    StaleReaderRevisionError,
    load_reader_source_record,
    resolve_reader_source,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


def _write_cbz(path: Path, page_count: int = 3) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for index in range(page_count):
            output = io.BytesIO()
            with Image.new("P", (1, 1), color=index) as image:
                image.save(output, format="GIF")
            archive.writestr(f"page-{index + 1}.gif", output.getvalue() + bytes([index]))


def _record(source: Path, root: Path) -> ReaderSourceRecord:
    return ReaderSourceRecord(
        issue_id=7,
        issue_title="A Reader Issue",
        issue_number="1",
        issue_number_value=1.0,
        series_id=3,
        series_title="Reader Series",
        library_file_id=11,
        file_path=str(source),
        root_path=str(root),
        file_format=FileFormat.CBZ,
        stored_file_hash=None,
    )


async def _seed_issue(
    session: AsyncSession,
    *,
    root_path: Path,
    file_path: Path,
) -> int:
    series = Series(
        comicvine_id=700_001,
        title="Reader Series",
        sort_title="reader series",
        year_start=2026,
        status=SeriesStatus.CONTINUING,
        series_type=SeriesType.STANDARD,
        monitored=True,
        issue_count=1,
    )
    session.add(series)
    await session.flush()
    issue = Issue(
        series_id=series.id,
        comicvine_id=700_002,
        issue_number=1,
        title="A Reader Issue",
        status=IssueStatus.OWNED,
    )
    session.add(issue)
    await session.flush()
    root = LibraryRoot(name="reader", path=str(root_path))
    session.add(root)
    await session.flush()
    source_stat = file_path.stat()
    session.add(
        LibraryFile(
            file_path=str(file_path),
            file_name=file_path.name,
            file_size=source_stat.st_size,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.fromtimestamp(source_stat.st_mtime, tz=UTC),
            issue_id=issue.id,
            match_confidence=MatchConfidence.HIGH,
            library_root_id=root.id,
        )
    )
    await session.flush()
    return issue.id


@pytest.mark.asyncio
async def test_load_record_is_database_only_and_resolution_enforces_root(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    outside = tmp_path / "outside.cbz"
    _write_cbz(outside)
    issue_id = await _seed_issue(
        db_session,
        root_path=root,
        file_path=outside,
    )

    record = await load_reader_source_record(db_session, issue_id)

    with pytest.raises(PageSourceError) as exc_info:
        resolve_reader_source(record)
    assert exc_info.value.code is PageSourceErrorCode.MISSING_FILE


@pytest.mark.asyncio
async def test_manifest_and_page_cache_use_revisioned_files(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "book.cbz"
    _write_cbz(source)
    service = ReaderContentService(cache_dir=tmp_path / "cache")
    resolved = resolve_reader_source(_record(source, root))

    manifest = await service.get_manifest(resolved)
    page = await service.get_page(resolved, page_index=1, revision=resolved.revision)
    cached = await service.get_page(resolved, page_index=1, revision=resolved.revision)

    assert manifest.page_count == 3
    assert manifest.revision == resolved.revision
    assert manifest.format is FileFormat.CBZ
    assert page.path == cached.path
    assert page.path.is_file()
    assert page.path.read_bytes().endswith(b"\x01")
    assert page.media_type == "image/gif"
    assert page.etag == f'"reader-{resolved.revision}-1"'


@pytest.mark.asyncio
async def test_stale_revision_is_rejected_before_page_work(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "book.cbz"
    _write_cbz(source)
    service = ReaderContentService(cache_dir=tmp_path / "cache")
    resolved = resolve_reader_source(_record(source, root))

    with pytest.raises(StaleReaderRevisionError):
        await service.get_page(resolved, page_index=0, revision="stale")


@pytest.mark.asyncio
async def test_identical_cold_requests_are_single_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "book.cbz"
    _write_cbz(source)
    service = ReaderContentService(cache_dir=tmp_path / "cache")
    resolved = resolve_reader_source(_record(source, root))
    page_source = await service.get_page_source(resolved)
    original_read = page_source.read_page
    read_count = 0

    def _counted_read(index: int):  # type: ignore[no-untyped-def]
        nonlocal read_count
        read_count += 1
        return original_read(index)

    monkeypatch.setattr(page_source, "read_page", _counted_read)

    pages = await asyncio.gather(
        service.get_page(resolved, page_index=0, revision=resolved.revision),
        service.get_page(resolved, page_index=0, revision=resolved.revision),
        service.get_page(resolved, page_index=0, revision=resolved.revision),
    )

    assert read_count == 1
    assert len({page.path for page in pages}) == 1


@pytest.mark.asyncio
async def test_expensive_reader_workers_are_globally_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.services import reader_content_service as content_module

    root = tmp_path / "library"
    root.mkdir()
    first_source = root / "first.cbz"
    second_source = root / "second.cbz"
    _write_cbz(first_source)
    _write_cbz(second_source)
    first = resolve_reader_source(_record(first_source, root))
    second = resolve_reader_source(
        dataclasses.replace(_record(second_source, root), library_file_id=12)
    )
    original_open = content_module.open_page_source
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def _tracked_open(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.08)
            return original_open(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(content_module, "open_page_source", _tracked_open)
    service = ReaderContentService(cache_dir=tmp_path / "cache", max_workers=1)

    await asyncio.gather(service.get_manifest(first), service.get_manifest(second))

    assert maximum_active == 1


@pytest.mark.asyncio
async def test_page_larger_than_cache_budget_is_rejected_without_retention(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "book.cbz"
    _write_cbz(source)
    service = ReaderContentService(cache_dir=tmp_path / "cache", max_cache_bytes=1)
    resolved = resolve_reader_source(_record(source, root))

    with pytest.raises(PageSourceError) as exc_info:
        await service.get_page(resolved, page_index=0, revision=resolved.revision)

    diagnostics = await service.get_diagnostics()
    assert exc_info.value.code is PageSourceErrorCode.RESOURCE_LIMIT
    assert diagnostics.cache_file_count == 0
    assert diagnostics.cache_bytes == 0


@pytest.mark.asyncio
async def test_worker_saturation_fails_with_a_bounded_retryable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.services import reader_content_service as content_module

    root = tmp_path / "library"
    root.mkdir()
    first_source = root / "first.cbz"
    second_source = root / "second.cbz"
    _write_cbz(first_source)
    _write_cbz(second_source)
    first = resolve_reader_source(_record(first_source, root))
    second = resolve_reader_source(
        dataclasses.replace(_record(second_source, root), library_file_id=12)
    )
    original_open = content_module.open_page_source
    started = threading.Event()
    release = threading.Event()

    def _blocked_open(path, **kwargs):  # type: ignore[no-untyped-def]
        if path == first_source:
            started.set()
            release.wait(timeout=2)
        return original_open(path, **kwargs)

    monkeypatch.setattr(content_module, "open_page_source", _blocked_open)
    service = ReaderContentService(
        cache_dir=tmp_path / "cache",
        max_workers=1,
        worker_wait_seconds=0.02,
    )
    first_request = asyncio.create_task(service.get_manifest(first))
    await asyncio.to_thread(started.wait, 1)

    with pytest.raises(ReaderWorkerBusyError):
        await service.get_manifest(second)

    release.set()
    await first_request


@pytest.mark.asyncio
async def test_cancelled_reader_request_holds_worker_slot_until_thread_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pullbox.services import reader_content_service as content_module

    root = tmp_path / "library"
    root.mkdir()
    first_source = root / "first.cbz"
    second_source = root / "second.cbz"
    _write_cbz(first_source)
    _write_cbz(second_source)
    first = resolve_reader_source(_record(first_source, root))
    second = resolve_reader_source(
        dataclasses.replace(_record(second_source, root), library_file_id=12)
    )
    original_open = content_module.open_page_source
    started = threading.Event()
    release = threading.Event()

    def _blocked_open(path, **kwargs):  # type: ignore[no-untyped-def]
        if path == first_source:
            started.set()
            release.wait(timeout=2)
        return original_open(path, **kwargs)

    monkeypatch.setattr(content_module, "open_page_source", _blocked_open)
    service = ReaderContentService(
        cache_dir=tmp_path / "cache",
        max_workers=1,
        worker_wait_seconds=0.02,
    )
    cancelled_request = asyncio.create_task(service.get_manifest(first))
    await asyncio.to_thread(started.wait, 1)

    cancelled_request.cancel()
    await asyncio.sleep(0)
    with pytest.raises(ReaderWorkerBusyError):
        await service.get_manifest(second)

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_request
    assert (await service.get_manifest(second)).page_count == 3


@pytest.mark.asyncio
async def test_cache_diagnostics_and_clear_are_bounded_to_generated_reader_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "book.cbz"
    _write_cbz(source)
    service = ReaderContentService(cache_dir=tmp_path / "cache")
    resolved = resolve_reader_source(_record(source, root))
    page = await service.get_page(resolved, page_index=0, revision=resolved.revision)
    page_size = page.path.stat().st_size

    before = await service.get_diagnostics()
    cleared = await service.clear_cache()
    after = await service.get_diagnostics()

    assert before.cache_file_count == 1
    assert before.cache_bytes == page_size
    assert before.max_cache_bytes == 512 * 1024 * 1024
    assert cleared.files_removed == 1
    assert after.cache_file_count == 0
    assert source.is_file()


@pytest.mark.asyncio
async def test_fifty_issue_manifests_keep_server_resources_bounded(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    service = ReaderContentService(
        cache_dir=tmp_path / "cache",
        max_open_sources=3,
        max_workers=2,
    )
    process = psutil.Process()
    before_rss = process.memory_info().rss
    before_fds = process.num_fds()
    before_children = len(process.children())

    for issue_id in range(1, 52):
        source = root / f"issue-{issue_id}.cbz"
        _write_cbz(source, page_count=2)
        record = dataclasses.replace(
            _record(source, root),
            issue_id=issue_id,
            library_file_id=issue_id,
        )
        manifest = await service.get_manifest(resolve_reader_source(record))
        assert manifest.page_count == 2

    gc.collect()
    await asyncio.sleep(0)
    diagnostics = await service.get_diagnostics()
    after_rss = process.memory_info().rss
    after_fds = process.num_fds()
    after_children = len(process.children())

    assert diagnostics.open_source_count == 3
    assert diagnostics.open_source_count <= diagnostics.max_open_sources
    assert diagnostics.cache_file_count == 0
    assert after_fds <= before_fds + 2
    assert after_children == before_children
    assert after_rss - before_rss < 32 * 1024 * 1024
    print(
        "reader_resource_scale "
        f"switches=50 open_sources={diagnostics.open_source_count} "
        f"fd_delta={after_fds - before_fds} child_delta={after_children - before_children} "
        f"rss_delta_bytes={after_rss - before_rss}"
    )


def test_content_revision_changes_when_source_changes(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "book.cbz"
    _write_cbz(source, page_count=1)
    record = _record(source, root)

    first = resolve_reader_source(record)
    with source.open("ab") as handle:
        handle.write(b"revision-change")
    second = resolve_reader_source(record)

    assert first.revision != second.revision
