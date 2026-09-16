"""Unit coverage for library ComicInfo preparation helpers."""

from __future__ import annotations

import threading
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest

from pullbox.core import library_comicinfo
from pullbox.core.exceptions import ConfigurationError
from pullbox.core.library_comicinfo import (
    apply_comicinfo_to_imported_artifact,
    build_comicinfo_payload_for_issue,
    cleanup_prepared_paths,
    format_comicinfo_issue_number,
    prepare_source_artifact,
)
from pullbox.models.issue import Issue
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


def test_format_comicinfo_issue_number_handles_empty_integer_and_fractional() -> None:
    assert format_comicinfo_issue_number(None) is None
    assert format_comicinfo_issue_number(12.0) == "12"
    assert format_comicinfo_issue_number(12.25) == "12.25"
    assert format_comicinfo_issue_number(1_000_000.0) == "1000000"


@pytest.mark.asyncio
async def test_prepare_source_artifact_leaves_existing_cbz_in_place(tmp_path: Path) -> None:
    source = tmp_path / "King Dracula 004.cbz"
    source.write_bytes(b"cbz")

    prepared_path, cleanup_paths = await prepare_source_artifact(
        source,
        normalize_to_cbz=True,
        update_embedded_comicinfo_from_match=True,
    )

    assert prepared_path == source
    assert cleanup_paths == []


@pytest.mark.asyncio
async def test_prepare_source_artifact_skips_conversion_when_not_needed(tmp_path: Path) -> None:
    source = tmp_path / "King Dracula 004.cbr"
    source.write_bytes(b"cbr")

    prepared_path, cleanup_paths = await prepare_source_artifact(
        source,
        normalize_to_cbz=False,
        update_embedded_comicinfo_from_match=False,
    )

    assert prepared_path == source
    assert cleanup_paths == []


@pytest.mark.asyncio
async def test_prepare_source_artifact_converts_with_resource_exception(
    tmp_path: Path,
) -> None:
    source = tmp_path / "The Joker - Endgame.pdf"
    source.write_bytes(b"pdf")
    calls: list[dict[str, Any]] = []

    async def fake_converter(
        source_path: Path,
        target_format: str,
        destination: Path,
        **kwargs: Any,
    ) -> Path:
        calls.append(
            {
                "source_path": source_path,
                "target_format": target_format,
                "destination": destination,
                "kwargs": kwargs,
            }
        )
        converted = destination / "The Joker - Endgame.cbz"
        converted.write_bytes(b"converted")
        return converted

    prepared_path, cleanup_paths = await prepare_source_artifact(
        source,
        normalize_to_cbz=True,
        update_embedded_comicinfo_from_match=False,
        converter=fake_converter,
        allow_resource_safety_exception=True,
    )

    assert prepared_path.name == "The Joker - Endgame.cbz"
    assert prepared_path.exists()
    assert len(cleanup_paths) == 1
    assert cleanup_paths[0].is_dir()
    assert calls == [
        {
            "source_path": source,
            "target_format": "cbz",
            "destination": cleanup_paths[0],
            "kwargs": {"allow_resource_safety_exception": True},
        }
    ]

    cleanup_prepared_paths(cleanup_paths)
    assert not cleanup_paths[0].exists()


@pytest.mark.asyncio
async def test_prepare_source_artifact_cleans_temp_dir_when_converter_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "broken.cbr"
    source.write_bytes(b"cbr")
    temp_dir = tmp_path / "staged"

    monkeypatch.setattr(library_comicinfo.tempfile, "mkdtemp", lambda prefix: str(temp_dir))

    async def failing_converter(*_args: Any, **_kwargs: Any) -> Path:
        raise RuntimeError("conversion failed")

    with pytest.raises(RuntimeError, match="conversion failed"):
        await prepare_source_artifact(
            source,
            normalize_to_cbz=True,
            update_embedded_comicinfo_from_match=False,
            converter=failing_converter,
        )

    assert not temp_dir.exists()


@pytest.mark.asyncio
async def test_apply_comicinfo_requires_cbz_artifact(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="requires a CBZ artifact"):
        await apply_comicinfo_to_imported_artifact(tmp_path / "issue.cbr", {})


@pytest.mark.asyncio
async def test_apply_comicinfo_delegates_to_embedder_without_blocking_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "issue.cbz"
    artifact.write_bytes(b"cbz")
    progress_calls: list[tuple[str, int, int, str]] = []
    embed_calls: list[tuple[Path, dict[str, Any]]] = []
    event_loop_thread_id = threading.get_ident()
    embed_thread_ids: list[int] = []

    def fake_embed(
        artifact_path: Path,
        comicinfo_payload: dict[str, Any],
        *,
        progress_callback: Any = None,
    ) -> None:
        embed_thread_ids.append(threading.get_ident())
        embed_calls.append((artifact_path, comicinfo_payload))
        assert progress_callback is not None
        progress_callback("comicinfo", 1, 1, "done")

    monkeypatch.setattr(library_comicinfo, "embed_comicinfo_in_cbz", fake_embed)

    await apply_comicinfo_to_imported_artifact(
        artifact,
        {"Series": "King Dracula"},
        progress_callback=lambda *args: progress_calls.append(args),
    )

    assert embed_calls == [(artifact, {"Series": "King Dracula"})]
    assert embed_thread_ids != [event_loop_thread_id]
    assert progress_calls == [("comicinfo", 1, 1, "done")]


def test_cleanup_prepared_paths_removes_directories_and_files(tmp_path: Path) -> None:
    staged_dir = tmp_path / "staged-dir"
    staged_dir.mkdir()
    staged_file = tmp_path / "staged-file.tmp"
    staged_file.write_bytes(b"temporary")

    cleanup_prepared_paths([staged_dir, staged_file, tmp_path / "missing.tmp"])

    assert not staged_dir.exists()
    assert not staged_file.exists()


@pytest.mark.asyncio
async def test_build_comicinfo_payload_for_issue_uses_series_issue_and_page_metadata(
    db_session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher = Publisher(name="Dynamite Entertainment", comicvine_id=9001)
    db_session.add(publisher)
    await db_session.flush()
    series = Series(
        title="King Dracula",
        sort_title="king dracula",
        year_start=2025,
        comicvine_id=165993,
        comicvine_url="https://comicvine.gamespot.com/king-dracula/4050-165993/",
        issue_count=4,
        publisher_id=publisher.id,
    )
    db_session.add(series)
    await db_session.flush()
    issue = Issue(
        series_id=series.id,
        issue_number=4.0,
        comicvine_id=1122334,
        title="King Dracula #4",
        description="The crown comes due.",
        page_count=28,
        comicvine_url="https://comicvine.gamespot.com/king-dracula-4/4000-1122334/",
        release_date=date(2026, 6, 17),
    )
    db_session.add(issue)
    await db_session.flush()
    source_path = tmp_path / "King Dracula 004.cbz"
    source_path.write_bytes(b"cbz")
    event_loop_thread_id = threading.get_ident()
    inspection_thread_ids: list[int] = []

    def inspect_page_count(_path: Path) -> int:
        inspection_thread_ids.append(threading.get_ident())
        return 31

    monkeypatch.setattr(library_comicinfo, "inspect_archive_page_count", inspect_page_count)

    payload = await build_comicinfo_payload_for_issue(
        db_session,
        issue,
        source_path=source_path,
    )

    assert payload["Series"] == "King Dracula"
    assert payload["Number"] == "4"
    assert payload["Title"] == "King Dracula #4"
    assert payload["Summary"] == "The crown comes due."
    assert payload["Publisher"] == "Dynamite Entertainment"
    assert payload["Year"] == 2026
    assert payload["Month"] == 6
    assert payload["Day"] == 17
    assert payload["PageCount"] == 31
    assert payload["Count"] == 4
    assert payload["Volume"] == 2025
    assert payload["Web"] == "https://comicvine.gamespot.com/king-dracula-4/4000-1122334/"
    assert payload["Notes"] == "[cv_vol_id:165993] [cv_issue_id:1122334]"
    assert inspection_thread_ids != [event_loop_thread_id]


@pytest.mark.asyncio
async def test_build_comicinfo_payload_preserves_volume_note_for_provisional_issue(
    db_session: AsyncSession,
) -> None:
    series = Series(
        title="DC Connect",
        sort_title="dc connect",
        year_start=2020,
        comicvine_id=131624,
        issue_count=72,
    )
    db_session.add(series)
    await db_session.flush()
    issue = Issue(
        series_id=series.id,
        issue_number=73.0,
        title="DC Connect #73",
        comicvine_id=None,
    )
    db_session.add(issue)
    await db_session.flush()

    payload = await build_comicinfo_payload_for_issue(db_session, issue)

    assert payload["Notes"] == "[cv_vol_id:131624]"
    assert payload["Web"] is None
