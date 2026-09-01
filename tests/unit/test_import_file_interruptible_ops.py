"""Tests for interruptible import file-operation adapters."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from pullbox.services.import_file_interruptible_ops import (
    convert_import_file_interruptible,
    materialize_import_cbz_with_comicinfo_interruptible,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_convert_import_file_interruptible_wires_cancellation_and_progress(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.cbr"
    destination = tmp_path / "converted"
    target_path = destination / "source.cbz"
    progress_callback = AsyncMock()
    raise_if_cancelled = AsyncMock()
    seen: dict[str, object] = {}

    async def fake_converter(
        source_arg: Path,
        target_format: str,
        *,
        destination: Path | None = None,
        cancellation_check=None,
        progress_callback=None,
        allow_resource_safety_exception: bool = False,
    ) -> Path:
        seen.update(
            {
                "source": source_arg,
                "target_format": target_format,
                "destination": destination,
                "progress_callback": progress_callback,
                "allow_resource_safety_exception": allow_resource_safety_exception,
            }
        )
        assert cancellation_check is not None
        await cancellation_check()
        return target_path

    result = await convert_import_file_interruptible(
        db_session,
        SimpleNamespace(id=42),
        source_path,
        "cbz",
        destination=destination,
        progress_callback=progress_callback,
        allow_resource_safety_exception=True,
        raise_if_cancelled_immediately=raise_if_cancelled,
        converter=fake_converter,
    )

    assert result == target_path
    assert seen == {
        "source": source_path,
        "target_format": "cbz",
        "destination": destination,
        "progress_callback": progress_callback,
        "allow_resource_safety_exception": True,
    }
    raise_if_cancelled.assert_awaited_once_with(db_session, 42)


@pytest.mark.asyncio
async def test_materialize_import_cbz_forwards_deterministic_temp_path(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.cbz"
    target_path = tmp_path / "staged.cbz"
    temp_path = tmp_path / "staged.cbz.pullbox-write.tmp"
    raise_if_cancelled = AsyncMock()
    seen: dict[str, object] = {}

    async def fake_materializer(
        source_arg: Path,
        target_arg: Path,
        payload: dict[str, object],
        *,
        transfer_method: str,
        temp_path: Path | None = None,
        cancellation_check=None,
        progress_callback=None,
    ) -> bool:
        seen.update(
            {
                "source": source_arg,
                "target": target_arg,
                "payload": payload,
                "transfer_method": transfer_method,
                "temp_path": temp_path,
                "progress_callback": progress_callback,
            }
        )
        assert cancellation_check is not None
        await cancellation_check()
        return True

    result = await materialize_import_cbz_with_comicinfo_interruptible(
        db_session,
        SimpleNamespace(id=42),
        source_path,
        target_path,
        {"Series": "Batman"},
        transfer_method="copy",
        temp_path=temp_path,
        raise_if_cancelled_immediately=raise_if_cancelled,
        materializer=fake_materializer,
    )

    assert result is True
    assert seen["temp_path"] == temp_path
    raise_if_cancelled.assert_awaited_once_with(db_session, 42)
