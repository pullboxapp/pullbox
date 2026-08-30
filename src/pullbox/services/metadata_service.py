"""Metadata service — orchestrates ComicVine metadata operations.

Handles fetching, caching, and refreshing series/issue metadata from
external providers. Cover images are downloaded and stored locally.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path  # noqa: TC003 — used at runtime via parameter values
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import delete, select

from pullbox.core.exceptions import NotFoundError, ProviderError
from pullbox.core.issue_numbers import format_issue_number, normalize_issue_number_text
from pullbox.core.name_matcher import NameMatcher
from pullbox.core.naming import (
    classify_series_type,
    detect_issue_type_from_metadata_title,
    extract_base_series_title,
)
from pullbox.core.type_semantics import TypeFamily, issue_type_family
from pullbox.models.creator import Creator, IssueCreator
from pullbox.models.issue import Issue, IssueType
from pullbox.models.publisher import Publisher
from pullbox.models.series import (
    IssueCatalogState,
    Series,
    SeriesStatus,
    SeriesStatusOverride,
    SeriesType,
)
from pullbox.providers.base import SeriesMetadata
from pullbox.providers.metadata.comicvine import ComicVineError
from pullbox.services.cover_cache_service import purge_series_cover_cache

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.providers.base import IssueSummary
    from pullbox.providers.metadata.comicvine import ComicVineProvider

logger = structlog.get_logger(__name__)


def _provider_error_from_comicvine(exc: ComicVineError) -> ProviderError:
    """Preserve provider failure classification across the service boundary."""
    return ProviderError(
        "comicvine",
        str(exc),
        details={"status_code": exc.status_code, "retryable": exc.retryable},
    )


def _exact_issue_number_text(
    issue_number: float,
    issue_number_text: str | None,
) -> str:
    """Normalize provider exact text or derive it for legacy provider DTOs."""
    if issue_number_text is None:
        return format_issue_number(issue_number)
    return normalize_issue_number_text(issue_number_text)


# Mapping from IssueType → SeriesType for propagating detected issue types
# to the parent series when all issues share a single non-standard type.
_ISSUE_TO_SERIES_TYPE: dict[IssueType, SeriesType] = {
    IssueType.ANNUAL: SeriesType.ANNUAL,
    IssueType.TPB: SeriesType.TPB,
    IssueType.OMNIBUS: SeriesType.OMNIBUS,
    IssueType.GN: SeriesType.GRAPHIC_NOVEL,
    IssueType.OGN: SeriesType.GRAPHIC_NOVEL,
    IssueType.HC: SeriesType.HARDCOVER,
    IssueType.ONE_SHOT: SeriesType.ONE_SHOT,
    IssueType.SPECIAL: SeriesType.SPECIAL,
    IssueType.DELUXE: SeriesType.DELUXE,
    IssueType.COMPENDIUM: SeriesType.COMPENDIUM,
    IssueType.VOLUME: SeriesType.VOLUME,
}


# Reverse mapping: SeriesType → IssueType for inheriting type from series
_SERIES_TO_ISSUE_TYPE: dict[SeriesType, IssueType] = {
    SeriesType.ANNUAL: IssueType.ANNUAL,
    SeriesType.TPB: IssueType.TPB,
    SeriesType.OMNIBUS: IssueType.OMNIBUS,
    SeriesType.GRAPHIC_NOVEL: IssueType.GN,
    SeriesType.HARDCOVER: IssueType.HC,
    SeriesType.ONE_SHOT: IssueType.ONE_SHOT,
    SeriesType.SPECIAL: IssueType.SPECIAL,
    SeriesType.DELUXE: IssueType.DELUXE,
    SeriesType.COMPENDIUM: IssueType.COMPENDIUM,
    SeriesType.VOLUME: IssueType.VOLUME,
}

_EXPLICIT_IMPORT_METADATA_SOURCES = frozenset({"provisional_import", "import_placeholder"})
_UNTRUSTED_LEGACY_SERIES_TYPES = frozenset({SeriesType.ONE_SHOT, SeriesType.SPECIAL})


def _series_type_from_complete_issue_evidence(
    issue_types: list[IssueType],
) -> SeriesType | None:
    """Return consensus only when every issue supplies non-standard type evidence."""
    if not issue_types or IssueType.ISSUE in issue_types:
        return None
    distinct_types = set(issue_types)
    if len(distinct_types) == 1:
        return _ISSUE_TO_SERIES_TYPE.get(next(iter(distinct_types)))
    if all(issue_type_family(issue_type) == TypeFamily.COLLECTION for issue_type in distinct_types):
        return SeriesType.VOLUME
    return None


class MetadataService:
    """Orchestrates metadata fetching and caching.

    Args:
        provider: ComicVine metadata provider instance.
        covers_dir: Base directory for cover image storage.
        refresh_days: Minimum days before re-fetching metadata.
    """

    def __init__(
        self,
        provider: ComicVineProvider,
        covers_dir: Path,
        refresh_days: int = 30,
    ) -> None:
        self._provider = provider
        self._covers_dir = covers_dir
        self._refresh_days = refresh_days

    async def fetch_series(
        self,
        session: AsyncSession,
        comicvine_id: int,
        *,
        download_cover: bool = True,
    ) -> Series:
        """Fetch and cache full series metadata from ComicVine.

        Creates or updates the Series and Publisher records in the database.
        The CDN cover URL is persisted on ``series.cover_url`` so templates
        can display it immediately while a local copy is downloaded later.
        """
        log = logger.bind(comicvine_id=comicvine_id)
        log.debug("metadata_fetch_series")

        meta = await self.get_series_metadata(comicvine_id)
        series = await self.upsert_series_metadata(
            session,
            comicvine_id,
            meta,
        )

        # Download cover into the series folder (if it exists)
        if download_cover and meta.cover_url:
            await self.download_series_cover(series, meta.cover_url)

        return series

    async def upsert_series_metadata(
        self,
        session: AsyncSession,
        comicvine_id: int,
        meta: SeriesMetadata,
    ) -> Series:
        """Create or update a local Series row from prefetched provider metadata.

        This split lets long-running workflows fetch provider data before they
        enter a write-heavy section, keeping SQLite write locks short.
        """
        log = logger.bind(comicvine_id=comicvine_id)

        publisher_id = None
        if meta.publisher:
            publisher_id = await self._ensure_publisher(session, meta.publisher)

        existing = (
            await session.execute(select(Series).where(Series.comicvine_id == comicvine_id))
        ).scalar_one_or_none()

        if existing:
            existing.title = meta.title
            existing.sort_title = meta.sort_title or meta.title
            if meta.year_start is not None:
                existing.year_start = meta.year_start
            if existing.status_override == SeriesStatusOverride.CONTINUING:
                existing.status = SeriesStatus.CONTINUING
                existing.year_end = None
            elif meta.year_end is not None:
                existing.year_end = meta.year_end
            if meta.status and existing.status_override is None:
                existing.status = SeriesStatus(meta.status)
            if meta.description is not None:
                existing.description = meta.description
            if meta.issue_count is not None:
                existing.issue_count = meta.issue_count
            existing.comicvine_url = meta.comicvine_url
            existing.cover_url = meta.cover_url
            existing.publisher_id = publisher_id
            existing.metadata_last_refreshed = datetime.now(UTC)
            existing.metadata_source = "comicvine"
            series = existing
            log.debug("metadata_series_updated", series_id=series.id)
        else:
            series = Series(
                comicvine_id=comicvine_id,
                title=meta.title,
                sort_title=meta.sort_title or meta.title,
                year_start=meta.year_start,
                year_end=meta.year_end,
                status=SeriesStatus(meta.status) if meta.status else SeriesStatus.CONTINUING,
                description=meta.description,
                issue_count=meta.issue_count or 0,
                comicvine_url=meta.comicvine_url,
                cover_url=meta.cover_url,
                publisher_id=publisher_id,
                metadata_last_refreshed=datetime.now(UTC),
                metadata_source="comicvine",
            )
            session.add(series)
            await session.flush()
            await purge_series_cover_cache(
                session,
                series.id,
                extra_base_dirs=(self._covers_dir,),
            )
            series.cover_path = None
            log.debug("metadata_series_created", series_id=series.id)

        await self.classify_and_link_series(session, series)
        return series

    async def get_series_metadata(
        self,
        comicvine_id: int,
    ) -> SeriesMetadata:
        """Fetch provider series metadata without creating or updating local rows."""
        log = logger.bind(comicvine_id=comicvine_id)
        log.debug("metadata_get_series_metadata")

        try:
            return await self._provider.get_series(str(comicvine_id))
        except ComicVineError as exc:
            raise _provider_error_from_comicvine(exc) from exc

    async def get_cached_series_metadata(
        self,
        comicvine_id: int,
    ) -> SeriesMetadata | None:
        """Return fresh cached series metadata without starting a provider request."""
        cached_lookup = getattr(type(self._provider), "get_series_cached", None)
        if cached_lookup is None:
            return None
        cached_metadata = await cached_lookup(self._provider, str(comicvine_id))
        return cached_metadata if isinstance(cached_metadata, SeriesMetadata) else None

    async def get_issue_summaries_for_series(
        self,
        comicvine_id: int,
    ) -> list[IssueSummary]:
        """Fetch provider issue summaries for a series without touching local issues."""
        log = logger.bind(comicvine_id=comicvine_id)
        log.debug("metadata_get_issue_summaries_for_series")

        try:
            return await self._provider.get_issues_for_series(str(comicvine_id))
        except ComicVineError as exc:
            raise _provider_error_from_comicvine(exc) from exc

    async def get_recent_issue_summaries_for_series(
        self,
        comicvine_id: int,
        *,
        limit: int = 100,
    ) -> list[IssueSummary]:
        """Fetch recent provider issue summaries for a series without touching local issues."""
        log = logger.bind(comicvine_id=comicvine_id, limit=limit)
        log.debug("metadata_get_recent_issue_summaries_for_series")

        try:
            return await self._provider.get_recent_issues_for_series(
                str(comicvine_id),
                limit=limit,
            )
        except ComicVineError as exc:
            raise _provider_error_from_comicvine(exc) from exc

    async def classify_and_link_series(
        self,
        session: AsyncSession,
        series: Series,
    ) -> None:
        """Detect series type from CV title and link to parent series.

        Sets ``series.series_type`` based on title analysis.  If the series
        is a variant (annual, TPB, etc.), searches for a matching base series
        in the database and sets ``series.parent_series_id``.
        """
        previous_type = series.series_type
        detected = SeriesType(
            classify_series_type(
                series.title,
                description=series.description,
                issue_count=series.issue_count,
                year_start=series.year_start,
            )
        )
        if detected == SeriesType.STANDARD and previous_type != SeriesType.STANDARD:
            issue_types = [
                IssueType(detect_issue_type_from_metadata_title(title))
                for title in (
                    await session.execute(select(Issue.title).where(Issue.series_id == series.id))
                )
                .scalars()
                .all()
            ]
            issue_consensus = _series_type_from_complete_issue_evidence(issue_types)
            if issue_consensus is not None:
                detected = issue_consensus
            elif previous_type not in _UNTRUSTED_LEGACY_SERIES_TYPES:
                # Existing collection classifications may come from provider
                # summary fields that are not represented in local prose.
                detected = previous_type
        series.series_type = detected
        await self._repair_issue_types_after_series_change(
            session,
            series,
            previous_type=previous_type,
        )

        if detected == SeriesType.STANDARD:
            series.parent_series_id = None
            return  # Nothing to link

        # Extract the base title (e.g. "Batman Annual" -> "Batman")
        base_title = extract_base_series_title(series.title)
        series.parent_series_id = None
        if base_title == series.title:
            return  # Couldn't extract a different base title

        # Search for a standard parent series with the base title + similar year
        parent = await self._find_parent_series(
            session,
            base_title,
            series.year_start,
        )

        if parent:
            series.parent_series_id = parent.id
            logger.info(
                "series_parent_linked",
                series_id=series.id,
                parent_series_id=parent.id,
                series_type=detected.value,
                child_title=series.title,
                parent_title=parent.title,
            )

    @staticmethod
    async def _repair_issue_types_after_series_change(
        session: AsyncSession,
        series: Series,
        *,
        previous_type: SeriesType,
    ) -> None:
        """Repair only default/inherited issue types when series evidence changes."""
        if series.id is None or previous_type == series.series_type:
            return

        old_inherited = _SERIES_TO_ISSUE_TYPE.get(previous_type)
        new_inherited = _SERIES_TO_ISSUE_TYPE.get(series.series_type, IssueType.ISSUE)
        issues = (
            (await session.execute(select(Issue).where(Issue.series_id == series.id)))
            .scalars()
            .all()
        )
        repaired = 0
        for issue in issues:
            explicit_type = IssueType(detect_issue_type_from_metadata_title(issue.title))
            if explicit_type != IssueType.ISSUE:
                if issue.issue_type in {IssueType.ISSUE, old_inherited}:
                    issue.issue_type = explicit_type
                    repaired += 1
                continue

            if (
                issue.metadata_source in _EXPLICIT_IMPORT_METADATA_SOURCES
                and issue.issue_type != IssueType.ISSUE
            ):
                continue
            was_old_inherited = old_inherited is not None and issue.issue_type == old_inherited
            needs_new_inheritance = (
                issue.issue_type == IssueType.ISSUE and new_inherited != IssueType.ISSUE
            )
            if was_old_inherited or needs_new_inheritance:
                issue.issue_type = new_inherited
                repaired += 1

        if repaired:
            logger.info(
                "series_issue_types_repaired",
                series_id=series.id,
                previous_series_type=previous_type.value,
                series_type=series.series_type.value,
                repaired=repaired,
            )

    @staticmethod
    async def _find_parent_series(
        session: AsyncSession,
        base_title: str,
        year: int | None,
    ) -> Series | None:
        """Find a standard series matching the base title and year."""
        normalized = NameMatcher.normalize(base_title)

        # Search for standard series with similar titles
        result = await session.execute(
            select(Series).where(
                Series.series_type == SeriesType.STANDARD,
                Series.title.ilike(f"%{base_title}%"),
            )
        )
        candidates = list(result.scalars().all())

        # Exact normalized match with year within ±1
        for s in candidates:
            if (
                NameMatcher.normalize(s.title) == normalized
                and year
                and s.year_start
                and abs(year - s.year_start) <= 1
            ):
                return s

        # Exact normalized match without year constraint
        for s in candidates:
            if NameMatcher.normalize(s.title) == normalized:
                return s

        return None

    async def download_series_cover(
        self,
        series: Series,
        cover_url: str,
    ) -> None:
        """Download cover art into the ``.covers/{series_id}/`` directory.

        Always writes to the centralized covers directory so comic files
        and cover images are stored separately.
        """
        cover_dest = self._covers_dir / str(series.id) / "series.jpg"

        await self.download_cover(cover_url, cover_dest)
        if cover_dest.exists():
            series.cover_path = f"/api/v1/series/{series.id}/cover"

    async def fetch_issue(
        self,
        session: AsyncSession,
        comicvine_id: int,
    ) -> Issue:
        """Fetch and cache full issue metadata from ComicVine."""
        log = logger.bind(comicvine_id=comicvine_id)
        log.debug("metadata_fetch_issue")

        try:
            meta = await self._provider.get_issue(str(comicvine_id))
        except ComicVineError as exc:
            raise _provider_error_from_comicvine(exc) from exc

        existing = (
            await session.execute(select(Issue).where(Issue.comicvine_id == comicvine_id))
        ).scalar_one_or_none()

        if existing:
            existing.issue_number = meta.issue_number
            existing.issue_number_text = _exact_issue_number_text(
                meta.issue_number,
                meta.issue_number_text,
            )
            existing.title = meta.title
            existing.description = meta.description
            existing.comicvine_url = meta.comicvine_url
            existing.release_date = _parse_date(meta.release_date)
            existing.store_date = _parse_date(meta.store_date)
            existing.cover_url = meta.cover_url
            if meta.page_count and meta.page_count > 0:
                existing.page_count = meta.page_count
            if meta.creators:
                await self._sync_issue_creators(session, existing, meta.creators)
            existing.metadata_source = "comicvine"
            issue = existing
            log.debug("metadata_issue_updated", issue_id=issue.id)
        else:
            raise NotFoundError("Issue", comicvine_id)

        return issue

    async def _sync_issue_creators(
        self,
        session: AsyncSession,
        issue: Issue,
        creators: list[dict[str, str]],
    ) -> None:
        """Replace issue creator credits with full provider issue metadata."""
        if issue.id is None:
            return

        await session.execute(delete(IssueCreator).where(IssueCreator.issue_id == issue.id))
        linked_creator_ids: set[int] = set()
        for credit in creators:
            name = (credit.get("name") or "").strip()
            role = (credit.get("role") or "").strip()
            if not name or not role:
                continue

            provider_id = _parse_optional_int(credit.get("provider_id"))
            creator = await self._get_or_create_creator(
                session,
                name=name,
                comicvine_id=provider_id,
                comicvine_url=(credit.get("comicvine_url") or None),
            )
            if creator.id is None or creator.id in linked_creator_ids:
                continue
            linked_creator_ids.add(creator.id)
            session.add(IssueCreator(issue_id=issue.id, creator_id=creator.id, role=role))

    @staticmethod
    async def _get_or_create_creator(
        session: AsyncSession,
        *,
        name: str,
        comicvine_id: int | None,
        comicvine_url: str | None,
    ) -> Creator:
        creator: Creator | None = None
        if comicvine_id is not None:
            creator = (
                await session.execute(select(Creator).where(Creator.comicvine_id == comicvine_id))
            ).scalar_one_or_none()
        if creator is None:
            creator = (
                await session.execute(select(Creator).where(Creator.name == name))
            ).scalar_one_or_none()
        if creator is None:
            creator = Creator(name=name, comicvine_id=comicvine_id, comicvine_url=comicvine_url)
            session.add(creator)
            await session.flush()
            return creator

        creator.name = name
        if comicvine_id is not None:
            creator.comicvine_id = comicvine_id
        if comicvine_url:
            creator.comicvine_url = comicvine_url
        return creator

    async def fetch_issues_for_series(
        self,
        session: AsyncSession,
        series_id: int,
    ) -> list[Issue]:
        """Fetch all issues for a series from ComicVine, creating/updating DB records."""
        log = logger.bind(series_id=series_id)
        log.debug("metadata_fetch_issues_for_series")

        series = await session.get(Series, series_id)
        if not series or not series.comicvine_id:
            raise NotFoundError("Series", series_id)

        summaries = await self.get_issue_summaries_for_series(series.comicvine_id)
        return await self.upsert_issue_summaries(
            session,
            series,
            summaries,
            infer_series_type_from_summaries=True,
        )

    async def fetch_recent_issues_for_series(
        self,
        session: AsyncSession,
        series_id: int,
        *,
        limit: int = 100,
    ) -> list[Issue]:
        """Fetch recent issues for a series from ComicVine, creating/updating DB records."""
        log = logger.bind(series_id=series_id, limit=limit)
        log.debug("metadata_fetch_recent_issues_for_series")

        series = await session.get(Series, series_id)
        if not series or not series.comicvine_id:
            raise NotFoundError("Series", series_id)

        summaries = await self.get_recent_issue_summaries_for_series(
            series.comicvine_id,
            limit=limit,
        )
        return await self.upsert_issue_summaries(
            session,
            series,
            summaries,
            infer_series_type_from_summaries=False,
        )

    async def upsert_issue_summaries(
        self,
        session: AsyncSession,
        series: Series,
        summaries: list[IssueSummary],
        *,
        infer_series_type_from_summaries: bool = False,
    ) -> list[Issue]:
        """Create or update local Issue rows from prefetched provider summaries."""
        series_id = series.id
        if series_id is None:
            raise NotFoundError("Series", "unpersisted")
        log = logger.bind(series_id=series_id)

        created = []
        provider_issue_ids = [int(summary.provider_id) for summary in summaries]
        existing_result = await session.execute(select(Issue).where(Issue.series_id == series_id))
        existing_issues = list(existing_result.scalars().all())
        provider_result = await session.execute(
            select(Issue).where(Issue.comicvine_id.in_(provider_issue_ids))
        )
        existing_provider_issues = list(provider_result.scalars().all())
        existing_by_text = {issue.effective_issue_number_text: issue for issue in existing_issues}
        legacy_by_number = {
            issue.issue_number: issue
            for issue in existing_issues
            if issue.issue_number_text is None
        }
        existing_by_provider_id = {
            int(issue.comicvine_id): issue
            for issue in existing_provider_issues
            if issue.comicvine_id is not None
        }
        summary_evidence_types: list[IssueType] = []
        for summary in summaries:
            provider_issue_id = int(summary.provider_id)
            exact_issue_number_text = _exact_issue_number_text(
                summary.issue_number,
                summary.issue_number_text,
            )
            assign_provider_issue_id = True
            sync_issue_identity = True
            existing = existing_by_text.get(exact_issue_number_text)
            if existing is None:
                existing = legacy_by_number.get(summary.issue_number)
            existing_by_provider = existing_by_provider_id.get(provider_issue_id)
            if existing_by_provider is not None and existing_by_provider.series_id != series_id:
                log.warning(
                    "issue_summary_provider_id_external_collision",
                    provider_issue_id=provider_issue_id,
                    existing_issue_id=existing_by_provider.id,
                    existing_series_id=existing_by_provider.series_id,
                    target_issue_number=summary.issue_number,
                )
                existing_by_provider = None
                assign_provider_issue_id = False
            if existing_by_provider is not None and existing_by_provider is not existing:
                if existing is None:
                    old_issue_number = existing_by_provider.issue_number
                    old_issue_number_text = existing_by_provider.effective_issue_number_text
                    existing = existing_by_provider
                    existing.issue_number = summary.issue_number
                    existing.issue_number_text = exact_issue_number_text
                    if legacy_by_number.get(old_issue_number) is existing:
                        legacy_by_number.pop(old_issue_number, None)
                    existing_by_text.pop(old_issue_number_text, None)
                    existing_by_text[exact_issue_number_text] = existing
                else:
                    log.warning(
                        "issue_summary_provider_id_collision",
                        provider_issue_id=provider_issue_id,
                        existing_issue_id=existing_by_provider.id,
                        existing_issue_number=existing_by_provider.issue_number,
                        target_issue_id=existing.id,
                        target_issue_number=summary.issue_number,
                    )
                    existing = existing_by_provider
                    sync_issue_identity = False

            # Compact provider type and provider title are explicit evidence.
            # Series inheritance is a fallback and cannot establish consensus.
            explicit_type = IssueType.ISSUE
            with contextlib.suppress(ValueError):
                explicit_type = IssueType(summary.issue_type)
            if explicit_type == IssueType.ISSUE:
                with contextlib.suppress(ValueError):
                    explicit_type = IssueType(detect_issue_type_from_metadata_title(summary.title))
            summary_evidence_types.append(explicit_type)
            detected_type = explicit_type
            if detected_type == IssueType.ISSUE:
                detected_type = _SERIES_TO_ISSUE_TYPE.get(
                    series.series_type,
                    IssueType.ISSUE,
                )

            if existing:
                if sync_issue_identity:
                    old_issue_number = existing.issue_number
                    old_issue_number_text = existing.effective_issue_number_text
                    existing.issue_number = summary.issue_number
                    existing.issue_number_text = exact_issue_number_text
                    if legacy_by_number.get(old_issue_number) is existing:
                        legacy_by_number.pop(old_issue_number, None)
                    if existing_by_text.get(old_issue_number_text) is existing:
                        existing_by_text.pop(old_issue_number_text, None)
                    existing_by_text[exact_issue_number_text] = existing
                if assign_provider_issue_id:
                    existing.comicvine_id = provider_issue_id
                if summary.title:
                    existing.title = summary.title
                if summary.release_date:
                    existing.release_date = _parse_date(summary.release_date)
                if summary.cover_url and not existing.cover_url:
                    existing.cover_url = summary.cover_url
                preserve_explicit_import_type = (
                    explicit_type == IssueType.ISSUE
                    and existing.metadata_source in _EXPLICIT_IMPORT_METADATA_SOURCES
                    and existing.issue_type != IssueType.ISSUE
                )
                if not preserve_explicit_import_type:
                    existing.issue_type = detected_type
                    existing.metadata_source = "comicvine"
            else:
                issue = Issue(
                    series_id=series_id,
                    comicvine_id=provider_issue_id if assign_provider_issue_id else None,
                    issue_number=summary.issue_number,
                    issue_number_text=exact_issue_number_text,
                    title=summary.title,
                    release_date=_parse_date(summary.release_date),
                    cover_url=summary.cover_url,
                    issue_type=detected_type,
                    metadata_source="comicvine",
                )
                session.add(issue)
                created.append(issue)
                existing_by_text[exact_issue_number_text] = issue
                if assign_provider_issue_id:
                    existing_by_provider_id[provider_issue_id] = issue

        await session.flush()

        # Only a complete catalog where every summary has explicit non-standard
        # evidence may infer the series type. Recent/targeted subsets and a mix
        # of ordinary plus special issues must never reclassify the parent.
        if (
            infer_series_type_from_summaries
            and summaries
            and classify_series_type(
                series.title,
                description=series.description,
                issue_count=series.issue_count,
                year_start=series.year_start,
            )
            == SeriesType.STANDARD.value
        ):
            inferred_series_type = _series_type_from_complete_issue_evidence(summary_evidence_types)
            if inferred_series_type is not None:
                series.series_type = inferred_series_type
                log.debug(
                    "series_type_inferred_from_issues",
                    series_id=series_id,
                    series_type=inferred_series_type.value,
                    source_issue_types=sorted(
                        {issue_type.value for issue_type in summary_evidence_types}
                    ),
                )

        log.debug(
            "metadata_issues_synced",
            total=len(summaries),
            created=len(created),
        )
        return created

    @staticmethod
    async def derive_series_end_year(session: AsyncSession, series: Series) -> int | None:
        """Derive a closed end year for a series from issue dates or start year."""
        from sqlalchemy import func as sa_func

        result = await session.execute(
            select(sa_func.max(sa_func.coalesce(Issue.release_date, Issue.store_date))).where(
                Issue.series_id == series.id,
                sa_func.coalesce(Issue.release_date, Issue.store_date).isnot(None),
            )
        )
        latest_release = result.scalar_one_or_none()
        if latest_release is not None:
            return latest_release.year
        return series.year_start

    @staticmethod
    async def infer_series_status(session: AsyncSession, series: Series) -> None:
        """Infer continuing/ended status from series type and issue dates.

        Non-standard series types (one-shots, TPBs, graphic novels, etc.) are
        always ended. For standard series, if the latest issue was released
        within the last 6 months, assume continuing; otherwise assume ended.
        """

        if series.status_override is not None:
            series.status = SeriesStatus(series.status_override.value)
            if series.status == SeriesStatus.CONTINUING:
                series.year_end = None
            elif series.year_end is None:
                series.year_end = await MetadataService.derive_series_end_year(session, series)
            return

        # Non-standard types are always ended by definition.
        if series.series_type != SeriesType.STANDARD:
            series.status = SeriesStatus.ENDED
            series.year_end = await MetadataService.derive_series_end_year(session, series)
            return

        from sqlalchemy import func as sa_func

        result = await session.execute(
            select(sa_func.max(sa_func.coalesce(Issue.release_date, Issue.store_date))).where(
                Issue.series_id == series.id,
                sa_func.coalesce(Issue.release_date, Issue.store_date).isnot(None),
            )
        )
        latest_release = result.scalar_one_or_none()
        if latest_release is None:
            return

        cutoff = date.today() - timedelta(days=180)
        if latest_release >= cutoff:
            series.status = SeriesStatus.CONTINUING
            series.year_end = None
            return

        series.status = SeriesStatus.ENDED
        series.year_end = latest_release.year

    async def refresh_series(
        self,
        session: AsyncSession,
        series_id: int,
        *,
        force: bool = False,
    ) -> Series:
        """Refresh metadata for a series if stale (past refresh interval).

        Args:
            force: Skip the stale check and always refresh.
        """
        series = await session.get(Series, series_id)
        if not series:
            raise NotFoundError("Series", series_id)

        if not force and series.metadata_last_refreshed:
            refreshed = series.metadata_last_refreshed
            if refreshed.tzinfo is None:
                refreshed = refreshed.replace(tzinfo=UTC)
            age = datetime.now(UTC) - refreshed
            if age.days < self._refresh_days:
                logger.debug(
                    "metadata_refresh_skipped",
                    series_id=series_id,
                    age_days=age.days,
                )
                # noinspection PyTypeChecker
                return series

        if not series.comicvine_id:
            raise ProviderError("comicvine", "Series has no ComicVine ID")

        # noinspection PyTypeChecker
        series = await self.fetch_series(session, series.comicvine_id)
        await self.fetch_issues_for_series(session, series.id)
        await self.infer_series_status(session, series)
        synced_at = datetime.now(UTC)
        series.issue_catalog_state = IssueCatalogState.COMPLETE
        series.issue_catalog_last_synced_at = synced_at
        series.issue_catalog_last_checked_at = synced_at
        series.issue_catalog_error = None
        return series

    async def download_cover(self, url: str, destination: Path) -> None:
        """Download and save a cover image."""
        log = logger.bind(url=url, destination=str(destination))
        log.debug("metadata_download_cover")

        try:
            image_bytes = await self._provider.get_cover_image(url)
        except ComicVineError as exc:
            log.error("metadata_cover_download_failed", error=str(exc))
            return

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(image_bytes)
        log.debug("metadata_cover_saved", size_bytes=len(image_bytes))

    @staticmethod
    async def _ensure_publisher(session: AsyncSession, name: str) -> int:
        """Find or create a publisher by name, returning the ID."""
        existing = (
            await session.execute(select(Publisher).where(Publisher.name == name))
        ).scalar_one_or_none()

        if existing:
            return existing.id

        publisher = Publisher(name=name)
        session.add(publisher)
        await session.flush()
        return publisher.id


def _parse_date(value: str | None) -> date | None:
    """Parse a date string (YYYY-MM-DD) to a date object, or None."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, int | str | float):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
