"""Tests for ImportService Step 4 file-processing adapter wiring."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from pullbox.models.library import LibraryFileStorageMode, MatchConfidence
from pullbox.services.import_series_file_processor import process_series_files_for_import

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@pytest.mark.asyncio
async def test_process_series_files_for_import_wires_job_scoped_callbacks() -> None:
    session = object()
    job = SimpleNamespace(id=42)
    item = SimpleNamespace(id=7)
    issue = SimpleNamespace(id=99)
    source_path = Path("/imports/source.cbz")
    payload = {"Series": "Adapter Test"}
    registered = object()
    build_cached_payload = AsyncMock(return_value=payload)
    register_library_file = AsyncMock(return_value=registered)

    async def fake_core_processor(
        session_arg: object,
        job_arg: object,
        item_arg: object,
        *,
        build_comicinfo_payload: Callable[..., Awaitable[dict[str, object]]],
        register_file: Callable[..., Awaitable[object]],
        **kwargs: object,
    ) -> tuple[int, int]:
        assert session_arg is session
        assert job_arg is job
        assert item_arg is item
        assert kwargs["duplicate_mode"] is True
        assert kwargs["series_id_override"] == 123

        callback_payload = await build_comicinfo_payload(
            session_arg,
            issue,
            source_path=source_path,
        )
        callback_registered = await register_file(
            session_arg,
            source_path,
            issue,
            MatchConfidence.HIGH,
            move_to_library=True,
            storage_mode=LibraryFileStorageMode.MANAGED,
            expected_source_signature=None,
            library_root_id=5,
            transfer_method="copy",
            normalize_to_cbz=True,
            update_embedded_comicinfo_from_match=True,
            comicinfo_payload=callback_payload,
        )

        assert callback_payload == payload
        assert callback_registered is registered
        return 1, 0

    result = await process_series_files_for_import(
        session,  # type: ignore[arg-type]
        job,  # type: ignore[arg-type]
        item,  # type: ignore[arg-type]
        duplicate_mode=True,
        series_id_override=123,
        load_media_settings=AsyncMock(),
        load_trash_dir=AsyncMock(),
        load_ingest_policy=AsyncMock(),
        load_permission_policy=AsyncMock(),
        raise_if_cancelled=AsyncMock(),
        prepare_file=AsyncMock(),
        build_cached_comicinfo_payload=build_cached_payload,
        apply_comicinfo=AsyncMock(),
        cleanup_prepared_file=AsyncMock(),
        record_action=AsyncMock(),
        log_event=AsyncMock(),
        register_import_library_file=register_library_file,
        move_to_trash=AsyncMock(),
        core_processor=fake_core_processor,
    )

    assert result == (1, 0)
    build_cached_payload.assert_awaited_once_with(
        session,
        job,
        issue,
        source_path=source_path,
        defer_issue_enrichment=False,
    )
    register_library_file.assert_awaited_once()
    assert register_library_file.await_args.args[:5] == (
        session,
        job,
        source_path,
        issue,
        MatchConfidence.HIGH,
    )
