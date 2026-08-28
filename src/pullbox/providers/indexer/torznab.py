"""Torznab indexer implementation.

Extends the Newznab indexer for torrent-based indexers. Adds seeders,
leechers, and other torrent-specific attributes to search results.

Torznab is a Newznab-compatible API extension used by Jackett, Prowlarr,
and torrent indexers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.providers.base import ReleaseResult
from pullbox.providers.indexer.torznab_transport import (
    ResolverAttemptCallback,
    TorznabDescriptor,
    TorznabTransport,
    TorznabTransportError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from xml.etree import ElementTree

    from pullbox.services.direct_resolver_service import NativeResolverOption
from pullbox.providers.indexer.newznab import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    NewznabError,
    NewznabIndexer,
    _check_xml_error,
    _parse_item_common,
    _safe_int,
)

logger = structlog.get_logger(__name__)


class TorznabIndexer(NewznabIndexer):
    """Torznab implementation of the Indexer protocol.

    Inherits all Newznab logic and overrides torrent-specific behaviour:
    ``supports_torrent = True``, ``supports_nzb = False``, and search
    results include seeders/leechers.  Constructor args are identical
    to ``NewznabIndexer``.
    """

    def __init__(
        self,
        name: str,
        url: str,
        api_key: str,
        rate_limit_per_minute: int = 5,
        *,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        resolver_enabled: bool | None = None,
        resolver_options: Sequence[NativeResolverOption] = (),
        request_transport: TorznabTransport | None = None,
        cache_namespace: str | None = None,
    ) -> None:
        super().__init__(
            name,
            url,
            api_key,
            rate_limit_per_minute,
            request_timeout=request_timeout,
        )
        self._browser_resolver_enabled = (
            bool(resolver_options) if resolver_enabled is None else resolver_enabled
        )
        self._request_transport = request_transport or TorznabTransport(
            resolver_options=resolver_options,
            http_client=self._client,
            cache_namespace=cache_namespace or f"manual-torznab:{self._base_url}",
            configured_base_url=self._base_url,
        )

    @property
    def browser_resolver_enabled(self) -> bool:
        """Whether descriptor handoff must stay inside Pullbox."""
        return self._browser_resolver_enabled

    @property
    def indexer_type(self) -> str:
        return "torznab"

    @property
    def supports_nzb(self) -> bool:
        return False

    @property
    def supports_torrent(self) -> bool:
        return True

    async def _request(self, params: dict[str, Any]) -> str:
        """Issue the credentialed API request through the bounded transport."""
        await self._wait_for_rate_limit()
        request_params: dict[str, Any] = (
            {"apikey": self._api_key, **params} if self._api_key else dict(params)
        )
        function = str(params.get("t", "request"))
        try:
            text = await self._request_transport.get_text(
                f"{self._base_url}/api",
                params=request_params,
                challenge_category=f"torznab_{function}",
            )
        except TorznabTransportError as exc:
            logger.warning(
                "torznab_request_failed",
                indexer=self._name,
                function=function,
                error_type=type(exc).__name__,
            )
            raise NewznabError(str(exc)) from exc
        if "<error " in text:
            _check_xml_error(text, self._name)
        return text

    async def fetch_torrent_descriptor(
        self,
        url: str,
        *,
        on_attempt: ResolverAttemptCallback | None = None,
    ) -> TorznabDescriptor:
        """Fetch a descriptor while keeping any embedded API key inside Pullbox."""
        try:
            return await self._request_transport.fetch_descriptor(
                url,
                on_attempt=on_attempt,
            )
        except TorznabTransportError as exc:
            raise NewznabError(str(exc)) from exc

    def _parse_item(self, item: ElementTree.Element) -> ReleaseResult:
        """Parse a single RSS <item> with torrent-specific attributes."""
        title, download_url, size_bytes, attrs, published_at, age_days, info_url = (
            _parse_item_common(item)
        )

        return ReleaseResult(
            title=title,
            indexer_name=self._name,
            download_url=download_url,
            size_bytes=size_bytes,
            age_days=age_days,
            seeders=_safe_int(attrs.get("seeders")),
            leechers=_safe_int(attrs.get("peers")),
            grabs=_safe_int(attrs.get("grabs")),
            protocol=AcquisitionProtocol.TORRENT,
            category=attrs.get("category"),
            published_at=published_at,
            info_url=info_url,
        )
