"""Targeted canonical seeding for arcs without whole-series adoption side effects."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from pullbox.core.issue_numbers import parse_issue_number_text
from pullbox.core.library_naming import build_series_relative_path
from pullbox.core.library_policy import load_effective_library_ingest_policy
from pullbox.core.naming import classify_series_type, detect_issue_type_from_metadata_title
from pullbox.core.type_semantics import canonical_issue_type_for_series_type
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import LibraryRoot
from pullbox.models.publisher import Publisher
from pullbox.models.series import IssueCatalogState, Series, SeriesStatus, SeriesType
from pullbox.services.story_arc_catalog_types import StoryArcCatalogError, exact_provider_id

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.providers.base import SeriesMetadata
    from pullbox.services.story_arc_catalog_types import StoryArcCatalogPreview


async def canonical_root(session: AsyncSession, root_id: int) -> LibraryRoot:
    if isinstance(root_id, bool) or not isinstance(root_id, int) or root_id < 1:
        raise StoryArcCatalogError("canonical_root_required", "Select a canonical library root")
    root = await session.get(LibraryRoot, root_id)
    if (
        root is None
        or not root.enabled
        or not Path(root.path).is_absolute()
        or not Path(root.path).is_dir()
    ):
        raise StoryArcCatalogError(
            "canonical_root_unavailable", "Canonical library root is unavailable"
        )
    return root


async def publisher_id(session: AsyncSession, name: str | None) -> int | None:
    if not name:
        return None
    publisher = await session.scalar(select(Publisher).where(Publisher.name == name))
    if publisher is None:
        publisher = Publisher(name=name)
        session.add(publisher)
        await session.flush()
    return publisher.id


async def seed_members(
    session: AsyncSession,
    preview: StoryArcCatalogPreview,
    root: LibraryRoot,
    provider_ids: Sequence[str],
) -> dict[str, Issue]:
    """Create only absent exact identities; all existing rows remain untouched.

    The caller's savepoint makes identity/path conflicts atomic. Paths are reserved
    in the database only; normal acquisition creates canonical folders later.
    """
    metadata_by_id = {issue.provider_id: issue for issue in preview.issues}
    series_metadata = {series.provider_id: series for series in preview.series}
    result: dict[str, Issue] = {}
    parents: dict[str, Series] = {}
    for provider_id in provider_ids:
        metadata = metadata_by_id[provider_id]
        parent_key = metadata.series_provider_id
        parent = parents.get(parent_key)
        if parent is None:
            parent = await session.scalar(
                select(Series).where(Series.comicvine_id == exact_provider_id(parent_key))
            )
            if parent is None:
                parent_metadata = series_metadata.get(parent_key)
                if parent_metadata is None:
                    raise StoryArcCatalogError(
                        "parent_metadata_missing", "Canonical parent changed; refresh the preview"
                    )
                parent = await _new_series(session, parent_metadata, root)
            parents[parent_key] = parent
        number, exact_number = parse_issue_number_text(
            metadata.issue_number_text or metadata.issue_number
        )
        issue = await session.scalar(
            select(Issue).where(Issue.comicvine_id == exact_provider_id(provider_id))
        )
        if issue is not None:
            if issue.series_id != parent.id or issue.effective_issue_number_text != exact_number:
                raise StoryArcCatalogError(
                    "identity_conflict",
                    "Existing issue disagrees with the provider's exact identity",
                )
        else:
            sibling = await session.scalar(
                select(Issue.id).where(
                    Issue.series_id == parent.id, Issue.issue_number_text == exact_number
                )
            )
            if sibling is not None:
                raise StoryArcCatalogError(
                    "identity_conflict",
                    "An issue with a different identity already uses this exact number",
                )
            detected_type = IssueType(detect_issue_type_from_metadata_title(metadata.title))
            if detected_type is IssueType.ISSUE:
                detected_type = canonical_issue_type_for_series_type(parent.series_type)
            issue = Issue(
                series_id=parent.id,
                comicvine_id=exact_provider_id(provider_id),
                issue_number=number,
                issue_number_text=exact_number,
                title=metadata.title,
                description=metadata.description,
                release_date=_date(metadata.release_date),
                store_date=_date(metadata.store_date),
                cover_url=metadata.cover_url,
                comicvine_url=metadata.comicvine_url,
                page_count=metadata.page_count,
                metadata_source="comicvine",
                issue_type=detected_type,
                status=IssueStatus.SKIPPED,
                manual_skip=False,
            )
            session.add(issue)
            await session.flush()
        result[provider_id] = issue
    return result


async def _new_series(session: AsyncSession, metadata: SeriesMetadata, root: LibraryRoot) -> Series:
    series = Series(
        comicvine_id=exact_provider_id(metadata.provider_id),
        title=metadata.title,
        sort_title=metadata.sort_title or metadata.title,
        year_start=metadata.year_start,
        year_end=metadata.year_end,
        description=metadata.description,
        cover_url=metadata.cover_url,
        comicvine_url=metadata.comicvine_url,
        issue_count=metadata.issue_count or 0,
        status=SeriesStatus.ENDED if metadata.status == "ended" else SeriesStatus.CONTINUING,
        metadata_source="comicvine_partial",
        monitored=False,
        issue_catalog_state=IssueCatalogState.PARTIAL,
        series_type=SeriesType(
            classify_series_type(
                metadata.title,
                description=metadata.description,
                issue_count=metadata.issue_count or 0,
                year_start=metadata.year_start,
            )
        ),
        library_root_id=root.id,
    )
    identifier = await publisher_id(session, metadata.publisher)
    series.publisher = await session.get(Publisher, identifier) if identifier is not None else None
    session.add(series)
    policy = await load_effective_library_ingest_policy(session, root)
    path = Path(root.path) / build_series_relative_path(series, policy)
    if await _path_claimed(session, path):
        path = path.with_name(f"{path.name} [cv-{metadata.provider_id}]")
        if await _path_claimed(session, path):
            raise StoryArcCatalogError(
                "canonical_path_collision", "Canonical series path is already in use"
            )
    if not path.resolve().is_relative_to(Path(root.path).resolve()):
        raise StoryArcCatalogError(
            "canonical_path_unsafe", "Canonical series path escapes its library root"
        )
    if len(str(path)) > 1000:
        raise StoryArcCatalogError("canonical_path_unsafe", "Canonical series path is too long")
    series.path = str(path)
    session.add(series)
    await session.flush()
    return series


async def _path_claimed(session: AsyncSession, path: Path) -> bool:
    return (
        path.exists()
        or path.is_symlink()
        or bool(
            await session.scalar(
                select(Series.id).where(func.lower(Series.path) == str(path).lower()).limit(1)
            )
        )
    )


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
