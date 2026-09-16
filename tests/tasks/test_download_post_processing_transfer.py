"""Download post-processing transfer helper tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from pullbox.tasks.post_processing_progress import PostProcessingPhase

if TYPE_CHECKING:
    from pathlib import Path


class _FakeRuntime:
    def __init__(self) -> None:
        self.phases: list[PostProcessingPhase] = []

    def enter_phase(self, phase: PostProcessingPhase) -> None:
        self.phases.append(phase)


class _FakeTrace:
    def __init__(self) -> None:
        self.configured_transfer_method: str | None = None
        self.effective_transfer_method: str | None = None
        self.file_size_bytes: int | None = None
        self.final_path: str | None = None
        self.finalized = False
        self.seed_safe_torrent_import = False
        self.source_preserved: bool | None = None
        self.transfer_method: str | None = None
        self.transferred_bytes: int | None = None

    def finalize_current_phase(self) -> None:
        self.finalized = True


class _FakeLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.events.append((event, kwargs))


@pytest.mark.asyncio
async def test_transfer_and_register_tracks_progress_and_final_state(
    tmp_path: Path,
) -> None:
    """Transfer progress should drive phase changes and final trace metadata."""
    from pullbox.tasks.download_post_processing_transfer import (
        transfer_and_register_library_file,
    )

    source = tmp_path / "download.cbz"
    source.write_bytes(b"comic")
    destination = tmp_path / "library" / "Issue.cbz"
    progress_calls: list[tuple[int, int, int]] = []
    infer_kwargs: dict[str, Any] = {}

    async def fake_register_library_file(
        session: object,
        source_path: Path,
        issue: object,
        confidence: object,
        *,
        move_to_library: bool,
        library_root_id: int | None,
        transfer_progress_callback: object,
        download_client: object,
        replace_existing_library_file: bool,
        replacement_trash_dir: Path | None,
    ) -> object:
        _ = session, issue, download_client
        assert source_path == source
        assert confidence.value == "high"
        assert move_to_library is True
        assert library_root_id == 22
        assert replace_existing_library_file is False
        assert replacement_trash_dir is None
        assert callable(transfer_progress_callback)
        transfer_progress_callback(5, 10)
        transfer_progress_callback(10, 10)
        return SimpleNamespace(file_path=str(destination), file_size=10)

    def fake_set_transfer_progress(
        download_id: int,
        *,
        total_bytes: int,
        done_bytes: int,
    ) -> None:
        progress_calls.append((download_id, total_bytes, done_bytes))

    def fake_infer_effective_transfer_method(**kwargs: Any) -> str:
        infer_kwargs.update(kwargs)
        return "hardlink"

    trace = _FakeTrace()
    runtime = _FakeRuntime()
    log = _FakeLog()

    dest_path = await transfer_and_register_library_file(
        session=object(),
        comic_file=source,
        issue=SimpleNamespace(id=1),
        series=SimpleNamespace(library_root_id=12, preferred_library_root_id=22),
        download=SimpleNamespace(id=44, download_client="sabnzbd"),
        ingest_policy=SimpleNamespace(post_processing_method="move"),
        trace=trace,
        runtime=runtime,
        log=log,
        register_library_file=fake_register_library_file,
        set_transfer_progress=fake_set_transfer_progress,
        infer_effective_transfer_method=fake_infer_effective_transfer_method,
    )

    assert dest_path == destination
    assert progress_calls == [(44, 10, 5), (44, 10, 10)]
    assert runtime.phases == [
        PostProcessingPhase.TRANSFERRING_FILE,
        PostProcessingPhase.REGISTERING_LIBRARY_FILE,
    ]
    assert trace.configured_transfer_method == "move"
    assert trace.transfer_method == "move"
    assert trace.effective_transfer_method == "hardlink"
    assert trace.final_path == str(destination)
    assert trace.file_size_bytes == 10
    assert trace.transferred_bytes == 10
    assert trace.source_preserved is True
    assert trace.finalized is True
    assert infer_kwargs["source_path"] == source
    assert infer_kwargs["destination_path"] == destination
    assert any(event == "post_processing_transferred" for event, _ in log.events)
