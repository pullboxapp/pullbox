"""Unit coverage for background manual issue import progress handling."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pullbox.tasks import issue_import_task


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _FakeSessionFactory:
    def __call__(self) -> _FakeSession:
        return _FakeSession()


@pytest.mark.asyncio
async def test_cancel_issue_import_run_marks_active_task_cancelled() -> None:
    """Cancelling an in-flight manual import should request cooperative cancellation."""

    issue_import_task._issue_import_states.clear()
    issue_import_task._issue_import_tasks.clear()
    issue_import_task._issue_import_cancel_requests.clear()

    async def cooperative_import() -> None:
        while 42 not in issue_import_task._issue_import_cancel_requests:
            await asyncio.sleep(0)
        issue_import_task._issue_import_cancel_requests.discard(42)
        issue_import_task._set_issue_import_state(
            42,
            state="cancelled",
            message="Import cancelled.",
        )

    task = asyncio.create_task(cooperative_import())
    issue_import_task._issue_import_tasks[42] = task
    issue_import_task._issue_import_states[42] = issue_import_task.ManualFileImportProgressResponse(
        issue_id=42,
        state="running",
        message="Importing selected file...",
    )

    try:
        state = await issue_import_task.cancel_issue_import_run(42)

        assert state.state == "cancelled"
        assert state.message == "Import cancelled."
        assert task.done()
        assert not task.cancelled()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        issue_import_task._issue_import_tasks.clear()
        issue_import_task._issue_import_states.clear()
        issue_import_task._issue_import_cancel_requests.clear()


@pytest.mark.asyncio
async def test_cancel_issue_import_run_reports_completed_if_safe_point_passed() -> None:
    """A cancel request must not publish cancelled while file work keeps running."""

    issue_import_task._issue_import_states.clear()
    issue_import_task._issue_import_tasks.clear()
    issue_import_task._issue_import_cancel_requests.clear()

    async def non_interruptible_import() -> None:
        await asyncio.sleep(0.01)
        issue_import_task._issue_import_cancel_requests.discard(43)
        issue_import_task._set_issue_import_state(
            43,
            state="completed",
            message="Import completed before cancellation could safely stop.",
        )

    task = asyncio.create_task(non_interruptible_import())
    issue_import_task._issue_import_tasks[43] = task
    issue_import_task._issue_import_states[43] = issue_import_task.ManualFileImportProgressResponse(
        issue_id=43,
        state="running",
        message="Importing selected file...",
    )

    try:
        state = await issue_import_task.cancel_issue_import_run(43)

        assert state.state == "completed"
        assert state.message == "Import completed before cancellation could safely stop."
        assert task.done()
        assert not task.cancelled()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        issue_import_task._issue_import_tasks.clear()
        issue_import_task._issue_import_states.clear()
        issue_import_task._issue_import_cancel_requests.clear()


@pytest.mark.asyncio
async def test_run_issue_import_accepts_positional_progress_callbacks(monkeypatch) -> None:
    """Converter-style positional progress callbacks should not fail the run."""

    issue_import_task._issue_import_states.clear()

    prepared = SimpleNamespace(
        source_path=Path("/tmp/imports/Henchgirl 001.pdf"),
        issue=SimpleNamespace(series=SimpleNamespace(library_root_id=1)),
        issue_id=7,
        ingest_policy=SimpleNamespace(
            normalize_imported_archives_to_cbz=True,
            update_embedded_comicinfo_from_match=True,
        ),
    )

    async def fake_execute_manual_issue_import(
        session,
        prepared_arg,
        *,
        allow_resource_safety_exception=False,
        preparation_progress_callback=None,
        transfer_progress_callback=None,
        comicinfo_progress_callback=None,
        cancellation_check=None,
    ):
        assert prepared_arg is prepared
        assert allow_resource_safety_exception is True
        assert preparation_progress_callback is not None
        assert transfer_progress_callback is not None
        assert comicinfo_progress_callback is not None
        assert cancellation_check is not None
        await cancellation_check()

        preparation_progress_callback("rendering", 1, 4, "pages")
        transfer_progress_callback(512, 1024)
        comicinfo_progress_callback("rewriting", 1, 2, "entries")

        return SimpleNamespace(
            issue_id=7,
            library_file=SimpleNamespace(
                id=99,
                file_name="Henchgirl 001.cbz",
                file_path="/comics/Henchgirl (2020)/Henchgirl 001.cbz",
                file_size=123456,
                file_format=SimpleNamespace(value="cbz"),
                match_confidence=SimpleNamespace(value="manual"),
            ),
        )

    monkeypatch.setattr(
        issue_import_task,
        "get_session_factory",
        lambda: _FakeSessionFactory(),
    )
    monkeypatch.setattr(
        issue_import_task,
        "prepare_manual_issue_import",
        AsyncMock(return_value=prepared),
    )
    monkeypatch.setattr(
        issue_import_task,
        "execute_manual_issue_import",
        fake_execute_manual_issue_import,
    )

    await issue_import_task._run_issue_import(
        7,
        {
            "file_path": "/tmp/imports/Henchgirl 001.pdf",
            "move_to_library": True,
            "allow_resource_safety_exception": True,
        },
    )

    state = issue_import_task.get_issue_import_progress_state(7)
    assert state is not None
    assert state.state == "completed"
    assert state.file_name == "Henchgirl 001.cbz"
    assert state.match_confidence == "manual"


@pytest.mark.asyncio
async def test_run_issue_import_accepts_worker_thread_progress_callbacks(monkeypatch) -> None:
    """Off-thread ComicInfo materialization should still update live progress."""

    issue_import_task._issue_import_states.clear()

    prepared = SimpleNamespace(
        source_path=Path("/tmp/imports/Aliens Epic Collection.cbz"),
        issue=SimpleNamespace(series=SimpleNamespace(library_root_id=1)),
        issue_id=11,
        ingest_policy=SimpleNamespace(
            normalize_imported_archives_to_cbz=False,
            update_embedded_comicinfo_from_match=True,
        ),
    )

    async def fake_execute_manual_issue_import(
        session,
        prepared_arg,
        *,
        allow_resource_safety_exception=False,
        preparation_progress_callback=None,
        transfer_progress_callback=None,
        comicinfo_progress_callback=None,
        cancellation_check=None,
    ):
        assert prepared_arg is prepared
        assert comicinfo_progress_callback is not None

        def emit_from_worker_thread() -> None:
            comicinfo_progress_callback("transferring", 512, 1024, "bytes")

        await asyncio.to_thread(emit_from_worker_thread)
        await asyncio.sleep(0)
        state = issue_import_task.get_issue_import_progress_state(11)
        assert state is not None
        assert state.current_file_stage == "transferring"
        assert state.current_file_progress_current == 512
        assert state.current_file_progress_total == 1024
        assert state.current_file_progress_unit == "bytes"

        return SimpleNamespace(
            issue_id=11,
            library_file=SimpleNamespace(
                id=111,
                file_name="Aliens Epic Collection.cbz",
                file_path="/comics/Aliens Epic Collection.cbz",
                file_size=654321,
                file_format=SimpleNamespace(value="cbz"),
                match_confidence=SimpleNamespace(value="manual"),
            ),
        )

    monkeypatch.setattr(
        issue_import_task,
        "get_session_factory",
        lambda: _FakeSessionFactory(),
    )
    monkeypatch.setattr(
        issue_import_task,
        "prepare_manual_issue_import",
        AsyncMock(return_value=prepared),
    )
    monkeypatch.setattr(
        issue_import_task,
        "execute_manual_issue_import",
        fake_execute_manual_issue_import,
    )

    await issue_import_task._run_issue_import(
        11,
        {
            "file_path": "/tmp/imports/Aliens Epic Collection.cbz",
            "move_to_library": True,
            "allow_resource_safety_exception": False,
        },
    )

    state = issue_import_task.get_issue_import_progress_state(11)
    assert state is not None
    assert state.state == "completed"
    assert state.file_name == "Aliens Epic Collection.cbz"
