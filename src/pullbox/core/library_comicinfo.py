"""ComicInfo preparation helpers for library file registration."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from pullbox.core.archive import inspect_archive_page_count
from pullbox.core.exceptions import ConfigurationError
from pullbox.core.issue_numbers import format_issue_number
from pullbox.models.publisher import Publisher
from pullbox.models.series import Series
from pullbox.utilities.comicinfo import embed_comicinfo_in_cbz
from pullbox.utilities.comicinfo_creators import load_comicinfo_creator_fields
from pullbox.utilities.executors.file_converter import convert_file

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.issue import Issue


async def prepare_source_artifact(
    source_path: Path,
    *,
    normalize_to_cbz: bool,
    update_embedded_comicinfo_from_match: bool,
    converter: Callable[..., Any] = convert_file,
    allow_resource_safety_exception: bool = False,
) -> tuple[Path, list[Path]]:
    """Normalize a source archive into a staged CBZ artifact when required."""
    if source_path.suffix.lower() == ".cbz":
        return source_path, []

    if not normalize_to_cbz and not update_embedded_comicinfo_from_match:
        return source_path, []

    temp_dir = Path(tempfile.mkdtemp(prefix="pullbox-ingest-convert-"))
    try:
        if allow_resource_safety_exception:
            converted_path = await converter(
                source_path,
                "cbz",
                temp_dir,
                allow_resource_safety_exception=True,
            )
        else:
            converted_path = await converter(source_path, "cbz", temp_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return converted_path, [temp_dir]


async def build_comicinfo_payload_for_issue(
    session: AsyncSession,
    issue: Issue,
    *,
    source_path: Path | None = None,
) -> dict[str, Any]:
    """Build authoritative ComicInfo fields for the resolved issue."""
    series = issue.__dict__.get("series")
    if series is None:
        series = await _load_series_with_publisher(session, series_id=issue.series_id)
    if series is None:
        raise ConfigurationError(f"Could not load series for issue {issue.id}")

    publisher_name: str | None = None
    publisher = series.__dict__.get("publisher")
    if publisher is not None:
        publisher_name = publisher.name
    elif series.publisher_id is not None:
        publisher = await session.get(Publisher, series.publisher_id)
        if publisher is not None:
            publisher_name = publisher.name

    release_date = issue.release_date
    inspected_page_count = (
        await asyncio.to_thread(inspect_archive_page_count, source_path)
        if source_path is not None
        else None
    )
    notes: str | None = None
    if series.comicvine_id and issue.comicvine_id:
        notes = f"[cv_vol_id:{series.comicvine_id}] [cv_issue_id:{issue.comicvine_id}]"
    elif series.comicvine_id:
        notes = f"[cv_vol_id:{series.comicvine_id}]"

    payload: dict[str, Any] = {
        "Series": series.title,
        "Number": issue.effective_issue_number_text,
        "Title": issue.title,
        "Summary": issue.description,
        "Publisher": publisher_name,
        "Year": release_date.year if release_date is not None else None,
        "Month": release_date.month if release_date is not None else None,
        "Day": release_date.day if release_date is not None else None,
        "PageCount": (
            inspected_page_count
            if inspected_page_count and inspected_page_count > 0
            else issue.page_count
            if issue.page_count and issue.page_count > 0
            else None
        ),
        "Count": series.issue_count if series.issue_count and series.issue_count > 0 else None,
        "Volume": series.year_start,
        "Web": issue.comicvine_url,
        "Notes": notes,
    }
    payload.update(await load_comicinfo_creator_fields(session, issue.id))
    return payload


async def apply_comicinfo_to_imported_artifact(
    artifact_path: Path,
    comicinfo_payload: dict[str, Any],
    *,
    progress_callback: Callable[[str, int, int, str], Any] | None = None,
) -> None:
    """Write authoritative ComicInfo without blocking request handling."""
    if artifact_path.suffix.lower() != ".cbz":
        raise ConfigurationError(
            "Updating embedded ComicInfo.xml from matched issue requires a CBZ artifact."
        )
    await asyncio.to_thread(
        embed_comicinfo_in_cbz,
        artifact_path,
        comicinfo_payload,
        progress_callback=progress_callback,
    )


def format_comicinfo_issue_number(issue_number: float | None) -> str | None:
    if issue_number is None:
        return None
    return format_issue_number(issue_number)


def cleanup_prepared_paths(paths: list[Path]) -> None:
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)


async def _load_series_with_publisher(
    session: AsyncSession,
    *,
    series_id: int,
) -> Series | None:
    """Reload a Series with eager publisher data, bypassing stale identity-map state."""
    statement = (
        select(Series)
        .options(joinedload(Series.publisher))
        .where(Series.id == series_id)
        .execution_options(populate_existing=True)
    )
    result = await session.execute(statement)
    return result.scalars().unique().one_or_none()
