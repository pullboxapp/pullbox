"""Focused tests for system backup route orchestration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pullbox.core.exceptions import NotFoundError, ValidationError
from pullbox.services.backup_service import BackupInfo


class _StubRuntimeService:
    def __init__(self, *, restore_result: bool = True) -> None:
        self.restore_result = restore_result
        self.create_calls: list[str] = []
        self.restore_calls: list[str] = []

    async def create_backup(self, *, backup_type: str) -> BackupInfo:
        self.create_calls.append(backup_type)
        return BackupInfo(
            filename="pullbox_backup_20260502_120000.zip",
            created_at="2026-05-02T12:00:00+00:00",
            size_bytes=2048,
            pullbox_version="1.2.3",
            db_size_bytes=1024,
            backup_type=backup_type,
        )

    async def restore_backup(self, filename: str) -> bool:
        self.restore_calls.append(filename)
        return self.restore_result


class TestSystemBackupRoutes:
    @pytest.mark.parametrize("operation", ["create", "restore"])
    async def test_manual_maintenance_releases_request_transaction(self, monkeypatch, operation):
        from pullbox.api.v1 import system

        session = AsyncMock()
        runtime = _StubRuntimeService()
        original_create = runtime.create_backup

        async def check_released(*args, **kwargs):
            session.commit.assert_awaited_once()
            session.close.assert_awaited_once()
            return await original_create(backup_type="manual") if operation == "create" else False

        runtime.create_backup = check_released
        runtime.restore_backup = check_released
        monkeypatch.setattr(system, "_get_backup_runtime_service", AsyncMock(return_value=runtime))
        if operation == "restore":
            with pytest.raises(NotFoundError):
                await system.restore_backup("backup.zip", object(), session)
        else:
            response = await system.create_backup(object(), session)
            assert response.backup.backup_type == "manual"

    @pytest.mark.asyncio
    async def test_get_backup_runtime_service_uses_runtime_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.api.v1 import system

        monkeypatch.setattr(
            system,
            "get_settings",
            lambda: SimpleNamespace(backup_dir=Path("/tmp/backups")),
        )
        monkeypatch.setattr(system, "_resolve_db_path", lambda: Path("/tmp/pullbox.db"))

        svc = await system._get_backup_runtime_service(object())

        assert svc.service._backup_dir == Path("/tmp/backups")
        assert svc.service._db_path == Path("/tmp/pullbox.db")

    @pytest.mark.asyncio
    async def test_create_backup_route_uses_runtime_service_and_preserves_shape(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.api.v1 import system

        runtime = _StubRuntimeService()

        async def _fake_get_runtime_service(_session: object) -> _StubRuntimeService:
            return runtime

        monkeypatch.setattr(system, "_get_backup_runtime_service", _fake_get_runtime_service)

        response = await system.create_backup(object(), AsyncMock())

        assert runtime.create_calls == ["manual"]
        assert response.message == "Backup created: pullbox_backup_20260502_120000.zip"
        assert response.backup.filename == "pullbox_backup_20260502_120000.zip"
        assert response.backup.backup_type == "manual"

    @pytest.mark.asyncio
    async def test_restore_backup_route_uses_runtime_service_and_preserves_shape(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.api.v1 import system

        runtime = _StubRuntimeService()

        async def _fake_get_runtime_service(_session: object) -> _StubRuntimeService:
            return runtime

        monkeypatch.setattr(system, "_get_backup_runtime_service", _fake_get_runtime_service)
        marker_calls: list[str] = []
        monkeypatch.setattr(
            system,
            "mark_restore_recovery_pending",
            lambda filename: marker_calls.append(filename) or {"status": "pending"},
        )

        response = await system.restore_backup(
            "pullbox_backup_20260502_120000.zip",
            object(),
            AsyncMock(),
        )

        assert runtime.restore_calls == ["pullbox_backup_20260502_120000.zip"]
        assert marker_calls == ["pullbox_backup_20260502_120000.zip"]
        assert response.restart_required is True
        assert response.restore_recovery is not None
        assert response.restore_recovery["status"] == "pending"
        assert "Restart the application" in response.message

    @pytest.mark.asyncio
    async def test_restore_backup_route_raises_not_found_when_service_misses(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.api.v1 import system

        runtime = _StubRuntimeService(restore_result=False)

        async def _fake_get_runtime_service(_session: object) -> _StubRuntimeService:
            return runtime

        monkeypatch.setattr(system, "_get_backup_runtime_service", _fake_get_runtime_service)

        with pytest.raises(NotFoundError):
            await system.restore_backup("pullbox_backup_20260502_120000.zip", object(), AsyncMock())

    @pytest.mark.asyncio
    async def test_restore_backup_route_rejects_unsafe_filename_before_service_lookup(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.api.v1 import system

        async def _unexpected_get_runtime_service(_session: object) -> _StubRuntimeService:
            raise AssertionError("runtime service should not be resolved for unsafe filenames")

        monkeypatch.setattr(system, "_get_backup_runtime_service", _unexpected_get_runtime_service)

        with pytest.raises(ValidationError):
            await system.restore_backup("../evil.zip", object(), object())

    @pytest.mark.asyncio
    async def test_restore_recovery_status_route_reads_status_service(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from pullbox.api.v1 import system

        monkeypatch.setattr(
            system,
            "get_restore_recovery_status",
            lambda: {"status": "completed", "message": "done"},
        )

        response = await system.restore_recovery_status(object(), object())

        assert response == {"status": "completed", "message": "done"}
