"""Direct branch coverage for utility API router orchestration."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.models.config import SystemConfig
from pullbox.models.library import LibraryFile, LibraryRoot
from pullbox.utilities import router as utilities_router
from pullbox.utilities.models import (
    ItemState,
    JobState,
    JobType,
    UtilityJob,
    UtilityJobItem,
    UtilityJobLog,
)
from pullbox.utilities.schemas import (
    ConvertPreviewRequest,
    DBCheckPreviewRequest,
    JobCreateRequest,
    LibraryPermissionsPreviewRequest,
    MassConvertPreviewRequest,
    MassRenamePreviewRequest,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


def _user() -> SimpleNamespace:
    return SimpleNamespace(username="admin")


def _job(
    job_id: str,
    *,
    state: str = JobState.QUEUED,
    job_type: str = JobType.FILE_CONVERT,
) -> UtilityJob:
    return UtilityJob(
        id=job_id,
        job_type=job_type,
        display_name=f"Job {job_id}",
        state=state,
        config="{}",
        total_items=2,
        completed_items=1,
        failed_items=0,
        skipped_items=0,
        warning_count=0,
        queue_position=None if state in {JobState.COMPLETED, JobState.FAILED} else 0,
        created_by="admin",
    )


class _Executor:
    def __init__(self, errors: list[str] | None = None) -> None:
        self.errors = errors or []

    def validate_config(self, _config: dict[str, object]) -> list[str]:
        return self.errors


class _Manager:
    def __init__(self, *, executor: _Executor | None = None) -> None:
        self.executor = executor
        self.created: list[dict[str, object]] = []
        self.paused: list[str] = []
        self.resumed: list[str] = []
        self.cancelled: list[tuple[str, bool]] = []
        self.rolled_back: list[tuple[str, str]] = []
        self.dispatch_next = AsyncMock()

    def get_executor(self, _job_type: str) -> _Executor | None:
        return self.executor

    async def create_job(
        self,
        *,
        session: AsyncSession,
        job_type: str,
        display_name: str,
        config: dict[str, object],
        created_by: str,
    ) -> UtilityJob:
        self.created.append(
            {
                "job_type": job_type,
                "display_name": display_name,
                "config": config,
                "created_by": created_by,
            }
        )
        job = _job("created-1", job_type=job_type)
        session.add(job)
        await session.flush()
        return job

    async def pause_job(self, _session: AsyncSession, job_id: str) -> None:
        if job_id == "bad":
            raise ValueError("cannot pause")
        self.paused.append(job_id)

    async def resume_job(self, _session: AsyncSession, job_id: str) -> None:
        if job_id == "bad":
            raise ValueError("cannot resume")
        self.resumed.append(job_id)

    async def cancel_job(self, _session: AsyncSession, job_id: str, *, rollback: bool) -> None:
        if job_id == "bad":
            raise ValueError("cannot cancel")
        self.cancelled.append((job_id, rollback))

    async def queue_rollback_job(
        self,
        _session: AsyncSession,
        job_id: str,
        *,
        created_by: str,
    ) -> None:
        if job_id == "bad":
            raise ValueError("cannot rollback")
        self.rolled_back.append((job_id, created_by))


def test_queue_manager_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(utilities_router, "_queue_manager", None)
    with pytest.raises(RuntimeError, match="not initialized"):
        utilities_router._get_manager()

    manager = _Manager()
    utilities_router.set_queue_manager(manager)  # type: ignore[arg-type]
    assert utilities_router._get_manager() is manager


@pytest.mark.asyncio
async def test_library_root_and_trash_helpers(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="No enabled library roots"):
        await utilities_router._load_enabled_library_roots(db_session)

    root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
    db_session.add(root)
    db_session.add(SystemConfig(key="utility_trash_retention_days", value="7", value_type="int"))
    await db_session.flush()

    roots = await utilities_router._load_enabled_library_roots(db_session)
    assert roots == [tmp_path]
    assert (
        await utilities_router._resolve_enabled_library_scan_root(
            db_session,
            str(tmp_path),
        )
        == tmp_path
    )
    with pytest.raises(ValidationError):
        await utilities_router._resolve_enabled_library_scan_root(
            db_session,
            str(tmp_path / "nope"),
        )

    monkeypatch.setattr(
        "pullbox.config.get_settings",
        lambda: SimpleNamespace(
            library_root=tmp_path,
            data_dir=tmp_path / "data",
        ),
    )
    trash_dir, retention_days = await utilities_router._resolve_utility_trash_context(db_session)
    assert trash_dir == tmp_path / ".trash"
    assert retention_days == 7


@pytest.mark.asyncio
async def test_create_job_validation_and_success(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="Invalid job_type"):
        await utilities_router.create_job(
            JobCreateRequest(job_type="bad", display_name="Bad"),
            _user(),
            db_session,
        )

    manager = _Manager(executor=_Executor(["missing target"]))
    utilities_router.set_queue_manager(manager)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="missing target"):
        await utilities_router.create_job(
            JobCreateRequest(job_type=JobType.FILE_CONVERT, display_name="Convert"),
            _user(),
            db_session,
        )

    manager = _Manager(executor=_Executor())
    utilities_router.set_queue_manager(manager)  # type: ignore[arg-type]
    monkeypatch.setattr(
        utilities_router,
        "ensure_utility_job_allowed_during_import",
        AsyncMock(),
    )
    monkeypatch.setattr(utilities_router, "_enforce_utility_trash_retention", AsyncMock())
    schedule = Mock()
    monkeypatch.setattr(utilities_router, "_schedule_dispatch", schedule)

    response = await utilities_router.create_job(
        JobCreateRequest(
            job_type=JobType.FILE_CONVERT,
            display_name="Convert",
            config={"target_format": "cbz"},
        ),
        _user(),
        db_session,
    )

    assert response.id == "created-1"
    assert manager.created[0]["created_by"] == "admin"
    schedule.assert_called_once_with(manager)


@pytest.mark.asyncio
async def test_empty_trash_uses_resolved_context(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(utilities_router, "ensure_no_active_import_file_mutation", AsyncMock())
    monkeypatch.setattr(
        utilities_router,
        "_resolve_utility_trash_context",
        AsyncMock(return_value=(tmp_path / ".trash", 30)),
    )
    monkeypatch.setattr(utilities_router, "empty_utility_trash", lambda _path: 3)

    response = await utilities_router.empty_trash(_user(), db_session)

    assert response == {"message": "Trash emptied.", "deleted_entries": 3}
    assert (tmp_path / ".trash").is_dir()


@pytest.mark.asyncio
async def test_job_crud_and_history_routes(db_session: AsyncSession) -> None:
    running = _job("running", state=JobState.RUNNING)
    completed = _job("completed", state=JobState.COMPLETED)
    failed = _job("failed", state=JobState.FAILED)
    db_session.add_all([running, completed, failed])
    db_session.add(UtilityJobItem(id="item-1", job_id="completed", item_index=0, operation="x"))
    db_session.add(UtilityJobLog(job_id="completed", message="done"))
    await db_session.flush()

    listed = await utilities_router.list_jobs(_user(), db_session, state=None, limit=10, offset=0)
    assert listed.total == 3
    filtered = await utilities_router.list_jobs(
        _user(),
        db_session,
        state="completed",
        limit=10,
        offset=0,
    )
    assert filtered.total == 1
    with pytest.raises(ValidationError, match="Invalid state"):
        await utilities_router.list_jobs(_user(), db_session, state="lost", limit=10, offset=0)

    detail = await utilities_router.get_job("completed", _user(), db_session)
    assert detail.id == "completed"
    with pytest.raises(NotFoundError):
        await utilities_router.get_job("missing", _user(), db_session)

    with pytest.raises(ValidationError, match="Cannot delete job"):
        await utilities_router.delete_job("running", _user(), db_session)
    with pytest.raises(NotFoundError):
        await utilities_router.delete_job("missing", _user(), db_session)

    await utilities_router.delete_job("failed", _user(), db_session)
    assert await db_session.get(UtilityJob, "failed") is None

    history = await utilities_router.clear_history(_user(), db_session)
    assert history == {"deleted": 1}
    assert await db_session.get(UtilityJob, "completed") is None


@pytest.mark.asyncio
async def test_job_items_and_queue_status(db_session: AsyncSession) -> None:
    db_session.add(_job("items", state=JobState.QUEUED))
    db_session.add(_job("done", state=JobState.COMPLETED))
    db_session.add_all(
        [
            UtilityJobItem(
                id="item-1",
                job_id="items",
                item_index=0,
                state=ItemState.COMPLETED,
                operation="convert",
            ),
            UtilityJobItem(
                id="item-2",
                job_id="items",
                item_index=1,
                state=ItemState.FAILED,
                operation="convert",
            ),
        ]
    )
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await utilities_router.get_job_items("missing", _user(), db_session)

    items = await utilities_router.get_job_items(
        "items",
        _user(),
        db_session,
        state="failed",
        limit=10,
        offset=0,
    )
    assert [item.id for item in items] == ["item-2"]

    status = await utilities_router.get_queue_status(_user(), db_session)
    assert status.queued == 1
    assert status.total_completed == 1


@pytest.mark.asyncio
async def test_job_control_routes_success_and_validation(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _Manager()
    utilities_router.set_queue_manager(manager)  # type: ignore[arg-type]
    monkeypatch.setattr(utilities_router, "_schedule_dispatch", Mock())

    assert await utilities_router.pause_job("job-1", _user(), db_session) == {
        "status": "pausing",
        "job_id": "job-1",
    }
    assert await utilities_router.resume_job("job-1", _user(), db_session) == {
        "status": "queued",
        "job_id": "job-1",
    }
    assert await utilities_router.cancel_job("job-1", _user(), db_session, rollback=False) == {
        "status": "cancelled",
        "job_id": "job-1",
    }
    assert await utilities_router.cancel_job("job-2", _user(), db_session, rollback=True) == {
        "status": "cancelling",
        "job_id": "job-2",
    }
    assert await utilities_router.rollback_job("job-1", _user(), db_session) == {
        "status": "queued",
        "job_id": "job-1",
    }

    for route in (
        utilities_router.pause_job,
        utilities_router.resume_job,
        utilities_router.rollback_job,
    ):
        with pytest.raises(ValidationError):
            await route("bad", _user(), db_session)
    with pytest.raises(ValidationError):
        await utilities_router.cancel_job("bad", _user(), db_session, rollback=False)


@pytest.mark.asyncio
async def test_preview_routes_delegate(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session.add(LibraryRoot(name="Root", path=str(tmp_path), enabled=True))
    await db_session.flush()

    monkeypatch.setattr(
        utilities_router,
        "build_convert_preview_response",
        lambda body, *, allowed_roots, excluded_paths: {
            "kind": "convert",
            "roots": allowed_roots,
            "excluded_paths": excluded_paths,
        },
    )
    monkeypatch.setattr(
        utilities_router,
        "build_mass_convert_preview",
        AsyncMock(return_value={"kind": "mass-convert"}),
    )
    monkeypatch.setattr(
        utilities_router,
        "build_library_permissions_preview",
        AsyncMock(return_value={"kind": "permissions"}),
    )
    monkeypatch.setattr(
        utilities_router,
        "build_mass_rename_preview",
        AsyncMock(return_value={"kind": "rename"}),
    )

    convert = await utilities_router.convert_preview(
        ConvertPreviewRequest(source_format="cbr", target_format="cbz"),
        _user(),
        db_session,
    )
    assert convert["kind"] == "convert"
    assert convert["roots"] == [tmp_path]
    assert convert["excluded_paths"] == frozenset()

    assert (
        await utilities_router.mass_convert_preview(
            MassConvertPreviewRequest(),
            _user(),
            db_session,
        )
    ) == {"kind": "mass-convert"}
    assert (
        await utilities_router.library_permissions_preview(
            LibraryPermissionsPreviewRequest(),
            _user(),
            db_session,
        )
    ) == {"kind": "permissions"}
    assert (
        await utilities_router.mass_rename_preview(
            MassRenamePreviewRequest(target="files"),
            _user(),
            db_session,
        )
    ) == {"kind": "rename"}


@pytest.mark.asyncio
async def test_db_check_preview_validation_and_findings(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="library_root is required"):
        await utilities_router.db_check_preview(
            DBCheckPreviewRequest(checks=["stale"]),
            _user(),
            db_session,
        )

    monkeypatch.setattr(
        "pullbox.utilities.executors.db_check_cleanup.DBCheckCleanupExecutor.validate_config",
        lambda _self, _config: ["bad checks"],
    )
    with pytest.raises(ValidationError, match="bad checks"):
        await utilities_router.db_check_preview(
            DBCheckPreviewRequest(checks=["unknown"]),
            _user(),
            db_session,
        )

    monkeypatch.setattr(
        "pullbox.utilities.executors.db_check_cleanup.DBCheckCleanupExecutor.validate_config",
        lambda _self, _config: [],
    )
    db_session.add(LibraryRoot(name="Root", path=str(tmp_path), enabled=True))
    db_session.add(
        LibraryFile(
            issue_id=None,
            library_root_id=1,
            file_path=str(tmp_path / "missing.cbz"),
            file_name="missing.cbz",
            file_size=1,
            file_format="cbz",
            file_modified_at=datetime.now(UTC),
            match_confidence="high",
        )
    )
    (tmp_path / "stale.cbz").write_text("comic")
    await db_session.flush()

    monkeypatch.setattr(
        "pullbox.utilities.executors.db_check_cleanup.detect_orphaned_records",
        lambda rows: rows,
    )
    monkeypatch.setattr(
        "pullbox.services.db_check_service.build_referential_findings",
        AsyncMock(
            return_value=[
                {
                    "finding_id": "ref-1",
                    "check_type": "referential",
                    "record_id": 1,
                    "record_type": "issue",
                    "file_path": None,
                    "description": "Broken issue link",
                    "suggested_action": "delete",
                    "allowed_actions": ["delete", "skip"],
                }
            ]
        ),
    )

    preview = await utilities_router.db_check_preview(
        DBCheckPreviewRequest(
            checks=["orphans", "stale", "referential", "reindex", "orphans"],
            library_root=str(tmp_path),
        ),
        _user(),
        db_session,
    )

    assert preview.checks == ["orphans", "stale", "referential", "reindex"]
    assert preview.finding_count >= 4
    assert {finding.check_type for finding in preview.findings} >= {
        "orphans",
        "stale",
        "referential",
        "reindex",
    }


@pytest.mark.asyncio
async def test_job_event_stream_yields_events_and_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Event:
        def format_sse(self) -> str:
            return "event: progress\ndata: {}\n\n"

    class Subscription:
        async def __aenter__(self) -> object:
            return SimpleNamespace(get=AsyncMock(side_effect=[TimeoutError(), Event(), None]))

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(utilities_router, "subscribe", lambda _topic: Subscription())

    response = await utilities_router.job_event_stream("job-1", _user())
    generator = response.body_iterator

    assert await anext(generator) == "event: heartbeat\ndata: {}\n\n"
    assert await anext(generator) == "event: progress\ndata: {}\n\n"
    with pytest.raises(StopAsyncIteration):
        await anext(generator)
