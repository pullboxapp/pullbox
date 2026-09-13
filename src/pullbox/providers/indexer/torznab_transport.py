"""Bounded browser-challenge transport for opted-in manual Torznab endpoints."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import structlog

from pullbox.core.torrent_metadata import torrent_info_hashes
from pullbox.providers.direct.resolver import (
    DirectResolverCookie,
    DirectResolverError,
    DirectResolverResult,
)
from pullbox.services.direct_resolver_service import (
    NativeResolverOption,
    ResolverAttemptProgress,
)

logger = structlog.get_logger(__name__)

_CACHE_TTL_SECONDS = 15 * 60
_MAX_CACHE_ENTRIES = 64
_MAX_API_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_DESCRIPTOR_BYTES = 10 * 1024 * 1024
_MAX_DESCRIPTOR_REDIRECTS = 4
_MAX_CHALLENGE_BODY_CHARS = 256_000
_CHALLENGE_STATUSES = frozenset({403, 429, 503})
_CHALLENGE_MARKERS = (
    "cf-chl-",
    "challenge-platform",
    "checking your browser",
    "just a moment",
    "turnstile",
    "captcha",
)

ResolverAttemptCallback = Callable[[ResolverAttemptProgress], Awaitable[None]]
QueryParam = str | int | float | bool | None
QueryParams = Mapping[str, QueryParam | Sequence[QueryParam]]


class TorznabTransportError(RuntimeError):
    """A classified Torznab transport failure that never includes credentials."""


@dataclass(frozen=True, slots=True)
class TorznabDescriptor:
    """Torrent handoff material fetched by Pullbox rather than the client."""

    content: bytes | None
    magnet_url: str | None


@dataclass(frozen=True, slots=True)
class _SessionMaterial:
    cookies: tuple[DirectResolverCookie, ...] = field(repr=False)
    user_agent: str | None = field(default=None, repr=False)
    expires_at: float = field(default=0.0, repr=False)


_session_cache: OrderedDict[str, _SessionMaterial] = OrderedDict()
_solve_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()


class TorznabTransport:
    """Keep Torznab credentials in Pullbox while resolvers solve only an origin."""

    def __init__(
        self,
        *,
        resolver_options: Sequence[NativeResolverOption] = (),
        http_client: httpx.AsyncClient,
        cache_namespace: str = "manual-torznab",
        configured_base_url: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._resolver_options = tuple(resolver_options)
        self._client = http_client
        self._cache_namespace = cache_namespace
        self._configured_origin = (
            _origin(configured_base_url) if configured_base_url is not None else None
        )
        self._clock = clock

    async def get_text(
        self,
        url: str,
        *,
        params: QueryParams,
        challenge_category: str,
        on_attempt: ResolverAttemptCallback | None = None,
    ) -> str:
        response = await self._request_with_resolvers(
            url,
            params=params,
            challenge_category=challenge_category,
            on_attempt=on_attempt,
            max_response_bytes=_MAX_API_RESPONSE_BYTES,
        )
        return response.text

    async def fetch_descriptor(
        self,
        url: str,
        *,
        on_attempt: ResolverAttemptCallback | None = None,
    ) -> TorznabDescriptor:
        if urlsplit(url).scheme.lower() == "magnet":
            return TorznabDescriptor(content=None, magnet_url=url)

        current_url = url
        for _redirect in range(_MAX_DESCRIPTOR_REDIRECTS + 1):
            response = await self._request_with_resolvers(
                current_url,
                params=None,
                challenge_category="torznab_descriptor",
                on_attempt=on_attempt,
                allow_redirect=True,
                max_response_bytes=_MAX_DESCRIPTOR_BYTES,
            )
            if response.is_redirect:
                location = response.headers.get("location", "")
                if location.startswith("magnet:?"):
                    return TorznabDescriptor(content=None, magnet_url=location)
                next_url = urljoin(str(response.url), location)
                if not _same_origin(str(response.url), next_url):
                    raise TorznabTransportError(
                        "The Torznab descriptor redirected outside its configured origin."
                    )
                current_url = next_url
                continue

            content = response.content
            if len(content) > _MAX_DESCRIPTOR_BYTES:
                raise TorznabTransportError("The torrent descriptor exceeds the safe size limit.")
            if not _looks_like_torrent(content):
                raise TorznabTransportError(
                    "The Torznab response was not a valid torrent descriptor."
                )
            return TorznabDescriptor(content=content, magnet_url=None)
        raise TorznabTransportError("The Torznab descriptor redirected too many times.")

    async def _request_with_resolvers(
        self,
        url: str,
        *,
        params: QueryParams | None,
        challenge_category: str,
        on_attempt: ResolverAttemptCallback | None,
        allow_redirect: bool = False,
        max_response_bytes: int,
    ) -> httpx.Response:
        origin = _origin(url)
        if self._configured_origin is not None and origin != self._configured_origin:
            raise TorznabTransportError(
                "The torrent descriptor is outside the configured Torznab origin."
            )
        cache_key = f"{self._cache_namespace}:{origin}"
        material = _read_cached_material(cache_key, clock=self._clock)
        response = await self._get(
            url,
            params=params,
            material=material,
            max_response_bytes=max_response_bytes,
        )
        if not _is_browser_challenge(response):
            return _accept_response(response, allow_redirect=allow_redirect)
        if not self._resolver_options:
            raise TorznabTransportError(
                "The manual Torznab endpoint requires browser challenge resolution."
            )

        lock = _lock_for(cache_key)
        async with lock:
            current = _read_cached_material(cache_key, clock=self._clock)
            if current is not None and current is not material:
                response = await self._get(
                    url,
                    params=params,
                    material=current,
                    max_response_bytes=max_response_bytes,
                )
                if not _is_browser_challenge(response):
                    return _accept_response(response, allow_redirect=allow_redirect)

            host = urlsplit(origin).hostname or ""
            last_error: Exception | None = None
            total = len(self._resolver_options)
            for attempt, option in enumerate(self._resolver_options, start=1):
                if on_attempt is not None:
                    await on_attempt(
                        ResolverAttemptProgress(
                            resolver_id=option.resolver_id,
                            resolver_name=option.resolver_name,
                            resolver_kind=option.resolver_kind,
                            attempt=attempt,
                            total=total,
                            scope="manual_torznab",
                        )
                    )
                try:
                    result = await option.solve(
                        origin,
                        declared_domains=(host,),
                        challenge_category=challenge_category,
                    )
                except DirectResolverError as exc:
                    last_error = exc
                    continue

                candidate = _material_from_result(result, clock=self._clock)
                response = await self._get(
                    url,
                    params=params,
                    material=candidate,
                    max_response_bytes=max_response_bytes,
                )
                if _is_browser_challenge(response):
                    continue
                accepted = _accept_response(response, allow_redirect=allow_redirect)
                _write_cached_material(cache_key, candidate)
                return accepted

        logger.warning(
            "manual_torznab_resolver_chain_exhausted",
            origin_host=urlsplit(origin).hostname,
            resolver_count=len(self._resolver_options),
            last_error_type=type(last_error).__name__ if last_error else None,
        )
        raise TorznabTransportError(
            "The browser resolver chain could not access the manual Torznab endpoint."
        ) from last_error

    async def _get(
        self,
        url: str,
        *,
        params: QueryParams | None,
        material: _SessionMaterial | None,
        max_response_bytes: int,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if material is not None:
            if material.user_agent:
                headers["user-agent"] = material.user_agent
            cookie_header = _cookie_header(material.cookies, url)
            if cookie_header:
                headers["cookie"] = cookie_header
        try:
            request = self._client.build_request("GET", url, params=params, headers=headers)
            response = await self._client.send(request, stream=True, follow_redirects=False)
            try:
                declared_length = response.headers.get("content-length")
                if declared_length is not None:
                    try:
                        if int(declared_length) > max_response_bytes:
                            raise TorznabTransportError(
                                "The Torznab response exceeds the safe size limit."
                            )
                    except ValueError:
                        pass

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_response_bytes:
                        raise TorznabTransportError(
                            "The Torznab response exceeds the safe size limit."
                        )
                    chunks.append(chunk)
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=b"".join(chunks),
                    request=request,
                    extensions=response.extensions,
                )
            finally:
                await response.aclose()
        except httpx.TimeoutException:
            raise TorznabTransportError("The Torznab request timed out.") from None
        except httpx.HTTPError as exc:
            raise TorznabTransportError(
                f"The Torznab request failed ({type(exc).__name__})."
            ) from exc


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise TorznabTransportError("The configured Torznab endpoint URL is invalid.")
    return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


def _same_origin(left: str, right: str) -> bool:
    return _origin(left) == _origin(right)


def _is_browser_challenge(response: httpx.Response) -> bool:
    if response.status_code not in _CHALLENGE_STATUSES and response.status_code != 200:
        return False
    body = response.text[:_MAX_CHALLENGE_BODY_CHARS].lower()
    if response.status_code == 200:
        content_type = response.headers.get("content-type", "").lower()
        stripped = body.lstrip()
        if "html" not in content_type and not stripped.startswith(("<!doctype html", "<html")):
            return False
    return any(marker in body for marker in _CHALLENGE_MARKERS)


def _accept_response(response: httpx.Response, *, allow_redirect: bool) -> httpx.Response:
    if allow_redirect and response.is_redirect:
        return response
    if response.status_code >= 400:
        raise TorznabTransportError(f"Torznab returned HTTP {response.status_code}.")
    return response


def _material_from_result(
    result: DirectResolverResult,
    *,
    clock: Callable[[], float],
) -> _SessionMaterial:
    return _SessionMaterial(
        cookies=result.cookies,
        user_agent=result.user_agent,
        expires_at=clock() + _CACHE_TTL_SECONDS,
    )


def _cookie_header(cookies: Sequence[DirectResolverCookie], url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    values: list[str] = []
    for cookie in cookies:
        domain = (cookie.domain or host).lstrip(".").lower()
        if host == domain or host.endswith(f".{domain}"):
            values.append(f"{cookie.name}={cookie.value}")
    return "; ".join(values)


def _looks_like_torrent(content: bytes) -> bool:
    try:
        torrent_info_hashes(content)
    except ValueError:
        return False
    return True


def _read_cached_material(
    key: str,
    *,
    clock: Callable[[], float],
) -> _SessionMaterial | None:
    material = _session_cache.get(key)
    if material is None:
        return None
    if material.expires_at <= clock():
        _session_cache.pop(key, None)
        return None
    _session_cache.move_to_end(key)
    return material


def _write_cached_material(key: str, material: _SessionMaterial) -> None:
    _session_cache[key] = material
    _session_cache.move_to_end(key)
    while len(_session_cache) > _MAX_CACHE_ENTRIES:
        _session_cache.popitem(last=False)


def _lock_for(key: str) -> asyncio.Lock:
    lock = _solve_locks.get(key)
    if lock is None:
        if len(_solve_locks) >= _MAX_CACHE_ENTRIES:
            removable_key = next(
                (candidate for candidate, item in _solve_locks.items() if not item.locked()),
                None,
            )
            if removable_key is None:
                raise TorznabTransportError(
                    "Pullbox is already resolving too many browser challenges."
                )
            _solve_locks.pop(removable_key, None)
        lock = asyncio.Lock()
        _solve_locks[key] = lock
    _solve_locks.move_to_end(key)
    return lock
