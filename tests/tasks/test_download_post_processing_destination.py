"""Download post-processing destination helper tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pullbox.models.library import LibraryFileStorageMode
from pullbox.tasks.post_processing_progress import PostProcessingPhase


class _FakeLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **kwargs: object) -> None:
        self.events.append((event, kwargs))

    def debug(self, event: str, **kwargs: object) -> None:
        self.events.append((event, kwargs))

    def info(self, event: str, **kwargs: object) -> None:
        self.events.append((event, kwargs))


class _FakeRuntime:
    def __init__(self) -> None:
        self.phases: list[PostProcessingPhase] = []
        self.summaries: list[str] = []

    def enter_phase(self, phase: PostProcessingPhase) -> None:
        self.phases.append(phase)

    def emit_summary(self, *, outcome: str) -> None:
        self.summaries.append(outcome)


class _FakeTrace:
    def __init__(self) -> None:
        self.file_size_bytes: int | None = None
        self.final_path: str | None = None
        self.finalized = False
        self.transferred_bytes: int | None = None

    def finalize_current_phase(self) -> None:
        self.finalized = True


@pytest.mark.asyncio
async def test_build_destination_plan_uses_probe_when_source_is_missing() -> None:
    """Missing sources should still resolve an expected destination from a cbz probe."""
    from pullbox.tasks.download_post_processing_destination import build_destination_plan

    captured_probe: list[Path] = []
    captured_root_ids: list[int | None] = []

    async def fake_resolve_destination(
        session: object,
        destination_probe: Path,
        issue: object,
        *,
        library_root_id: int | None,
    ) -> tuple[Path, Path]:
        _ = session, issue
        captured_probe.append(destination_probe)
        captured_root_ids.append(library_root_id)
        return Path("relative/Absolute Superman 009.cbz"), Path("relative")

    log = _FakeLog()
    plan = await build_destination_plan(
        session=object(),
        comic_file=None,
        series=SimpleNamespace(
            title="Absolute Superman",
            library_root_id=4,
            preferred_library_root_id=14,
        ),
        issue=SimpleNamespace(id=9),
        log=log,
        resolve_library_destination=fake_resolve_destination,
    )

    assert captured_probe == [Path("Absolute Superman #probe.cbz")]
    assert captured_root_ids == [14]
    assert plan.extension == "cbz"
    assert plan.dest_path.is_absolute()
    assert plan.issue_filename == "Absolute Superman 009.cbz"
    assert any(event == "post_processing_relative_dest" for event, _ in log.events)
    assert any(event == "post_processing_transfer_plan" for event, _ in log.events)


@pytest.mark.asyncio
async def test_build_destination_plan_uses_source_extension(tmp_path: Path) -> None:
    """Available source files should drive destination extension selection."""
    from pullbox.tasks.download_post_processing_destination import build_destination_plan

    source = tmp_path / "source.cbr"
    resolver = AsyncMock(return_value=(tmp_path / "Library" / "Issue.cbr", tmp_path / "Library"))

    plan = await build_destination_plan(
        session=object(),
        comic_file=source,
        series=SimpleNamespace(title="Series", library_root_id=12),
        issue=SimpleNamespace(id=1),
        log=_FakeLog(),
        resolve_library_destination=resolver,
    )

    resolver.assert_awaited_once()
    assert resolver.await_args.args[1] == source
    assert resolver.await_args.kwargs["library_root_id"] is None
    assert plan.extension == "cbr"
    assert plan.dest_path == tmp_path / "Library" / "Issue.cbr"


@pytest.mark.asyncio
async def test_find_existing_destination_file_recovers_alt_extension(
    tmp_path: Path,
) -> None:
    """Prior partial attempts can leave the moved file with a different extension."""
    from pullbox.tasks.download_post_processing_destination import (
        find_existing_destination_file,
    )

    dest_dir = tmp_path / "Library"
    dest_dir.mkdir()
    expected_dest = dest_dir / "Absolute Superman 009.cbz"
    alt_dest = dest_dir / "Absolute Superman 009.cbr"
    alt_dest.write_bytes(b"already moved")
    log = _FakeLog()

    recovered = await find_existing_destination_file(
        comic_file=None,
        dest_path=expected_dest,
        dest_dir=dest_dir,
        log=log,
    )

    assert recovered is not None
    assert recovered.path == alt_dest
    assert recovered.size_bytes == len(b"already moved")
    assert any(event == "post_processing_found_alt_extension" for event, _ in log.events)
    assert any(event == "post_processing_already_at_destination" for event, _ in log.events)


@pytest.mark.asyncio
async def test_find_existing_destination_file_ignores_same_source_and_destination(
    tmp_path: Path,
) -> None:
    """A source already in place should still flow through normal registration."""
    from pullbox.tasks.download_post_processing_destination import (
        find_existing_destination_file,
    )

    source = tmp_path / "Library" / "Issue.cbz"
    source.parent.mkdir()
    source.write_bytes(b"same file")

    recovered = await find_existing_destination_file(
        comic_file=source,
        dest_path=source,
        dest_dir=source.parent,
        log=_FakeLog(),
    )

    assert recovered is None


@pytest.mark.asyncio
async def test_register_existing_destination_file_records_recovered_file(
    tmp_path: Path,
) -> None:
    """Recovered files should be registered without another library move."""
    from pullbox.tasks.download_post_processing_destination import (
        ExistingDestinationFile,
        register_existing_destination_file,
    )

    dest_path = tmp_path / "Library" / "Issue.cbr"
    dest_path.parent.mkdir()
    dest_path.write_bytes(b"already moved")
    register_calls: list[tuple[Any, ...]] = []

    async def fake_register_library_file(*args: Any, **kwargs: Any) -> None:
        register_calls.append((*args, kwargs))

    trace = _FakeTrace()
    runtime = _FakeRuntime()
    log = _FakeLog()
    download = SimpleNamespace(final_path=None)

    result = await register_existing_destination_file(
        session=object(),
        existing_destination=ExistingDestinationFile(
            path=dest_path,
            size_bytes=dest_path.stat().st_size,
        ),
        issue=SimpleNamespace(id=1),
        series=SimpleNamespace(library_root_id=8, preferred_library_root_id=18),
        download=download,
        trace=trace,
        runtime=runtime,
        log=log,
        register_library_file=fake_register_library_file,
    )

    assert result == dest_path
    assert download.final_path == str(dest_path)
    assert trace.final_path == str(dest_path)
    assert trace.file_size_bytes == len(b"already moved")
    assert trace.transferred_bytes == len(b"already moved")
    assert trace.finalized is True
    assert runtime.phases == [PostProcessingPhase.REGISTERING_LIBRARY_FILE]
    assert runtime.summaries == ["success"]
    assert register_calls
    *_, kwargs = register_calls[0]
    assert kwargs["move_to_library"] is False
    assert kwargs["storage_mode"] is LibraryFileStorageMode.MANAGED
    assert kwargs["recover_existing_managed_artifact"] is True
    assert kwargs["rename"] is False
    assert kwargs["library_root_id"] == 18
    assert any(event == "post_processing_complete" for event, _ in log.events)
