"""Bounded, read-only Metron HTTP adapter; no implicit identity attachment."""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit

import httpx

from pullbox import __version__
from pullbox.core.metadata_identity import MetadataEntityKind, MetadataSource
from pullbox.core.provider_cooldown import ProviderCooldown, provider_cooldown, retry_after_seconds
from pullbox.providers.metadata import metron_normalization as normalize
from pullbox.schemas.metadata_sources import (
    MetadataFetch,
    MetadataPage,
    ProviderIssueRead,
    ProviderSeriesRead,
    ProviderStoryArcRead,
    RecentIssueWindow,
    SourceStatus,
)
from pullbox.services.metadata_discovery import MetadataSourceError, SourcePage
from pullbox.services.metadata_source_reads import issue_checkpoint, validate_recent_issue_window

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from pydantic import SecretStr

    from pullbox.schemas.metadata_sources import SeriesDiscoveryQuery

_MAX_BYTES = 2 * 1024 * 1024
_PAGE_SIZE = 100
_REQUEST_TIMEOUT = 8.0
_QUEUE_TIMEOUT = 3.0


@dataclass(frozen=True)
class _Response:
    status: SourceStatus
    payload: object = None
    validator: str | None = None


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON field")
    return result


def _json_constant(value: str) -> None:
    raise ValueError("Nonstandard JSON constant")


def _validator(value: str | None) -> str | None:
    if value is not None:
        if len(value) > 128 or "\r" in value or "\n" in value:
            raise ValueError("Invalid refresh validator")
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            raise ValueError("Refresh validator must include a timezone")
    return value


def _page_number(page: int) -> None:
    if type(page) is not int or not 1 <= page <= 10000:
        raise ValueError("Metron page must be between one and ten thousand")


def _metadata_page[T](rows: list[T], total: int, next_page: int | None) -> MetadataPage[T]:
    truncated = next_page is not None and next_page > 10000
    return MetadataPage(
        results=rows, total=total, next_page=None if truncated else next_page, truncated=truncated
    )


def _envelope(
    payload: object, path: str, params: dict[str, str]
) -> tuple[list[object], int, int | None]:
    row = normalize.object_row(payload)
    total, rows = row.get("count"), row.get("results")
    if type(total) is not int or not 0 <= total <= 1_000_000_000 or not isinstance(rows, list):
        raise ValueError("Invalid pagination envelope")
    page = int(params["page"])
    remaining = max(0, total - (page - 1) * _PAGE_SIZE)
    if len(rows) != min(_PAGE_SIZE, remaining):
        raise ValueError("Incomplete source page")
    next_page = page + 1 if remaining > _PAGE_SIZE else None
    link = row.get("next")
    if next_page is None:
        if link is not None:
            raise ValueError("Unexpected continuation")
    else:
        if not isinstance(link, str) or len(link) > 4096:
            raise ValueError("Missing continuation")
        url = urlsplit(link)
        query = parse_qsl(url.query, keep_blank_values=True, max_num_fields=10)
        if (
            url.scheme != "https"
            or url.hostname != "metron.cloud"
            or url.port not in {None, 443}
            or url.username is not None
            or url.password is not None
            or url.fragment
            or url.path != f"/api/{path}"
            or len(query) != len(dict(query))
            or dict(query) != {**params, "page": str(next_page)}
            or any(char.isspace() or ord(char) < 32 for char in link)
        ):
            raise ValueError("Invalid continuation")
    return rows, total, next_page


class MetronSource:
    def __init__(
        self,
        token: SecretStr,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        cooldown: ProviderCooldown | None = None,
        minimum_interval: float = 3.0,
    ) -> None:
        credential = token.get_secret_value()
        if not credential:
            raise MetadataSourceError(SourceStatus.UNCONFIGURED)
        if (
            len(credential) > 4096
            or not credential.isascii()
            or credential.startswith("enc:")
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in credential)
        ):
            raise MetadataSourceError(SourceStatus.INVALID_CONFIG)
        if not math.isfinite(minimum_interval) or not 0 <= minimum_interval <= 3:
            raise ValueError("Invalid request spacing")
        self.cooldown = cooldown or provider_cooldown("metron", credential)
        self.minimum_interval = minimum_interval
        self.client = httpx.AsyncClient(
            base_url="https://metron.cloud/api/",
            headers={
                "Authorization": f"Bearer {credential}",
                "User-Agent": f"Pullbox/{__version__}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(_REQUEST_TIMEOUT, connect=4),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            follow_redirects=False,
            transport=transport,
        )

    def _rate_headers(self, headers: httpx.Headers) -> None:
        for scope in ("Burst", "Sustained"):
            try:
                if headers.get(f"X-RateLimit-{scope}-Remaining") == "0":
                    reset = float(headers[f"X-RateLimit-{scope}-Reset"]) - time.time()
                    if math.isfinite(reset) and reset > 0:
                        self.cooldown.defer(min(reset, 7 * 86400))
            except (ValueError, KeyError):
                pass

    async def _exchange(
        self, path: str, params: dict[str, str], validator: str | None
    ) -> _Response:
        headers = {"If-Modified-Since": validator} if validator else {}
        async with self.client.stream("GET", path, params=params, headers=headers) as response:
            self._rate_headers(response.headers)
            status = response.status_code
            if status in {401, 403}:
                raise MetadataSourceError(SourceStatus.AUTHENTICATION_FAILED)
            if status == 429:
                self.cooldown.defer(
                    retry_after_seconds(response.headers.get("Retry-After"), default=60)
                )
                raise MetadataSourceError(
                    SourceStatus.RATE_LIMITED, self.cooldown.remaining_seconds
                )
            if status in {500, 502, 503, 504}:
                if "Retry-After" in response.headers:
                    self.cooldown.defer(
                        retry_after_seconds(response.headers["Retry-After"], default=1)
                    )
                raise MetadataSourceError(
                    SourceStatus.UNAVAILABLE, self.cooldown.remaining_seconds or None
                )
            if status == 404:
                return _Response(SourceStatus.NOT_FOUND)
            if status == 304 and validator:
                return _Response(SourceStatus.NOT_MODIFIED, validator=validator)
            if (
                status != 200
                or response.headers.get("content-type", "").split(";")[0] != "application/json"
            ):
                raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > _MAX_BYTES:
                    raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE)
                body.extend(chunk)
            try:
                payload = json.loads(
                    body, object_pairs_hook=_json_object, parse_constant=_json_constant
                )
                modified = _validator(response.headers.get("Last-Modified"))
            except (ValueError, TypeError, OverflowError, RecursionError):
                raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE) from None
            return _Response(SourceStatus.OK, payload, modified)

    async def _get(
        self, path: str, params: dict[str, str] | None = None, validator: str | None = None
    ) -> _Response:
        _validator(validator)
        try:
            for attempt in range(2):
                try:
                    if self.cooldown.remaining_seconds:
                        raise MetadataSourceError(
                            SourceStatus.RATE_LIMITED, self.cooldown.remaining_seconds
                        )
                    try:
                        async with asyncio.timeout(_QUEUE_TIMEOUT):
                            await self.cooldown.request_lock.acquire()
                    except TimeoutError:
                        raise MetadataSourceError(
                            SourceStatus.RATE_LIMITED, 3, account_failure=False
                        ) from None
                    try:
                        if self.cooldown.remaining_seconds:
                            raise MetadataSourceError(
                                SourceStatus.RATE_LIMITED, self.cooldown.remaining_seconds
                            )
                        delay = self.minimum_interval - (
                            time.monotonic() - self.cooldown.last_request_time
                        )
                        if delay > 0:
                            await asyncio.sleep(delay)
                        self.cooldown.last_request_time = time.monotonic()
                        async with asyncio.timeout(_REQUEST_TIMEOUT):
                            return await self._exchange(path, params or {}, validator)
                    finally:
                        self.cooldown.request_lock.release()
                except MetadataSourceError as exc:
                    if (
                        exc.status is not SourceStatus.UNAVAILABLE
                        or exc.retry_after_seconds
                        or attempt
                    ):
                        raise
                except httpx.TimeoutException:
                    raise MetadataSourceError(SourceStatus.TIMEOUT) from None
                except (httpx.NetworkError, httpx.RemoteProtocolError):
                    if attempt:
                        raise MetadataSourceError(SourceStatus.UNAVAILABLE) from None
                await asyncio.sleep(0.1)
        except TimeoutError:
            raise MetadataSourceError(SourceStatus.TIMEOUT) from None
        except httpx.HTTPError:
            raise MetadataSourceError(SourceStatus.UNAVAILABLE) from None
        raise MetadataSourceError(SourceStatus.UNAVAILABLE)

    async def search(self, query: SeriesDiscoveryQuery, offset: int) -> SourcePage:
        limit = query.limit_per_source
        if type(offset) is not int or not 0 <= offset <= 10000 or offset % limit:
            raise ValueError("Invalid source offset")
        params = {"name": query.query, "page": str(offset // _PAGE_SIZE + 1)}
        if query.year is not None:
            params["year_began"] = str(query.year)
        results = []
        rejected = 0
        consumed = 0
        seen = set()
        total = None
        try:
            for _ in range(2):
                response = await self._get("series/", params)
                if response.status is not SourceStatus.OK:
                    raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE)
                rows, current_total, next_page = _envelope(response.payload, "series/", params)
                if total is not None and total != current_total:
                    raise ValueError("Source changed between pages")
                total = current_total
                start = offset % _PAGE_SIZE if not consumed else 0
                selected = rows[start : start + limit - consumed]
                consumed += len(selected)
                for row in selected:
                    try:
                        result = normalize.series(row)
                        if result.external_id in seen:
                            raise ValueError("Repeated source identity")
                        seen.add(result.external_id)
                        results.append(result)
                    except (ValueError, TypeError):
                        rejected += 1
                if consumed >= limit or next_page is None:
                    break
                params["page"] = str(next_page)
            more = total is not None and offset + consumed < total
            next_offset = offset + limit if more and offset + limit <= 10000 else None
            return SourcePage(results, total, next_offset, rejected, more and next_offset is None)
        except (ValueError, TypeError):
            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE) from None

    async def check(self) -> None:
        from pullbox.schemas.metadata_sources import SeriesDiscoveryQuery

        page = await self.search(SeriesDiscoveryQuery(query="test", limit_per_source=1), 0)
        if page.rejected_results:
            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE)

    async def close(self) -> None:
        await self.client.aclose()

    async def _detail[T: (ProviderSeriesRead, ProviderIssueRead, ProviderStoryArcRead)](
        self,
        path: str,
        identifier: str,
        normalize_row: Callable[[object], T],
        validator: str | None,
    ) -> MetadataFetch[T]:
        identifier = normalize.external_id(identifier)
        response = await self._get(f"{path}/{identifier}/", validator=validator)
        if response.status is not SourceStatus.OK:
            return MetadataFetch(status=response.status, validator=response.validator)
        try:
            row = normalize_row(response.payload)
            if row.external_id != identifier:
                raise ValueError("Different detail identity")
            return MetadataFetch(status=SourceStatus.OK, data=row, validator=response.validator)
        except (ValueError, TypeError):
            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE) from None

    async def recent_issues(
        self, external_id: str, *, since: datetime
    ) -> MetadataFetch[RecentIssueWindow]:
        identifier = normalize.external_id(external_id)
        checkpoint = issue_checkpoint(since)
        params = {"series_id": identifier, "modified_gt": checkpoint.isoformat(), "page": "1"}
        response = await self._get("issue/", params)
        if response.status is not SourceStatus.OK:
            return MetadataFetch(status=response.status)
        try:
            rows, total, next_page = _envelope(response.payload, "issue/", params)
            result = RecentIssueWindow(
                results=[normalize.issue(row) for row in rows],
                matched_total=total,
                scope="modified_since",
                since=checkpoint,
                truncated=next_page is not None,
            )
            validate_recent_issue_window(result, MetadataSource.METRON_API, identifier, checkpoint)
            return MetadataFetch(status=SourceStatus.OK, data=result)
        except (ValueError, TypeError):
            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE) from None

    async def series(
        self, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderSeriesRead]:
        return await self._detail(
            "series", external_id, lambda row: normalize.series(row, detail=True), validator
        )

    async def issue(
        self, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderIssueRead]:
        return await self._detail("issue", external_id, normalize.issue, validator)

    async def story_arc(
        self, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderStoryArcRead]:
        return await self._detail("arc", external_id, normalize.story_arc, validator)

    async def issues(
        self,
        external_id: str,
        *,
        kind: MetadataEntityKind = MetadataEntityKind.SERIES,
        page: int = 1,
        validator: str | None = None,
    ) -> MetadataFetch[MetadataPage[ProviderIssueRead]]:
        if not isinstance(kind, MetadataEntityKind) or kind not in {
            MetadataEntityKind.SERIES,
            MetadataEntityKind.STORY_ARC,
        }:
            raise ValueError("Unsupported issue-list parent")
        identifier = normalize.external_id(external_id)
        _page_number(page)
        path = (
            f"{'series' if kind is MetadataEntityKind.SERIES else 'arc'}/{identifier}/issue_list/"
        )
        params = {"page": str(page)}
        response = await self._get(path, params, validator)
        if response.status is not SourceStatus.OK:
            return MetadataFetch(status=response.status, validator=response.validator)
        try:
            rows, total, next_page = _envelope(response.payload, path, params)
            issues = [normalize.issue(row) for row in rows]
            if len({issue.external_id for issue in issues}) != len(issues) or (
                kind is MetadataEntityKind.SERIES
                and any(issue.series_external_id != identifier for issue in issues)
            ):
                raise ValueError("Inconsistent issue membership")
            return MetadataFetch(
                status=SourceStatus.OK,
                validator=response.validator,
                data=_metadata_page(issues, total, next_page),
            )
        except (ValueError, TypeError):
            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE) from None

    async def story_arcs(self, query: str, *, page: int = 1) -> MetadataPage[ProviderStoryArcRead]:
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError("Expected a bounded arc query")
        _page_number(page)
        if page > 100:
            raise ValueError("Story arc search is bounded to 100 source pages")
        params = {"name": query.strip(), "page": str(page)}
        response = await self._get("arc/", params)
        if response.status is not SourceStatus.OK:
            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE)
        try:
            rows, total, next_page = _envelope(response.payload, "arc/", params)
            results = [normalize.story_arc(row) for row in rows]
            if len({row.external_id for row in results}) != len(results):
                raise ValueError("Repeated source identity")
            result = _metadata_page(results, total, next_page)
            if page == 100 and result.next_page is not None:
                result.next_page, result.truncated = None, True
            return result
        except (ValueError, TypeError):
            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE) from None

    async def story_arc_issues(
        self, external_id: str, *, page: int = 1, validator: str | None = None
    ) -> MetadataFetch[MetadataPage[ProviderIssueRead]]:
        _page_number(page)
        if page > 50:
            raise ValueError("Story arc membership is bounded to 5000 issues")
        return await self.issues(
            external_id, kind=MetadataEntityKind.STORY_ARC, page=page, validator=validator
        )
