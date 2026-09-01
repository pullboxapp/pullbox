"""Tests for import-only staging and no-overwrite publication adapters."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pullbox.core.exceptions import ImportDestinationValidationError
from pullbox.services.import_file_registration_adapters import (
    build_import_library_file_adapters,
)


def _build_adapters(
    *,
    transfer: AsyncMock | object | None = None,
    materializer: AsyncMock | object | None = None,
):  # type: ignore[no-untyped-def]
    return build_import_library_file_adapters(
        session=object(),
        job=SimpleNamespace(id=42),
        convert_file_interruptible=AsyncMock(),
        embed_comicinfo_interruptible=AsyncMock(),
        transfer_artifact_interruptible=transfer or AsyncMock(),
        materialize_cbz_with_comicinfo_interruptible=materializer or AsyncMock(),
    )


@pytest.mark.asyncio
async def test_import_registration_adapters_forward_callbacks_and_record_timings(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source-bytes")
    transfer_target = tmp_path / "transferred.cbz"
    materialized_target = tmp_path / "materialized.cbz"
    converted = tmp_path / "converted.cbz"
    converted.write_bytes(b"converted")
    progress_callback = AsyncMock()
    transfer_progress_callback = AsyncMock()
    observed: dict[str, Any] = {}
    clock_values = iter([1.0, 1.25, 2.0, 2.5, 4.0, 4.75])

    async def fake_convert_file(
        session: object,
        job: object,
        convert_source: Path,
        target_format: str,
        *,
        destination: Path | None = None,
        progress_callback: object | None = None,
    ) -> Path:
        observed["convert"] = (
            session,
            job,
            convert_source,
            target_format,
            destination,
            progress_callback,
        )
        return converted

    async def fake_embed_comicinfo(
        session: object,
        job: object,
        artifact_path: Path,
        payload: dict[str, Any],
        *,
        progress_callback: object | None = None,
    ) -> bool:
        observed["embed"] = (session, job, artifact_path, payload, progress_callback)
        artifact_path.write_bytes(b"embedded")
        return True

    async def fake_transfer_artifact(
        session: object,
        job: object,
        artifact_source: Path,
        artifact_target: Path,
        transfer_method: str,
        *,
        transfer_progress_callback: object | None = None,
    ) -> Path:
        observed["transfer"] = (
            session,
            job,
            artifact_source,
            artifact_target,
            transfer_method,
            transfer_progress_callback,
        )
        artifact_target.write_bytes(b"transferred")
        return artifact_target

    async def fake_materialize(
        session: object,
        job: object,
        artifact_source: Path,
        artifact_target: Path,
        payload: dict[str, Any],
        *,
        transfer_method: str,
        temp_path: Path | None = None,
        progress_callback: object | None = None,
    ) -> bool:
        observed["materialize"] = (
            session,
            job,
            artifact_source,
            artifact_target,
            payload,
            transfer_method,
            temp_path,
            progress_callback,
        )
        artifact_target.write_bytes(b"materialized")
        return True

    session = object()
    job = SimpleNamespace(id=42)
    adapters = build_import_library_file_adapters(
        session=session,
        job=job,
        convert_file_interruptible=fake_convert_file,
        embed_comicinfo_interruptible=fake_embed_comicinfo,
        transfer_artifact_interruptible=fake_transfer_artifact,
        materialize_cbz_with_comicinfo_interruptible=fake_materialize,
        clock=lambda: next(clock_values),
    )

    assert (
        await adapters.converter(
            source,
            "cbz",
            destination=tmp_path,
            progress_callback=progress_callback,
        )
        == converted
    )
    assert (
        await adapters.artifact_transfer(
            source,
            transfer_target,
            "move",
            transfer_progress_callback=transfer_progress_callback,
        )
        == transfer_target
    )
    assert (
        await adapters.comicinfo_materializer(
            source,
            materialized_target,
            {"Series": "Progress"},
            transfer_method="move",
            progress_callback=progress_callback,
        )
        is True
    )
    assert (
        await adapters.comicinfo_embedder(
            materialized_target,
            {"Series": "Progress"},
            progress_callback=progress_callback,
        )
        is True
    )

    assert observed["convert"][-1] is progress_callback
    assert observed["transfer"][-1] is transfer_progress_callback
    materializer_temp_path = observed["materialize"][-2]
    assert isinstance(materializer_temp_path, Path)
    assert materializer_temp_path.name.endswith(".pullbox-write.tmp")
    assert observed["materialize"][-1] is progress_callback
    assert observed["embed"][-1] is progress_callback
    assert [timing["kind"] for timing in adapters.operation_timings] == [
        "transfer",
        "cbz_comicinfo_materialize",
        "comicinfo_rewrite",
    ]
    assert adapters.operation_timings[0]["target_size_bytes"] == len(b"transferred")
    assert adapters.operation_timings[1]["target_size_bytes"] == len(b"materialized")
    assert adapters.operation_timings[2]["changed"] is True
    assert adapters.operation_timings[0]["duration_ms"] == 250


def test_import_adapter_temp_paths_are_deterministic_destination_siblings(
    tmp_path: Path,
) -> None:
    adapters = _build_adapters()
    source = tmp_path / "incoming" / "Batman 001.cbz"
    target = tmp_path / "library" / "Batman (2026) #001.cbz"

    first = adapters.placement_temp_paths(source, target)
    second = adapters.placement_temp_paths(source, target)

    assert first == second
    assert len(first) == 2
    assert all(path.parent == target.parent for path in first)
    assert first[0].name.startswith(".pullbox-import-42-")
    assert first[0].suffix == target.suffix
    assert first[1] == first[0].with_name(f"{first[0].name}.pullbox-write.tmp")


@pytest.mark.asyncio
async def test_transfer_adapter_preserves_preexisting_deterministic_stage(
    tmp_path: Path,
) -> None:
    source = tmp_path / "incoming.cbz"
    source.write_bytes(b"source comic")
    target = tmp_path / "library" / "final.cbz"
    target.parent.mkdir()
    transfer = AsyncMock()
    adapters = _build_adapters(transfer=transfer)
    stage_path, _worker_temp_path = adapters.placement_temp_paths(source, target)
    stage_path.write_bytes(b"unknown prior stage")

    with pytest.raises(ImportDestinationValidationError) as exc_info:
        await adapters.artifact_transfer(source, target, "copy")

    assert exc_info.value.reason == "staging_path_exists"
    assert source.read_bytes() == b"source comic"
    assert stage_path.read_bytes() == b"unknown prior stage"
    assert not target.exists()
    transfer.assert_not_awaited()


@pytest.mark.asyncio
async def test_comicinfo_adapter_preserves_preexisting_deterministic_worker_temp(
    tmp_path: Path,
) -> None:
    source = tmp_path / "incoming.cbz"
    source.write_bytes(b"source comic")
    target = tmp_path / "library" / "final.cbz"
    target.parent.mkdir()
    materializer = AsyncMock()
    adapters = _build_adapters(materializer=materializer)
    stage_path, worker_temp_path = adapters.placement_temp_paths(source, target)
    worker_temp_path.write_bytes(b"unknown prior worker output")

    with pytest.raises(ImportDestinationValidationError) as exc_info:
        await adapters.comicinfo_materializer(
            source,
            target,
            {"Series": "Batman"},
            transfer_method="copy",
        )

    assert exc_info.value.reason == "staging_path_exists"
    assert source.read_bytes() == b"source comic"
    assert worker_temp_path.read_bytes() == b"unknown prior worker output"
    assert not stage_path.exists()
    assert not target.exists()
    materializer.assert_not_awaited()


@pytest.mark.asyncio
async def test_transfer_adapter_publishes_staged_file_without_overwrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "incoming.cbz"
    source.write_bytes(b"source comic")
    target = tmp_path / "library" / "final.cbz"
    target.parent.mkdir()
    seen_stage: Path | None = None

    async def transfer(
        _session: object,
        _job: object,
        source_path: Path,
        stage_path: Path,
        method: str,
        *,
        transfer_progress_callback=None,
    ) -> Path:
        nonlocal seen_stage
        _ = transfer_progress_callback
        assert source_path == source
        assert method == "copy"
        seen_stage = stage_path
        stage_path.write_bytes(source_path.read_bytes())
        return stage_path

    adapters = _build_adapters(transfer=transfer)

    result = await adapters.artifact_transfer(source, target, "copy")

    assert result == target
    assert target.read_bytes() == b"source comic"
    assert source.read_bytes() == b"source comic"
    assert seen_stage is not None
    assert seen_stage.parent == target.parent
    assert not os.path.lexists(seen_stage)


@pytest.mark.asyncio
async def test_transfer_adapter_preserves_source_and_intruder_on_post_plan_collision(
    tmp_path: Path,
) -> None:
    source = tmp_path / "incoming.cbz"
    source.write_bytes(b"source comic")
    target = tmp_path / "library" / "final.cbz"
    target.parent.mkdir()
    seen_stage: Path | None = None

    async def racing_move(
        _session: object,
        _job: object,
        source_path: Path,
        stage_path: Path,
        method: str,
        *,
        transfer_progress_callback=None,
    ) -> Path:
        nonlocal seen_stage
        _ = transfer_progress_callback
        assert method == "move"
        seen_stage = stage_path
        source_path.rename(stage_path)
        target.write_bytes(b"late intruder")
        return stage_path

    adapters = _build_adapters(transfer=racing_move)

    with pytest.raises(ImportDestinationValidationError, match="appeared during import"):
        await adapters.artifact_transfer(source, target, "move")

    assert source.read_bytes() == b"source comic"
    assert target.read_bytes() == b"late intruder"
    assert seen_stage is not None
    assert not os.path.lexists(seen_stage)


@pytest.mark.asyncio
async def test_transfer_adapter_preserves_reappeared_move_source_and_stage_for_review(
    tmp_path: Path,
) -> None:
    source = tmp_path / "incoming.cbz"
    source.write_bytes(b"original source")
    target = tmp_path / "library" / "final.cbz"
    target.parent.mkdir()
    seen_stage: Path | None = None

    async def racing_move(
        _session: object,
        _job: object,
        source_path: Path,
        stage_path: Path,
        method: str,
        *,
        transfer_progress_callback=None,
    ) -> Path:
        nonlocal seen_stage
        _ = transfer_progress_callback
        assert method == "move"
        seen_stage = stage_path
        source_path.rename(stage_path)
        source_path.write_bytes(b"reappeared source")
        target.write_bytes(b"late intruder")
        return stage_path

    adapters = _build_adapters(transfer=racing_move)

    with pytest.raises(ImportDestinationValidationError, match="appeared during import"):
        await adapters.artifact_transfer(source, target, "move")

    assert source.read_bytes() == b"reappeared source"
    assert target.read_bytes() == b"late intruder"
    assert seen_stage is not None
    assert seen_stage.read_bytes() == b"original source"


@pytest.mark.asyncio
async def test_combined_comicinfo_adapter_preserves_source_on_post_plan_collision(
    tmp_path: Path,
) -> None:
    source = tmp_path / "incoming.cbz"
    source.write_bytes(b"source comic")
    target = tmp_path / "library" / "final.cbz"
    target.parent.mkdir()
    seen_stage: Path | None = None

    async def racing_materializer(
        _session: object,
        _job: object,
        source_path: Path,
        stage_path: Path,
        payload: dict[str, object],
        *,
        transfer_method: str,
        temp_path: Path | None = None,
        progress_callback=None,
    ) -> bool:
        nonlocal seen_stage
        _ = payload, temp_path, progress_callback
        assert transfer_method == "move"
        seen_stage = stage_path
        stage_path.write_bytes(b"comic with ComicInfo")
        source_path.unlink()
        target.write_bytes(b"late intruder")
        return True

    adapters = _build_adapters(materializer=racing_materializer)

    with pytest.raises(ImportDestinationValidationError, match="appeared during import"):
        await adapters.comicinfo_materializer(
            source,
            target,
            {"Series": "Batman"},
            transfer_method="move",
        )

    assert source.read_bytes() == b"comic with ComicInfo"
    assert target.read_bytes() == b"late intruder"
    assert seen_stage is not None
    assert not os.path.lexists(seen_stage)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["hardlink", "symlink"])
async def test_transfer_adapter_publishes_link_staging_safely(
    tmp_path: Path,
    method: str,
) -> None:
    source = tmp_path / "incoming.cbz"
    source.write_bytes(b"source comic")
    target = tmp_path / "library" / f"{method}.cbz"
    target.parent.mkdir()

    async def link_transfer(
        _session: object,
        _job: object,
        source_path: Path,
        stage_path: Path,
        transfer_method: str,
        *,
        transfer_progress_callback=None,
    ) -> Path:
        _ = transfer_progress_callback
        if transfer_method == "hardlink":
            stage_path.hardlink_to(source_path)
        else:
            stage_path.symlink_to(source_path)
        return stage_path

    adapters = _build_adapters(transfer=link_transfer)

    result = await adapters.artifact_transfer(source, target, method)

    assert result == target
    assert target.read_bytes() == b"source comic"
    assert source.exists()
    if method == "hardlink":
        assert target.samefile(source)
    else:
        assert target.is_symlink()
