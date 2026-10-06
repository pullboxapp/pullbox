"""Feature-gated GCD API v2 reads; registration owns activation."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlsplit

import httpx
from pydantic import SecretStr

from pullbox import __version__
from pullbox.core.provider_cooldown import ProviderCooldown, provider_cooldown, retry_after_seconds
from pullbox.providers.metadata import gcd_api_normalization as normalize
from pullbox.schemas.metadata_sources import (
    MetadataFetch,
    MetadataPage,
    ProviderIssueRead,
    ProviderSeriesRead,
    SourceStatus,
)
from pullbox.services.metadata_discovery import MetadataSourceError, SourcePage
from pullbox.services.metadata_source_reads import MAX_PAGE, PAGE_SIZE, page_number

if TYPE_CHECKING:
    from collections.abc import Callable

    from pullbox.schemas.metadata_sources import SeriesDiscoveryQuery

_BASE = "https://beta.comics.org/api/v2/"
_MAX_BYTES = 2 * 1024 * 1024


async def exchange_gcd_token(
    username: SecretStr,
    password: SecretStr,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    cooldown: ProviderCooldown | None = None,
) -> SecretStr:
    """Exchange request-scoped credentials at the fixed GCD token endpoint."""
    hold = cooldown or provider_cooldown("gcd_api_v2", "token-exchange")
    credentials = {"username": username.get_secret_value(), "password": password.get_secret_value()}
    try:
        async with asyncio.timeout(8), hold.request_lock:
            if hold.remaining_seconds:
                raise MetadataSourceError(SourceStatus.RATE_LIMITED, hold.remaining_seconds)
            async with (
                httpx.AsyncClient(
                    base_url=_BASE,
                    headers={"User-Agent": f"Pullbox/{__version__}", "Accept": "application/json"},
                    timeout=httpx.Timeout(8, connect=4),
                    follow_redirects=False,
                    transport=transport,
                ) as client,
                client.stream("POST", "auth/token/", json=credentials) as response,
            ):
                status = response.status_code
                if status in {400, 401, 403}:
                    raise MetadataSourceError(SourceStatus.AUTHENTICATION_FAILED)
                if status == 429 or status in {500, 502, 503, 504}:
                    if status == 429 or "Retry-After" in response.headers:
                        hold.defer(
                            retry_after_seconds(response.headers.get("Retry-After"), default=60)
                        )
                    raise MetadataSourceError(
                        SourceStatus.RATE_LIMITED if status == 429 else SourceStatus.UNAVAILABLE,
                        hold.remaining_seconds or None,
                    )
                if (
                    status != 200
                    or response.headers.get("content-type", "").split(";")[0] != "application/json"
                ):
                    raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE)
                body = bytearray()
                try:
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 8192:
                            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE)
                        body.extend(chunk)
                    payload = json.loads(
                        body, object_pairs_hook=_json_object, parse_constant=_json_constant
                    )
                    token = (
                        payload.get("token")
                        if isinstance(payload, dict) and set(payload) == {"token"}
                        else None
                    )
                    if (
                        not isinstance(token, str)
                        or not token
                        or len(token) > 4096
                        or not token.isascii()
                        or token.startswith("enc:")
                        or any(
                            char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token
                        )
                    ):
                        raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE)
                    return SecretStr(token)
                except (ValueError, TypeError, OverflowError, RecursionError):
                    raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE) from None
                finally:
                    body.clear()
    except (TimeoutError, httpx.TimeoutException):
        raise MetadataSourceError(SourceStatus.TIMEOUT) from None
    except httpx.HTTPError:
        raise MetadataSourceError(SourceStatus.UNAVAILABLE) from None
    finally:
        credentials.clear()


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON field")
    return result


def _json_constant(value: str) -> None:
    raise ValueError("Nonstandard JSON constant")


def _envelope(
    payload: object, path: str, params: dict[str, str]
) -> tuple[list[object], int, int | None]:
    row = normalize.object_row(payload)
    total, rows = row.get("count"), row.get("results")
    if type(total) is not int or not 0 <= total <= 1_000_000_000 or not isinstance(rows, list):
        raise ValueError("Invalid GCD pagination envelope")
    page, size = int(params["page"]), int(params["page_size"])
    remaining = max(0, total - (page - 1) * size)
    if len(rows) != min(size, remaining):
        raise ValueError("Incomplete GCD source page")
    next_page = page + 1 if remaining > size else None
    link = row.get("next")
    if next_page is None:
        if link is not None:
            raise ValueError("Unexpected GCD continuation")
    else:
        if not isinstance(link, str) or len(link) > 4096:
            raise ValueError("Missing GCD continuation")
        url = urlsplit(link)
        query = parse_qsl(url.query, keep_blank_values=True, max_num_fields=10)
        if (
            url.scheme != "https"
            or url.hostname != "beta.comics.org"
            or url.port not in {None, 443}
            or url.username is not None
            or url.password is not None
            or url.fragment
            or url.path != f"/api/v2/{path}"
            or len(query) != len(dict(query))
            or dict(query) != {**params, "page": str(next_page)}
            or any(char.isspace() or ord(char) < 32 for char in link)
        ):
            raise ValueError("Invalid GCD continuation")
    return rows, total, next_page


class GcdApiV2Source:
    def __init__(
        self,
        token: SecretStr,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        cooldown: ProviderCooldown | None = None,
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
        self.cooldown = cooldown or provider_cooldown("gcd_api_v2", credential)
        self._search_key: tuple[str, int | None] | None = None
        self._search_rows: list[object] = []
        self._search_total = 0
        self.client = httpx.AsyncClient(
            base_url=_BASE,
            headers={
                "Authorization": f"Token {credential}",
                "User-Agent": f"Pullbox/{__version__}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(8, connect=4),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            follow_redirects=False,
            transport=transport,
        )

    async def search(self, query: SeriesDiscoveryQuery, offset: int) -> SourcePage:
        limit = query.limit_per_source
        if type(offset) is not int or not 0 <= offset < 100 or offset % limit:
            raise ValueError("GCD beta discovery is bounded to one hundred candidates")
        params = {"name": query.query, "page_size": "100", "page": "1"}
        if query.year is not None:
            params["year_began"] = str(query.year)
        try:
            key = (query.query, query.year)
            if self._search_key != key:
                status, payload = await self._get("series/", params)
                if status is not SourceStatus.OK:
                    raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE)
                rows, total, _ = _envelope(payload, "series/", params)
                self._search_key, self._search_rows, self._search_total = key, rows, total
            selected = self._search_rows[offset : offset + limit]
            results = []
            rejected = 0
            seen = set()
            for row in selected:
                try:
                    result = normalize.series(row)
                    if result.external_id in seen:
                        raise ValueError("Repeated GCD search identity")
                    seen.add(result.external_id)
                    results.append(result)
                except (ValueError, TypeError):
                    rejected += 1
            consumed = offset + len(selected)
            next_offset = offset + limit if consumed < len(self._search_rows) else None
            return SourcePage(
                results,
                self._search_total,
                next_offset,
                rejected,
                self._search_total > len(self._search_rows) and next_offset is None,
            )
        except (ValueError, TypeError):
            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE) from None

    async def check(self) -> None:
        params = {"page_size": "1", "page": "1"}
        status, payload = await self._get("series/", params)
        if status is not SourceStatus.OK:
            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE)
        try:
            rows, _, _ = _envelope(payload, "series/", params)
            for row in rows:
                normalize.series(row)
        except (ValueError, TypeError):
            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE) from None

    async def _get(
        self, path: str, params: dict[str, str] | None = None
    ) -> tuple[SourceStatus, object]:
        try:
            async with asyncio.timeout(8), self.cooldown.request_lock:
                if self.cooldown.remaining_seconds:
                    raise MetadataSourceError(
                        SourceStatus.RATE_LIMITED, self.cooldown.remaining_seconds
                    )
                self.cooldown.last_request_time = time.monotonic()
                async with self.client.stream("GET", path, params=params or {}) as response:
                    status = response.status_code
                    if status in {401, 403}:
                        raise MetadataSourceError(SourceStatus.AUTHENTICATION_FAILED)
                    if status == 404:
                        return SourceStatus.NOT_FOUND, None
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
                            SourceStatus.UNAVAILABLE,
                            self.cooldown.remaining_seconds or None,
                        )
                    if (
                        status != 200
                        or response.headers.get("content-type", "").split(";")[0]
                        != "application/json"
                    ):
                        raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > _MAX_BYTES:
                            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE)
                        body.extend(chunk)
                    try:
                        return SourceStatus.OK, json.loads(
                            body, object_pairs_hook=_json_object, parse_constant=_json_constant
                        )
                    except (ValueError, TypeError, OverflowError, RecursionError):
                        raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE) from None
        except (TimeoutError, httpx.TimeoutException):
            raise MetadataSourceError(SourceStatus.TIMEOUT) from None
        except httpx.HTTPError:
            raise MetadataSourceError(SourceStatus.UNAVAILABLE) from None

    async def _detail[T: (ProviderSeriesRead, ProviderIssueRead)](
        self, path: str, external_id: str, normalize_row: Callable[[object], T]
    ) -> MetadataFetch[T]:
        identifier = normalize.external_id(external_id)
        status, payload = await self._get(f"{path}/{identifier}/")
        if status is not SourceStatus.OK:
            return MetadataFetch(status=status)
        try:
            row = normalize_row(payload)
            if row.external_id != identifier:
                raise ValueError("Different GCD detail identity")
            return MetadataFetch(status=SourceStatus.OK, data=row)
        except (ValueError, TypeError, ArithmeticError):
            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE) from None

    async def series(
        self, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderSeriesRead]:
        return await self._detail("series", external_id, normalize.series)

    async def issue(
        self, external_id: str, *, validator: str | None = None
    ) -> MetadataFetch[ProviderIssueRead]:
        return await self._detail("issues", external_id, normalize.issue)

    async def issues(
        self, external_id: str, *, page: int = 1, validator: str | None = None
    ) -> MetadataFetch[MetadataPage[ProviderIssueRead]]:
        identifier = normalize.external_id(external_id)
        page_number(page)
        params = {
            "series": identifier,
            "variant_of": "false",
            "page_size": str(PAGE_SIZE),
            "page": str(page),
        }
        status, payload = await self._get("issues/", params)
        if status is not SourceStatus.OK:
            return MetadataFetch(status=status)
        try:
            rows, total, next_page = _envelope(payload, "issues/", params)
            issues = [normalize.issue(row) for row in rows]
            if (
                len({row.external_id for row in issues}) != len(issues)
                or any(row.series_external_id != identifier for row in issues)
                or any(normalize.object_row(row).get("variant_of") is not None for row in rows)
            ):
                raise ValueError("Inconsistent GCD base-issue membership")
            truncated = next_page is not None and next_page > MAX_PAGE
            return MetadataFetch(
                status=SourceStatus.OK,
                data=MetadataPage(
                    results=issues,
                    total=total,
                    next_page=None if truncated else next_page,
                    truncated=truncated,
                ),
            )
        except (ValueError, TypeError, ArithmeticError):
            raise MetadataSourceError(SourceStatus.INCOMPATIBLE_RESPONSE) from None

    async def close(self) -> None:
        await self.client.aclose()
