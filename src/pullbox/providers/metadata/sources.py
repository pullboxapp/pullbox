"""Executable source registrations; keep legacy import/cache consumers unchanged."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from pullbox.config import get_settings
from pullbox.core.metadata_identity import ExternalIdentityRef, MetadataEntityKind, MetadataSource
from pullbox.providers.metadata import comicvine_normalization as normalize
from pullbox.providers.metadata.comicvine import ComicVineError, ComicVineProvider
from pullbox.providers.metadata.gcd_api_v2 import GcdApiV2Source
from pullbox.providers.metadata.gcd_local import GcdLocalSource
from pullbox.providers.metadata.metron import MetronSource
from pullbox.schemas.metadata_sources import (
    MetadataFetch,
    MetadataPage,
    ProviderIssueRead,
    ProviderSeriesRead,
    ProviderStoryArcRead,
    RecentIssueWindow,
    SeriesDiscoveryQuery,
    SourceCapability,
    SourceStatus,
)
from pullbox.services.catalog.contract import CatalogError
from pullbox.services.catalog.reader import get_catalog_reader
from pullbox.services.catalog.storage import disk_work
from pullbox.services.metadata_discovery import MetadataSourceError, SourcePage, SourceRegistration
from pullbox.services.metadata_source_reads import (
    issue_checkpoint,
    page_number,
    source_id,
    validate_recent_issue_window,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from pullbox.providers.base import SeriesSearchResult
    from pullbox.providers.story_arcs import StoryArcMetadata, StoryArcSearchResult
    from pullbox.services.catalog.reader import CatalogReader
    from pullbox.services.metadata_sources import SourceRuntime


_catalog_reads: dict[asyncio.AbstractEventLoop, set[asyncio.Task[object]]] = {}


async def _catalog_read[T](operation: Callable[[], Awaitable[T]]) -> T:
    """Return promptly on cancellation without spawning unbounded disk workers."""
    loop = asyncio.get_running_loop()
    reads = _catalog_reads.setdefault(loop, set())
    if len(reads) >= 2:
        raise MetadataSourceError(SourceStatus.UNAVAILABLE, retry_after_seconds=1)

    async def owned_read() -> object:
        return await operation()

    def finished(task: asyncio.Task[object]) -> None:
        reads.discard(task)
        if not task.cancelled():
            task.exception()
        if not reads:
            _catalog_reads.pop(loop, None)

    task = asyncio.create_task(owned_read(), name="metadata-catalog-read")
    reads.add(task)
    task.add_done_callback(finished)
    try:
        return cast("T", await asyncio.shield(task))
    except asyncio.CancelledError:
        # disk_work joins its thread on cancellation. Retain the slot and task
        # until that owned read finishes; never abandon a database/file handle.
        task.cancel()
        raise


async def catalog_search_cache_token() -> str | None:
    """Use the same bounded disk admission as catalog discovery."""
    return await _catalog_read(get_catalog_reader().cache_token)


def _image_url(value: str | None) -> str | None:
    if not value or len(value) > 4096 or any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme == "https"
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and (
                host in {"comicvine.gamespot.com", "comicvine.com"}
                or host.endswith(".cbsistatic.com")
            )
        ):
            return value
    except ValueError:
        pass
    return None


def _page(
    source: MetadataSource,
    rows: list[SeriesSearchResult],
    *,
    total: int | None,
    offset: int,
    limit: int,
    has_more: bool,
) -> SourcePage:
    results = []
    rejected = 0
    for row in rows[:limit]:
        try:
            identity = ExternalIdentityRef(
                source.identity_namespace, MetadataEntityKind.SERIES, row.provider_id
            )
            if not row.title or not row.title.strip():
                raise ValueError("A series title is required")
            results.append(
                ProviderSeriesRead(
                    source=source,
                    identity_namespace=source.identity_namespace,
                    external_id=identity.external_id,
                    title=row.title[:500],
                    year_start=row.year_start,
                    publisher=row.publisher[:500] if row.publisher else None,
                    issue_count=row.issue_count,
                    description=row.description[:20000] if row.description else None,
                    resource_url=f"https://comicvine.gamespot.com/volume/4050-{identity.external_id}/",
                    image_url=_image_url(row.cover_url),
                )
            )
        except (ValueError, TypeError, AttributeError):
            rejected += 1
    next_offset = offset + limit if has_more and offset + limit <= 10000 else None
    return SourcePage(results, total, next_offset, rejected, has_more and next_offset is None)


def _api_error(exc: ComicVineError) -> MetadataSourceError:
    if exc.timed_out or exc.status_code in {408, 504}:
        status = SourceStatus.TIMEOUT
    elif exc.status_code in {100, 401, 403}:
        status = SourceStatus.AUTHENTICATION_FAILED
    elif exc.status_code in {107, 420, 429}:
        status = SourceStatus.RATE_LIMITED
    else:
        status = SourceStatus.UNAVAILABLE
    return MetadataSourceError(status, exc.retry_after_seconds)


def _cv_id(value: str, kind: MetadataEntityKind) -> str:
    identifier = source_id(MetadataSource.COMICVINE_API, kind, value)
    if int(identifier) >= 2**63:
        raise ValueError("ComicVine identity exceeds the supported range")
    return identifier


async def _fetch[T](operation: Callable[[], Awaitable[T | None]]) -> MetadataFetch[T]:
    try:
        data = await operation()
        return MetadataFetch(
            status=SourceStatus.OK if data is not None else SourceStatus.NOT_FOUND, data=data
        )
    except ComicVineError as exc:
        if exc.status_code == 101:
            return MetadataFetch(status=SourceStatus.NOT_FOUND)
        raise _api_error(exc) from None
    except (CatalogError, OSError):
        raise MetadataSourceError(SourceStatus.UNAVAILABLE) from None
    except (ValueError, TypeError, AttributeError, KeyError):
        raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE) from None


class ComicVineApiSource:
    def __init__(self, provider: ComicVineProvider) -> None:
        self.provider = provider

    async def recent_issues(
        self, external_id: str, *, since: datetime
    ) -> MetadataFetch[RecentIssueWindow]:
        identifier = _cv_id(external_id, MetadataEntityKind.SERIES)
        checkpoint = issue_checkpoint(since)

        async def read() -> RecentIssueWindow:
            rows, total = await self.provider.get_issues_page(identifier, newest_first=True)
            result = RecentIssueWindow(
                results=[normalize.issue(MetadataSource.COMICVINE_API, row) for row in rows],
                matched_total=total,
                scope="recent_publication",
                truncated=total > 100,
            )
            validate_recent_issue_window(
                result, MetadataSource.COMICVINE_API, identifier, checkpoint
            )
            return result

        return await _fetch(read)

    async def search(self, query: SeriesDiscoveryQuery, offset: int) -> SourcePage:
        try:
            if query.search_mode == "full":
                rows, total = await self.provider.search_series_candidates_page(
                    query.query, limit=query.limit_per_source, offset=offset
                )
            else:
                rows, total = await self.provider.search_series_page(
                    query.query,
                    query.year,
                    limit=query.limit_per_source,
                    offset=offset,
                    suppress_errors=False,
                    strict_response=True,
                )
            if total < len(rows) or len(rows) > query.limit_per_source:
                raise ValueError("Inconsistent source page")
            return _page(
                MetadataSource.COMICVINE_API,
                rows,
                total=total,
                offset=offset,
                limit=query.limit_per_source,
                has_more=offset + query.limit_per_source < total,
            )
        except ComicVineError as exc:
            raise _api_error(exc) from None
        except (ValueError, TypeError, AttributeError, KeyError):
            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE) from None

    async def check(self) -> None:
        page = await self.search(SeriesDiscoveryQuery(query="test", limit_per_source=1), 0)
        if page.rejected_results:
            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE)

    async def close(self) -> None:
        await self.provider.close()

    async def series(
        self, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderSeriesRead]:
        identifier = _cv_id(external_id, MetadataEntityKind.SERIES)

        async def read() -> ProviderSeriesRead:
            row = await self.provider.get_series(identifier, strict_response=True)
            return normalize.series(MetadataSource.COMICVINE_API, row, identifier)

        return await _fetch(read)

    async def issue(
        self, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderIssueRead]:
        identifier = _cv_id(external_id, MetadataEntityKind.ISSUE)

        async def read() -> ProviderIssueRead:
            return normalize.issue(
                MetadataSource.COMICVINE_API,
                await self.provider.get_issue(identifier, strict_response=True),
            )

        return await _fetch(read)

    async def issues(
        self, external_id: str, *, page: int = 1, validator: str | None = None
    ) -> MetadataFetch[MetadataPage[ProviderIssueRead]]:
        identifier = _cv_id(external_id, MetadataEntityKind.SERIES)
        page_number(page)

        async def read() -> MetadataPage[ProviderIssueRead]:
            rows, total = await self.provider.get_issues_page(identifier, page=page)
            return normalize.issue_page(MetadataSource.COMICVINE_API, rows, total, page)

        return await _fetch(read)

    @staticmethod
    def _arc(row: StoryArcSearchResult | StoryArcMetadata) -> ProviderStoryArcRead:
        identifier = _cv_id(row.provider_id, MetadataEntityKind.STORY_ARC)
        return ProviderStoryArcRead(
            source=MetadataSource.COMICVINE_API,
            identity_namespace=MetadataSource.COMICVINE_API.identity_namespace,
            external_id=identifier,
            title=row.title,
            description=row.description,
            publisher=row.publisher,
            declared_issue_count=row.declared_issue_count,
            image_url=_image_url(row.cover_url),
            resource_url=f"https://comicvine.gamespot.com/story-arc/4045-{identifier}/",
        )

    async def story_arcs(self, query: str, *, page: int = 1) -> MetadataPage[ProviderStoryArcRead]:
        page_number(page)
        if page > 100:
            raise ValueError("Story arc search is bounded to 100 source pages")
        try:
            rows, total = await self.provider.search_story_arcs_page(
                query, limit=100, offset=(page - 1) * 100
            )
            more = page * 100 < total
            return MetadataPage(
                results=[self._arc(row) for row in rows],
                total=total,
                next_page=page + 1 if more and page < 100 else None,
                truncated=more and page == 100,
            )
        except ComicVineError as exc:
            raise _api_error(exc) from None
        except (ValueError, TypeError, AttributeError):
            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE) from None

    async def story_arc(
        self, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderStoryArcRead]:
        identifier = _cv_id(external_id, MetadataEntityKind.STORY_ARC)

        async def read() -> ProviderStoryArcRead:
            row = await self.provider.get_story_arc(identifier)
            result = self._arc(row)
            result.issue_external_ids = list(row.issue_provider_ids)
            result.membership_complete = row.membership_complete
            result.warnings = list(row.warnings)
            return result

        return await _fetch(read)

    async def story_arc_issues(
        self, external_id: str, *, page: int = 1, validator: str | None = None
    ) -> MetadataFetch[MetadataPage[ProviderIssueRead]]:
        identifier = _cv_id(external_id, MetadataEntityKind.STORY_ARC)
        page_number(page)
        if page > 50:
            raise ValueError("Story arc membership is bounded to 5000 issues")

        async def read() -> MetadataPage[ProviderIssueRead]:
            arc = await self.provider.get_story_arc(identifier)
            if arc.provider_id != identifier or not arc.membership_complete:
                raise ValueError("Incomplete arc membership")
            ids = arc.issue_provider_ids[(page - 1) * 100 : page * 100]
            rows = await self.provider.get_story_arc_issues(ids) if ids else []
            if len(rows) != len(ids) or [row.provider_id for row in rows] != list(ids):
                raise ValueError("Different arc membership")
            return normalize.issue_page(
                MetadataSource.COMICVINE_API, rows, len(arc.issue_provider_ids), page
            )

        return await _fetch(read)


class ComicVineLocalSource:
    def __init__(self, reader: CatalogReader) -> None:
        self.reader = reader

    async def recent_issues(
        self, external_id: str, *, since: datetime
    ) -> MetadataFetch[RecentIssueWindow]:
        identifier = _cv_id(external_id, MetadataEntityKind.SERIES)
        checkpoint = issue_checkpoint(since)

        async def read() -> RecentIssueWindow:
            await self._available()
            rows, total, cutoff = await self.reader.recent_issues(int(identifier))
            result = RecentIssueWindow(
                results=[normalize.issue(MetadataSource.COMICVINE_LOCAL, row) for row in rows],
                matched_total=total,
                scope="recent_publication",
                truncated=total > 100,
                source_updated_at=cutoff,
            )
            validate_recent_issue_window(
                result, MetadataSource.COMICVINE_LOCAL, identifier, checkpoint
            )
            return result

        return await _fetch(lambda: _catalog_read(read))

    async def _available(self) -> None:
        if not await disk_work(lambda: self.reader.available):
            raise MetadataSourceError(SourceStatus.UNCONFIGURED)

    async def search(self, query: SeriesDiscoveryQuery, offset: int) -> SourcePage:
        return await _catalog_read(lambda: self._search(query, offset))

    async def _search(self, query: SeriesDiscoveryQuery, offset: int) -> SourcePage:
        await self._available()
        try:
            rows = await self.reader.search(
                query.query, query.year, query.limit_per_source + 1, offset
            )
        except (CatalogError, OSError, ValueError):
            raise MetadataSourceError(SourceStatus.UNAVAILABLE) from None
        return _page(
            MetadataSource.COMICVINE_LOCAL,
            rows,
            total=None,
            offset=offset,
            limit=query.limit_per_source,
            has_more=len(rows) > query.limit_per_source,
        )

    async def check(self) -> None:
        await _catalog_read(self._check)

    async def _check(self) -> None:
        await self._available()
        try:
            # Validate/open the active generation, even if it has no matching series.
            await self.reader.series(1)
        except (CatalogError, OSError, ValueError):
            raise MetadataSourceError(SourceStatus.UNAVAILABLE) from None

    async def close(self) -> None:
        return None

    async def series(
        self, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderSeriesRead]:
        identifier = _cv_id(external_id, MetadataEntityKind.SERIES)

        async def read() -> ProviderSeriesRead | None:
            await self._available()
            row = await self.reader.series(int(identifier))
            return (
                normalize.series(MetadataSource.COMICVINE_LOCAL, row, identifier) if row else None
            )

        return await _fetch(lambda: _catalog_read(read))

    async def issue(
        self, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderIssueRead]:
        identifier = _cv_id(external_id, MetadataEntityKind.ISSUE)

        async def read() -> ProviderIssueRead | None:
            await self._available()
            row = await self.reader.issue(int(identifier), preserve_number_text=True)
            return normalize.issue(MetadataSource.COMICVINE_LOCAL, row) if row else None

        return await _fetch(lambda: _catalog_read(read))

    async def issues(
        self, external_id: str, *, page: int = 1, validator: str | None = None
    ) -> MetadataFetch[MetadataPage[ProviderIssueRead]]:
        identifier = _cv_id(external_id, MetadataEntityKind.SERIES)
        page_number(page)

        async def read() -> MetadataPage[ProviderIssueRead]:
            await self._available()
            rows, total = await self.reader.issue_page(int(identifier), page=page)
            return normalize.issue_page(MetadataSource.COMICVINE_LOCAL, rows, total, page)

        return await _fetch(lambda: _catalog_read(read))


def _api(runtime: SourceRuntime) -> ComicVineApiSource:
    if runtime.credential is None or not runtime.credential.get_secret_value():
        raise MetadataSourceError(SourceStatus.UNCONFIGURED)
    return ComicVineApiSource(
        ComicVineProvider(
            runtime.credential.get_secret_value(), rate_limit=get_settings().comicvine_rate_limit
        )
    )


def comicvine_sources() -> dict[MetadataSource, SourceRegistration]:
    capabilities = {
        SourceCapability.SERIES_SEARCH,
        SourceCapability.SERIES_DETAILS,
        SourceCapability.ISSUE_LIST,
        SourceCapability.RECENT_ISSUES,
        SourceCapability.ISSUE_DETAILS,
    }
    return {
        MetadataSource.COMICVINE_API: SourceRegistration(
            frozenset(
                capabilities
                | {
                    SourceCapability.STORY_ARC_SEARCH,
                    SourceCapability.STORY_ARC_DETAILS,
                    SourceCapability.STORY_ARC_ISSUES,
                }
            ),
            _api,
        ),
        MetadataSource.COMICVINE_LOCAL: SourceRegistration(
            frozenset(capabilities | {SourceCapability.OFFLINE}),
            lambda runtime: ComicVineLocalSource(get_catalog_reader()),
        ),
    }


def _metron(runtime: SourceRuntime) -> MetronSource:
    if runtime.credential is None:
        raise MetadataSourceError(SourceStatus.UNCONFIGURED)
    return MetronSource(runtime.credential)


def _gcd_api(runtime: SourceRuntime) -> GcdApiV2Source:
    if runtime.credential is None:
        raise MetadataSourceError(SourceStatus.UNCONFIGURED)
    return GcdApiV2Source(runtime.credential)


def metadata_sources() -> dict[MetadataSource, SourceRegistration]:
    return {
        **comicvine_sources(),
        MetadataSource.GCD_API_V2: SourceRegistration(
            frozenset(
                {
                    SourceCapability.SERIES_SEARCH,
                    SourceCapability.SERIES_DETAILS,
                    SourceCapability.ISSUE_LIST,
                    SourceCapability.ISSUE_DETAILS,
                }
            ),
            _gcd_api,
        ),
        MetadataSource.GCD_LOCAL: SourceRegistration(
            frozenset(
                {
                    SourceCapability.SERIES_SEARCH,
                    SourceCapability.SERIES_DETAILS,
                    SourceCapability.ISSUE_LIST,
                    SourceCapability.ISSUE_DETAILS,
                    SourceCapability.OFFLINE,
                    SourceCapability.STORY_ARC_SEARCH,
                    SourceCapability.STORY_ARC_DETAILS,
                    SourceCapability.STORY_ARC_ISSUES,
                }
            ),
            lambda runtime: GcdLocalSource(runtime.gcd_snapshot),
        ),
        MetadataSource.METRON_API: SourceRegistration(
            frozenset(
                {
                    SourceCapability.SERIES_SEARCH,
                    SourceCapability.SERIES_DETAILS,
                    SourceCapability.ISSUE_LIST,
                    SourceCapability.RECENT_ISSUES,
                    SourceCapability.ISSUE_DETAILS,
                    SourceCapability.STORY_ARC_SEARCH,
                    SourceCapability.STORY_ARC_DETAILS,
                    SourceCapability.STORY_ARC_ISSUES,
                    SourceCapability.CROSS_IDENTITIES,
                    SourceCapability.CONDITIONAL_REFRESH,
                    SourceCapability.COVER_REFERENCE,
                }
            ),
            _metron,
        ),
    }
