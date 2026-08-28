"""Download post-processing source validation helper tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pullbox.core.file_safety import FileSafetyError


@pytest.mark.asyncio
async def test_resolve_and_validate_source_rejects_missing_client_path() -> None:
    """A completed download with no resolved path should fail before filesystem work."""
    from pullbox.tasks.download_post_processing_source_validation import (
        resolve_and_validate_source,
    )

    with pytest.raises(FileNotFoundError, match="did not report a file path"):
        await resolve_and_validate_source(
            session=object(),
            download=SimpleNamespace(id=7, downloaded_path=None),
            trace=SimpleNamespace(),
            runtime=SimpleNamespace(enter_phase=lambda phase: None),
            log=SimpleNamespace(debug=lambda *args, **kwargs: None),
            resolve_local_path=AsyncMock(return_value=None),
            probe_source=AsyncMock(),
            build_integrity_exception=lambda comic_file, errors: RuntimeError(errors),
        )


@pytest.mark.asyncio
async def test_resolve_and_validate_source_refuses_filesystem_root_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed path mapping must never send the container root to file safety."""
    from pullbox.tasks import download_post_processing_source_validation as source_validation

    monkeypatch.setattr(
        source_validation,
        "get_allowed_extensions",
        AsyncMock(return_value={".cbz"}),
    )
    probe = SimpleNamespace(
        source_seen=True,
        probe_root=Path("/"),
        comic_file=Path("/unexpected.cbz"),
        attempts=1,
    )

    with pytest.raises(RuntimeError, match="filesystem root"):
        await source_validation.resolve_and_validate_source(
            session=object(),
            download=SimpleNamespace(id=7, downloaded_path="/downloads\\Release\\file.cbr"),
            trace=SimpleNamespace(),
            runtime=SimpleNamespace(enter_phase=lambda phase: None),
            log=SimpleNamespace(
                debug=lambda *args, **kwargs: None,
                error=lambda *args, **kwargs: None,
            ),
            resolve_local_path=AsyncMock(return_value="/downloads\\Release\\file.cbr"),
            probe_source=AsyncMock(return_value=probe),
            build_integrity_exception=lambda comic_file, errors: RuntimeError(errors),
        )


@pytest.mark.asyncio
async def test_allow_once_bypasses_only_resource_limit_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approved resource exception keeps all non-resource safety checks intact."""
    from pullbox.tasks import download_post_processing_source_validation as source_validation

    comic_file = tmp_path / "oversized.cbz"
    comic_file.write_bytes(b"fixture")
    probe = SimpleNamespace(
        source_seen=True,
        probe_root=comic_file,
        comic_file=comic_file,
        attempts=1,
    )
    monkeypatch.setattr(
        source_validation,
        "get_allowed_extensions",
        AsyncMock(return_value={".cbz"}),
    )
    monkeypatch.setattr(
        source_validation,
        "is_dangerous_file_blocking_enabled",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        source_validation,
        "get_archive_size_limit_bytes",
        AsyncMock(return_value=2_000 * 1024 * 1024),
    )
    monkeypatch.setattr(
        source_validation,
        "check_file_integrity",
        AsyncMock(
            return_value=SimpleNamespace(
                status="ok",
                page_count=1,
                file_hash="fixture",
                errors=[],
                warnings=[],
            )
        ),
    )

    def oversized(*_args: object, **_kwargs: object) -> None:
        raise FileSafetyError(
            "Archive decompressed size (4,248,234,210 bytes) exceeds limit (2,097,152,000 bytes)"
        )

    monkeypatch.setattr(source_validation, "run_safety_checks", oversized)
    log = SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )

    result = await source_validation.resolve_and_validate_source(
        session=object(),
        download=SimpleNamespace(id=-1, downloaded_path=str(comic_file)),
        trace=SimpleNamespace(),
        runtime=SimpleNamespace(enter_phase=lambda phase: None),
        log=log,
        resolve_local_path=AsyncMock(return_value=str(comic_file)),
        probe_source=AsyncMock(return_value=probe),
        build_integrity_exception=lambda path, errors: RuntimeError(errors),
        allow_resource_safety_exception=True,
    )

    assert result.comic_file == comic_file


@pytest.mark.asyncio
async def test_allow_once_does_not_bypass_hard_safety_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Traversal and dangerous-content failures remain hard stops."""
    from pullbox.tasks import download_post_processing_source_validation as source_validation

    comic_file = tmp_path / "unsafe.cbz"
    comic_file.write_bytes(b"fixture")
    probe = SimpleNamespace(
        source_seen=True,
        probe_root=comic_file,
        comic_file=comic_file,
        attempts=1,
    )
    monkeypatch.setattr(
        source_validation,
        "get_allowed_extensions",
        AsyncMock(return_value={".cbz"}),
    )
    monkeypatch.setattr(
        source_validation,
        "is_dangerous_file_blocking_enabled",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        source_validation,
        "get_archive_size_limit_bytes",
        AsyncMock(return_value=2_000 * 1024 * 1024),
    )

    def unsafe(*_args: object, **_kwargs: object) -> None:
        raise FileSafetyError(
            "Archive contains path traversal entries - entire release rejected",
            details=["../escape.jpg"],
        )

    monkeypatch.setattr(source_validation, "run_safety_checks", unsafe)
    log = SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )

    with pytest.raises(RuntimeError, match="path traversal"):
        await source_validation.resolve_and_validate_source(
            session=object(),
            download=SimpleNamespace(id=-1, downloaded_path=str(comic_file)),
            trace=SimpleNamespace(),
            runtime=SimpleNamespace(enter_phase=lambda phase: None),
            log=log,
            resolve_local_path=AsyncMock(return_value=str(comic_file)),
            probe_source=AsyncMock(return_value=probe),
            build_integrity_exception=lambda path, errors: RuntimeError(errors),
            allow_resource_safety_exception=True,
        )
