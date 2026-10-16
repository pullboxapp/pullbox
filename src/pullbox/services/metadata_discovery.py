"""Source-aware metadata discovery; identity attachment is a separate operation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog

from pullbox.core.metadata_identity import MetadataSource
from pullbox.schemas.metadata_sources import (
    MetadataDomain,
    MetadataFetch,
    MetadataPage,
    ProviderIssueRead,
    ProviderSeriesRead,
    ProviderStoryArcRead,
    RecentIssueWindow,
    SeriesDiscoveryQuery,
    SeriesDiscoveryRead,
    SourceCapability,
    SourceDescriptor,
    SourceOutcome,
    SourcePolicyRead,
    SourceStatus,
    StoryArcDiscoveryQuery,
    StoryArcDiscoveryRead,
    StoryArcSourceOutcome,
)
from pullbox.services.metadata_account_admission import AccountAttempt, account_request

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime

    from pullbox.services.metadata_read_cache import MetadataReadCache
    from pullbox.services.metadata_sources import SourceRuntime

logger = structlog.get_logger(__name__)


def describe_source_policies(
    policies: Sequence[SourcePolicyRead], *, gcd_api_enabled: bool
) -> list[SourceDescriptor]:
    """Expose capability/configuration snapshots without constructing clients."""
    from pullbox.providers.metadata.sources import metadata_sources

    registrations = metadata_sources()
    result = []
    for policy in policies:
        registration = registrations.get(policy.source)
        availability = policy.configuration_status
        if policy.source is MetadataSource.GCD_API_V2 and not gcd_api_enabled:
            availability = SourceStatus.FEATURE_DISABLED
        elif not policy.enabled:
            availability = availability or SourceStatus.DISABLED
        elif registration is None:
            availability = SourceStatus.NOT_IMPLEMENTED
        elif (
            policy.source
            in {MetadataSource.COMICVINE_API, MetadataSource.METRON_API, MetadataSource.GCD_API_V2}
            and not policy.credential_configured
        ):
            availability = SourceStatus.UNCONFIGURED
        result.append(
            SourceDescriptor(
                **policy.model_dump(),
                capabilities=sorted(registration.capabilities) if registration else [],
                availability=availability,
            )
        )
    return result


@dataclass(frozen=True)
class SourcePage:
    results: list[ProviderSeriesRead]
    total: int | None = None
    next_offset: int | None = None
    rejected_results: int = 0
    truncated: bool = False


class MetadataSourceAdapter(Protocol):
    async def search(self, query: SeriesDiscoveryQuery, offset: int) -> SourcePage: ...
    async def check(self) -> None: ...
    async def close(self) -> None: ...


@dataclass
class _SourceHandle:
    adapter: MetadataSourceAdapter | None = None


class MetadataSourceError(Exception):
    def __init__(self, status: SourceStatus, retry_after_seconds: int | None = None) -> None:
        self.status = status
        self.retry_after_seconds = retry_after_seconds
        super().__init__(status.value)


@dataclass(frozen=True)
class SourceRegistration:
    capabilities: frozenset[SourceCapability]
    factory: Callable[[SourceRuntime], MetadataSourceAdapter]


class MetadataSourceRegistry:
    def __init__(
        self,
        runtime: Sequence[SourceRuntime],
        *,
        factories: Mapping[MetadataSource, SourceRegistration] | None = None,
        gcd_api_enabled: bool = False,
        per_source_timeout: float = 8,
        total_timeout: float = 15,
        concurrency: int = 3,
        read_cache: MetadataReadCache | None = None,
        revalidate_reads: bool = False,
    ) -> None:
        if factories is None:
            from pullbox.providers.metadata.sources import metadata_sources

            factories = metadata_sources()
        self.runtime = {item.policy.source: item for item in runtime}
        self.factories = factories
        self.gcd_api_enabled = gcd_api_enabled
        if not 0 < per_source_timeout <= 30 or not 0 < total_timeout <= 60:
            raise ValueError("Metadata discovery deadlines must be bounded")
        if not 1 <= concurrency <= 5:
            raise ValueError("Metadata discovery concurrency must be between one and five")
        self.per_source_timeout = per_source_timeout
        self.total_timeout = total_timeout
        self.concurrency = concurrency
        self.read_slots = asyncio.Semaphore(concurrency)
        self.read_cache = read_cache
        self.revalidate_reads = revalidate_reads

    def _unavailable(
        self,
        source: MetadataSource,
        *,
        search: bool = False,
        capability: SourceCapability | None = None,
    ) -> SourceStatus | None:
        runtime = self.runtime.get(source)
        if source is MetadataSource.GCD_API_V2 and not self.gcd_api_enabled:
            return SourceStatus.FEATURE_DISABLED
        if runtime is None:
            return SourceStatus.UNCONFIGURED
        if runtime.unavailable is not None:
            return runtime.unavailable
        if not runtime.policy.enabled:
            return SourceStatus.DISABLED
        registration = self.factories.get(source)
        if registration is None:
            return SourceStatus.NOT_IMPLEMENTED
        if search and SourceCapability.SERIES_SEARCH not in registration.capabilities:
            return SourceStatus.UNSUPPORTED
        if capability is not None and capability not in registration.capabilities:
            return SourceStatus.UNSUPPORTED
        return None

    async def _run(
        self,
        source: MetadataSource,
        query: SeriesDiscoveryQuery | None,
        semaphore: asyncio.Semaphore,
        deadline: float,
        handle: _SourceHandle | None = None,
        *,
        retry_authentication: bool = False,
    ) -> tuple[SourcePage, SourceOutcome]:
        unavailable = self._unavailable(source, search=query is not None)
        if unavailable is not None:
            return SourcePage([]), SourceOutcome(source=source, status=unavailable)
        if asyncio.get_running_loop().time() >= deadline:
            return SourcePage([]), SourceOutcome(source=source, status=SourceStatus.TIMEOUT)
        try:
            async with account_request(
                self.runtime[source],
                slots=semaphore,
                deadline=deadline,
                retry_authentication=retry_authentication,
            ) as attempt:
                if attempt.blocked is not None:
                    return SourcePage([]), attempt.blocked
                page, outcome = await self._run_admitted(source, query, deadline, handle, attempt)
                attempt.outcome = outcome
                return page, outcome
        except TimeoutError:
            return SourcePage([]), SourceOutcome(source=source, status=SourceStatus.TIMEOUT)

    async def _run_admitted(
        self,
        source: MetadataSource,
        query: SeriesDiscoveryQuery | None,
        deadline: float,
        handle: _SourceHandle | None,
        attempt: AccountAttempt,
    ) -> tuple[SourcePage, SourceOutcome]:
        owns_handle = handle is None
        handle = handle or _SourceHandle()
        page = SourcePage([])
        try:
            async with asyncio.timeout_at(deadline):
                async with asyncio.timeout(self.per_source_timeout):
                    if handle.adapter is None:
                        handle.adapter = self.factories[source].factory(self.runtime[source])
                    adapter = handle.adapter
                    attempt.started = True
                    if query is None:
                        await adapter.check()
                    else:
                        page = await adapter.search(query, query.offsets.get(source, 0))
                    if any(
                        item.source != source
                        or item.identity_namespace != source.identity_namespace
                        for item in page.results
                    ):
                        raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE)
                    if query is not None and len(page.results) > query.limit_per_source:
                        raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE)
                    status = (
                        SourceStatus.INCOMPATIBLE_RESPONSE
                        if page.rejected_results
                        else SourceStatus.OK
                        if page.results or query is None
                        else SourceStatus.EMPTY
                    )
                    outcome = SourceOutcome(
                        source=source,
                        status=status,
                        total=page.total,
                        next_offset=page.next_offset,
                        rejected_results=page.rejected_results,
                        truncated=page.truncated,
                    )
        except TimeoutError:
            outcome = SourceOutcome(source=source, status=SourceStatus.TIMEOUT)
            page = SourcePage([])
        except MetadataSourceError as exc:
            outcome = SourceOutcome(
                source=source, status=exc.status, retry_after_seconds=exc.retry_after_seconds
            )
            page = SourcePage([])
        except Exception:
            # A provider fault must not leak credentials or hide other source results.
            logger.warning("metadata_source_operation_failed", source=source.value)
            outcome = SourceOutcome(source=source, status=SourceStatus.UNAVAILABLE)
            page = SourcePage([])
        finally:
            if owns_handle:
                await self._close_source(source, handle)
        return page, outcome

    @staticmethod
    async def _close_source(source: MetadataSource, handle: _SourceHandle) -> None:
        if handle.adapter is not None:
            try:
                async with asyncio.timeout(2):
                    await handle.adapter.close()
            except Exception:
                logger.warning("metadata_source_close_failed", source=source.value)

    def _ordered_sources(
        self,
        query: SeriesDiscoveryQuery | StoryArcDiscoveryQuery,
        *,
        domain: MetadataDomain = MetadataDomain.CORE,
    ) -> list[MetadataSource]:
        return self.ordered_sources(sources=query.sources, domain=domain)

    def ordered_sources(
        self,
        *,
        sources: Sequence[MetadataSource] | None = None,
        domain: MetadataDomain = MetadataDomain.CORE,
    ) -> list[MetadataSource]:
        """Use the same configured authority for discovery and exact-ID refresh."""
        return sorted(
            sources if sources is not None else self.runtime,
            key=lambda source: (
                self.runtime[source].policy.domain_priorities.get(
                    domain, self.runtime[source].policy.priority
                )
                if source in self.runtime
                else 1001,
                source.value,
            ),
        )

    def source_availability(
        self, source: MetadataSource, capability: SourceCapability
    ) -> SourceStatus | None:
        """Inspect configuration and capabilities without constructing a client."""
        return self._unavailable(source, capability=capability)

    @staticmethod
    def _group_pages(pages: Sequence[tuple[SourcePage, SourceOutcome]]) -> SeriesDiscoveryRead:
        grouped = {}
        for page, _ in pages:
            for item in page.results:
                key = (item.identity_namespace, item.external_id)
                if key not in grouped:
                    grouped[key] = item.model_copy(deep=True)
                elif (
                    item.source != grouped[key].source and item.source not in grouped[key].also_from
                ):
                    grouped[key].also_from.append(item.source)
        return SeriesDiscoveryRead(
            results=list(grouped.values()), sources=[outcome for _, outcome in pages]
        )

    async def discover(
        self,
        query: SeriesDiscoveryQuery,
        *,
        satisfied_by: Callable[[SourcePage], bool] | None = None,
    ) -> SeriesDiscoveryRead:
        """Cascade only stops when the caller proves its requirements are met."""
        sources = self._ordered_sources(query)
        semaphore = asyncio.Semaphore(self.concurrency)
        deadline = asyncio.get_running_loop().time() + self.total_timeout
        pages = []
        if query.mode == "automatic":
            satisfied = False
            for source in sources:
                if satisfied:
                    status = self._unavailable(source, search=True) or SourceStatus.NOT_QUERIED
                    pages.append((SourcePage([]), SourceOutcome(source=source, status=status)))
                else:
                    page, outcome = await self._run(source, query, semaphore, deadline)
                    pages.append((page, outcome))
                    satisfied = (
                        bool(page.results) and satisfied_by is not None and satisfied_by(page)
                    )
        else:
            tasks = [
                asyncio.create_task(self._run(source, query, semaphore, deadline))
                for source in sources
            ]
            try:
                pages = list(await asyncio.gather(*tasks))
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        return self._group_pages(pages)

    async def check(
        self, source: MetadataSource, *, retry_authentication: bool = False
    ) -> SourceOutcome:
        _, outcome = await self._run(
            source,
            None,
            asyncio.Semaphore(1),
            asyncio.get_running_loop().time() + self.total_timeout,
            retry_authentication=retry_authentication,
        )
        return outcome

    async def discover_all(
        self, query: SeriesDiscoveryQuery, *, max_results_per_source: int = 1000
    ) -> SeriesDiscoveryRead:
        """Collect a bounded candidate set for interactive sorting and pagination."""
        if query.mode != "interactive" or query.offsets:
            raise ValueError("Full candidate collection starts at the first interactive page")
        if (
            type(max_results_per_source) is not int
            or not query.limit_per_source <= max_results_per_source <= 1000
            or max_results_per_source % query.limit_per_source
        ):
            raise ValueError("Candidate limits must be complete source pages, at most 1000 rows")
        semaphore = asyncio.Semaphore(self.concurrency)
        deadline = asyncio.get_running_loop().time() + self.total_timeout

        async def collect(source: MetadataSource) -> tuple[SourcePage, SourceOutcome]:
            handle = _SourceHandle()
            try:
                return await self._collect_source(
                    source, query, semaphore, deadline, max_results_per_source, handle
                )
            finally:
                await self._close_source(source, handle)

        tasks = [asyncio.create_task(collect(source)) for source in self._ordered_sources(query)]
        try:
            pages = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        # Group only after collecting each source: a preferred source's later
        # page must still win over the same identity on another source's first.
        return self._group_pages(pages)

    async def _collect_source(
        self,
        source: MetadataSource,
        query: SeriesDiscoveryQuery,
        semaphore: asyncio.Semaphore,
        deadline: float,
        max_results: int,
        handle: _SourceHandle,
    ) -> tuple[SourcePage, SourceOutcome]:
        rows: list[ProviderSeriesRead] = []
        offset = rejected = 0
        total = None
        while True:
            request = query.model_copy(
                update={"sources": [source], "offsets": {source: offset}, "search_mode": "full"}
            )
            page, outcome = await self._run(source, request, semaphore, deadline, handle)
            rows.extend(page.results)
            total = page.total if page.total is not None else total
            rejected += page.rejected_results
            outcome.total, outcome.rejected_results = total, rejected
            readable_page = outcome.status in {SourceStatus.OK, SourceStatus.EMPTY} or (
                outcome.status is SourceStatus.INCOMPATIBLE_RESPONSE and page.rejected_results > 0
            )
            if not readable_page:
                if offset:
                    outcome.next_offset, outcome.truncated = offset, True
                break
            if rejected:
                outcome.status = SourceStatus.INCOMPATIBLE_RESPONSE
            elif rows:
                outcome.status = SourceStatus.OK
            cursor = page.next_offset
            if cursor is None:
                break
            if type(cursor) is not int or cursor != offset + query.limit_per_source:
                outcome.status = SourceStatus.INCOMPATIBLE_RESPONSE
                outcome.next_offset, outcome.truncated = None, True
                break
            if cursor >= max_results or page.truncated:
                outcome.truncated = True
                break
            offset = cursor
        return SourcePage(rows), outcome

    async def series(
        self, source: MetadataSource, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderSeriesRead]:
        from pullbox.services.metadata_source_reads import read_series

        return await read_series(self, source, external_id, validator=validator)

    async def issue(
        self, source: MetadataSource, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderIssueRead]:
        from pullbox.services.metadata_source_reads import read_issue

        return await read_issue(self, source, external_id, validator=validator)

    async def issues(
        self,
        source: MetadataSource,
        external_id: str,
        *,
        page: int = 1,
        validator: str | None = None,
    ) -> MetadataFetch[MetadataPage[ProviderIssueRead]]:
        from pullbox.services.metadata_source_reads import read_issues

        return await read_issues(self, source, external_id, page=page, validator=validator)

    async def recent_issues(
        self, source: MetadataSource, external_id: str, *, since: datetime
    ) -> MetadataFetch[RecentIssueWindow]:
        from pullbox.services.metadata_source_reads import read_recent_issues

        return await read_recent_issues(self, source, external_id, since=since)

    async def story_arc(
        self, source: MetadataSource, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderStoryArcRead]:
        from pullbox.services.metadata_source_reads import read_story_arc

        return await read_story_arc(self, source, external_id, validator=validator)

    async def story_arc_issues(
        self,
        source: MetadataSource,
        external_id: str,
        *,
        page: int = 1,
        validator: str | None = None,
    ) -> MetadataFetch[MetadataPage[ProviderIssueRead]]:
        from pullbox.services.metadata_source_reads import read_story_arc_issues

        return await read_story_arc_issues(
            self, source, external_id, page=page, validator=validator
        )

    async def discover_arcs(
        self,
        query: StoryArcDiscoveryQuery,
        *,
        satisfied_by: Callable[[MetadataPage[ProviderStoryArcRead]], bool] | None = None,
    ) -> StoryArcDiscoveryRead:
        from pullbox.services.metadata_source_reads import read_story_arcs

        sources = self._ordered_sources(query, domain=MetadataDomain.STORY_ARCS)
        deadline = asyncio.get_running_loop().time() + self.total_timeout

        async def read(source: MetadataSource) -> MetadataFetch[MetadataPage[ProviderStoryArcRead]]:
            return await read_story_arcs(
                self, source, query.query, page=query.pages.get(source, 1), deadline=deadline
            )

        pages = []
        if query.mode == "automatic":
            satisfied = False
            for source in sources:
                if satisfied:
                    result: MetadataFetch[MetadataPage[ProviderStoryArcRead]] = MetadataFetch(
                        status=self._unavailable(
                            source, capability=SourceCapability.STORY_ARC_SEARCH
                        )
                        or SourceStatus.NOT_QUERIED
                    )
                else:
                    result = await read(source)
                    satisfied = bool(
                        result.data
                        and result.data.results
                        and satisfied_by
                        and satisfied_by(result.data)
                    )
                pages.append(result)
        else:
            tasks = [asyncio.create_task(read(source)) for source in sources]
            try:
                pages = list(await asyncio.gather(*tasks))
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        grouped = {}
        outcomes = []
        for source, result in zip(sources, pages, strict=True):
            page = result.data
            outcomes.append(
                StoryArcSourceOutcome(
                    source=source,
                    status=SourceStatus.EMPTY
                    if page is not None and not page.results
                    else result.status,
                    total=page.total if page is not None else None,
                    next_page=page.next_page if page is not None else None,
                    truncated=page.truncated if page is not None else False,
                    retry_after_seconds=result.retry_after_seconds,
                )
            )
            if page is not None:
                for row in page.results:
                    key = (row.identity_namespace, row.external_id)
                    if key not in grouped:
                        grouped[key] = row.model_copy(deep=True)
                    elif (
                        row.source != grouped[key].source
                        and row.source not in grouped[key].also_from
                    ):
                        grouped[key].also_from.append(row.source)
        return StoryArcDiscoveryRead(results=list(grouped.values()), sources=outcomes)
