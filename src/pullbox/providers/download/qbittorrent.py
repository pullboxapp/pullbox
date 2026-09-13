"""qBittorrent download client implementation.

Integrates with the qBittorrent Web API v2 to manage torrent downloads.
Implements the DownloadClient protocol with session-based authentication,
auto-reconnect on session expiry, and structured logging.

qBittorrent API docs: https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from pullbox.core.config_resolver import resolve_runtime_service_url
from pullbox.core.torrent_metadata import torrent_info_hashes
from pullbox.core.url_validation import normalize_peer_base_url
from pullbox.providers.base import ClientOptions, DownloadStatus, ProviderHealthResult

logger = structlog.get_logger(__name__)

_REQUEST_TIMEOUT = 10.0
_HTTP_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_QBITTORRENT_API_ROUTES = {
    "/app/version": "api/v2/app/version",
    "/torrents/add": "api/v2/torrents/add",
    "/torrents/categories": "api/v2/torrents/categories",
    "/torrents/delete": "api/v2/torrents/delete",
    "/torrents/info": "api/v2/torrents/info",
}


class QBittorrentError(Exception):
    """Raised when the qBittorrent API returns an error."""


class QBittorrentClient:
    """qBittorrent implementation of DownloadClient.

    Uses session-based cookie authentication. Automatically re-authenticates
    when the session expires.

    Args:
        url: Base URL of the qBittorrent Web UI (e.g. ``http://localhost:8080``).
        username: Web UI username.
        password: Web UI password.
        category: Default download category (optional).
    """

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        category: str | None = None,
        content_layout: str | None = None,
        ratio_limit: float | None = None,
        seeding_time_limit: int | None = None,
    ) -> None:
        runtime_url = resolve_runtime_service_url(url)
        self._base_url = normalize_peer_base_url(
            runtime_url,
            reject_query_or_fragment=True,
        )
        self._username = username
        self._password = password
        self._default_category = category
        self._content_layout = content_layout
        self._ratio_limit = ratio_limit
        self._seeding_time_limit = seeding_time_limit
        self._client = httpx.AsyncClient(
            base_url=f"{self._base_url}/",
            timeout=_REQUEST_TIMEOUT,
        )
        self._authenticated = False

    @property
    def name(self) -> str:
        return "qbittorrent"

    @property
    def client_type(self) -> str:
        return "qbittorrent"

    # -- authentication -----------------------------------------------------

    async def _login(self) -> None:
        """Authenticate with the qBittorrent Web UI."""
        log = logger.bind(url=self._base_url)
        log.debug("qbittorrent_login")

        try:
            response = await self._client.post(
                "api/v2/auth/login",
                data={"username": self._username, "password": self._password},
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            log.error("qbittorrent_login_timeout")
            raise QBittorrentError("Login timed out") from None
        except httpx.HTTPError as exc:
            log.error("qbittorrent_login_failed", error=str(exc))
            raise QBittorrentError(f"Login failed: {exc}") from None

        if response.text == "Fails.":
            log.error("qbittorrent_login_invalid_credentials")
            raise QBittorrentError("Invalid username or password")

        # Session cookie is automatically stored by httpx
        self._authenticated = True
        log.debug("qbittorrent_login_success")

    async def _ensure_auth(self) -> None:
        """Ensure we have a valid session, logging in if needed."""
        if not self._authenticated:
            await self._login()

    # -- internal request plumbing ------------------------------------------

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> httpx.Response:
        """Make an authenticated request to the qBittorrent API.

        Automatically retries once on 403 (session expired).
        """
        await self._ensure_auth()

        api_route = _QBITTORRENT_API_ROUTES.get(endpoint)
        if api_route is None:
            raise ValueError("Unsupported qBittorrent API endpoint.")
        log = logger.bind(endpoint=endpoint)
        log.debug("qbittorrent_request")

        for attempt in range(2):
            try:
                response = await self._client.request(
                    method,
                    api_route,
                    data=data,
                    params=params,
                    files=files,
                )
            except httpx.TimeoutException:
                log.error("qbittorrent_timeout")
                raise QBittorrentError(f"Request timed out: {endpoint}") from None
            except httpx.HTTPError as exc:
                log.error("qbittorrent_request_failed", error=str(exc))
                raise QBittorrentError(f"Request failed: {exc}") from None

            # 403 means session expired — re-authenticate and retry once
            if response.status_code == 403 and attempt == 0:
                log.debug("qbittorrent_session_expired")
                self._authenticated = False
                await self._login()
                continue

            if response.status_code >= 400:
                log.error("qbittorrent_http_error", status=response.status_code)
                raise QBittorrentError(f"HTTP {response.status_code}: {endpoint}")

            return response

        raise QBittorrentError(f"Request failed after retry: {endpoint}")

    # -- DownloadClient implementation --------------------------------------

    async def _resolve_redirected_magnet_url(self, url: str, log: Any) -> str:
        """Resolve HTTP download URLs that immediately redirect to magnets.

        Some Torznab/Prowlarr download endpoints expose torrent results as HTTP
        URLs that redirect to ``magnet:`` links. qBittorrent does not reliably
        add those redirecting URLs, so submit the final magnet directly when we
        can detect it without downloading the torrent payload.
        """
        if not url.lower().startswith(("http://", "https://")):
            return url

        current_url = url
        for attempt in range(3):
            try:
                response = await self._client.request(
                    "GET",
                    current_url,
                    follow_redirects=False,
                    headers={"Range": "bytes=0-0"},
                )
            except httpx.HTTPError as exc:
                log.debug(
                    "qbittorrent_magnet_redirect_probe_failed",
                    attempt=attempt + 1,
                    error=str(exc),
                    url_prefix=current_url[:80],
                )
                return url

            if response.status_code not in _HTTP_REDIRECT_STATUSES:
                return url

            location = response.headers.get("location")
            if not location:
                return url
            redirect_location = str(location)

            if redirect_location.lower().startswith("magnet:"):
                log.info(
                    "qbittorrent_download_url_resolved_to_magnet",
                    hash=_extract_magnet_hash(redirect_location),
                    url_prefix=url[:80],
                )
                return redirect_location

            if redirect_location.lower().startswith(("http://", "https://")):
                current_url = redirect_location
            else:
                current_url = str(httpx.URL(current_url).join(redirect_location))

        return url

    async def add_nzb(
        self,
        url: str,
        title: str,
        category: str | None = None,
    ) -> str:
        """Not supported — qBittorrent is torrent-only."""
        raise NotImplementedError("qBittorrent does not support NZB downloads")

    async def add_torrent(
        self,
        url: str,
        title: str,
        category: str | None = None,
    ) -> str | None:
        """Add a torrent or magnet URL. Returns the torrent hash."""
        import asyncio

        log = logger.bind(title=title, category=category)
        log.info("qbittorrent_add_torrent")

        submit_url = await self._resolve_redirected_magnet_url(url, log)

        # If this is a magnet link we can extract the info-hash directly,
        # which is far more reliable than the before/after comparison.
        known_hash = _extract_magnet_hash(submit_url)
        if known_hash:
            log.debug("qbittorrent_magnet_hash_extracted", hash=known_hash)

        # Snapshot existing torrent hashes so we can detect the new one
        # when we don't have a known hash.
        before_resp = await self._request(
            "GET",
            "/torrents/info",
            params={"sort": "added_on", "reverse": "true", "limit": "50"},
        )
        existing_hashes = {t.get("hash") for t in before_resp.json()}

        form_data: dict[str, Any] = {"urls": submit_url, "rename": title}
        cat = category or self._default_category
        if cat:
            form_data["category"] = cat
        if self._content_layout:
            form_data["contentLayout"] = self._content_layout
        if self._ratio_limit is not None and self._ratio_limit > 0:
            form_data["ratioLimit"] = str(self._ratio_limit)
        if self._seeding_time_limit is not None and self._seeding_time_limit > 0:
            form_data["seedingTimeLimit"] = str(self._seeding_time_limit)

        await self._request("POST", "/torrents/add", data=form_data)

        # If we already know the hash (magnet link), verify it appears in
        # qBittorrent, then return it.  It may already be there (duplicate
        # add) or may need a moment to register.
        if known_hash:
            # It might already exist (duplicate add) — check immediately
            if known_hash in existing_hashes:
                log.info(
                    "qbittorrent_torrent_already_exists",
                    hash=known_hash,
                    hint="Torrent was already in qBittorrent; reusing.",
                )
                return known_hash

            # Poll for the hash to appear
            for attempt in range(6):
                delay = 0.3 if attempt < 2 else 1.0
                await asyncio.sleep(delay)

                response = await self._request(
                    "GET",
                    "/torrents/info",
                    params={"hashes": known_hash},
                )
                torrents = response.json()
                if torrents:
                    log.info(
                        "qbittorrent_torrent_added",
                        hash=known_hash,
                        poll_attempts=attempt + 1,
                    )
                    return known_hash

            # Even if polling timed out, the add likely succeeded.
            # Return the hash — monitor_downloads will track it.
            log.warning(
                "qbittorrent_hash_not_yet_visible",
                hash=known_hash,
                hint="Torrent hash not found after polling but returning it "
                "anyway since we extracted it from the magnet link.",
            )
            return known_hash

        # Non-magnet URL (e.g. .torrent file link): fall back to
        # before/after comparison to detect the new torrent.
        for attempt in range(6):  # up to ~5.5 seconds total
            delay = 0.3 if attempt < 2 else 1.0
            await asyncio.sleep(delay)

            response = await self._request(
                "GET",
                "/torrents/info",
                params={"sort": "added_on", "reverse": "true", "limit": "50"},
            )
            torrents = response.json()

            # Look for a torrent that wasn't in the list before
            for torrent in torrents:
                torrent_hash = torrent.get("hash", "")
                if torrent_hash and torrent_hash not in existing_hashes:
                    log.info(
                        "qbittorrent_torrent_added",
                        hash=torrent_hash,
                        poll_attempts=attempt + 1,
                    )
                    return str(torrent_hash)

            log.debug(
                "qbittorrent_waiting_for_torrent",
                attempt=attempt + 1,
                known_count=len(torrents),
            )

        # Last resort: the torrent may already have existed (duplicate add).
        # Try to find it by matching the rename title we set.
        for torrent in torrents:
            if torrent.get("name") == title:
                torrent_hash = torrent.get("hash", "")
                if torrent_hash:
                    log.info(
                        "qbittorrent_torrent_matched_by_name",
                        hash=torrent_hash,
                        hint="Torrent likely already existed; matched by name.",
                    )
                    return str(torrent_hash)

        # Check if the torrent list is unchanged — qBittorrent silently
        # rejects duplicate torrents (POST returns success but nothing is
        # added).  If the hash set is identical to before, the add was a
        # no-op and we should fail immediately rather than leaving a
        # phantom download record for 10 minutes.
        current_hashes = {t.get("hash") for t in torrents}
        if current_hashes == existing_hashes:
            log.warning(
                "qbittorrent_torrent_not_added",
                url_prefix=submit_url[:80],
                hint="Torrent list unchanged after add. qBittorrent may have "
                "rejected the URL, failed to resolve it, or already had it.",
            )
            raise QBittorrentError(
                "Torrent was not added to qBittorrent. The client accepted the "
                "request but its torrent list did not change; the URL may have "
                "been rejected, failed to resolve, or already exist in the client."
            )

        # Torrent list changed but we couldn't match the new hash — the
        # torrent metadata may still be downloading.  Return None so the
        # download record is created; monitor_downloads will match it by
        # title later.
        log.warning(
            "qbittorrent_hash_not_found_returning_none",
            url_prefix=submit_url[:80],
            hint="Torrent added but hash not yet identifiable. "
            "monitor_downloads will match it by title.",
        )
        return None

    async def add_torrent_data(
        self,
        content: bytes,
        title: str,
        category: str | None = None,
    ) -> str | None:
        """Upload descriptor bytes that Pullbox already fetched and validated."""
        import asyncio

        log = logger.bind(title=title, category=category)
        log.info("qbittorrent_add_torrent_data")
        expected_hashes = _torrent_info_hashes(content)
        before_resp = await self._request(
            "GET",
            "/torrents/info",
            params={"sort": "added_on", "reverse": "true", "limit": "50"},
        )
        existing_hashes = {
            str(torrent_hash).lower()
            for torrent in before_resp.json()
            if (torrent_hash := torrent.get("hash"))
        }
        form_data: dict[str, Any] = {"rename": title}
        cat = category or self._default_category
        if cat:
            form_data["category"] = cat
        if self._content_layout:
            form_data["contentLayout"] = self._content_layout
        if self._ratio_limit is not None and self._ratio_limit > 0:
            form_data["ratioLimit"] = str(self._ratio_limit)
        if self._seeding_time_limit is not None and self._seeding_time_limit > 0:
            form_data["seedingTimeLimit"] = str(self._seeding_time_limit)

        filename = f"{title}.torrent" if not title.endswith(".torrent") else title
        await self._request(
            "POST",
            "/torrents/add",
            data=form_data,
            files={"torrents": (filename, content, "application/x-bittorrent")},
        )

        torrents: list[dict[str, Any]] = []
        for attempt in range(6):
            await asyncio.sleep(0.3 if attempt < 2 else 1.0)
            response = await self._request(
                "GET",
                "/torrents/info",
                params={"sort": "added_on", "reverse": "true", "limit": "50"},
            )
            torrents = response.json()
            for torrent in torrents:
                torrent_hash = str(torrent.get("hash", "")).lower()
                if torrent_hash in expected_hashes:
                    return torrent_hash

        current_hashes = {
            str(torrent_hash).lower()
            for torrent in torrents
            if (torrent_hash := torrent.get("hash"))
        }
        if current_hashes == existing_hashes:
            raise QBittorrentError(
                "Torrent was not added to qBittorrent. The descriptor may already exist."
            )
        log.warning(
            "qbittorrent_descriptor_hash_not_yet_visible",
            expected_hashes=sorted(expected_hashes),
            hint="Other torrents appeared while the uploaded descriptor was still unavailable; "
            "monitor_downloads will match it by title without adopting an unrelated hash.",
        )
        return None

    async def find_torrent_by_title(self, title: str) -> str | None:
        """Search for a torrent by its name/title. Returns hash or None."""
        response = await self._request(
            "GET",
            "/torrents/info",
            params={"sort": "added_on", "reverse": "true", "limit": "50"},
        )
        for torrent in response.json():
            if torrent.get("name") == title:
                found_hash = torrent.get("hash", "")
                if found_hash:
                    logger.debug(
                        "qbittorrent_torrent_found_by_title",
                        title=title,
                        hash=found_hash,
                    )
                    return str(found_hash)
        return None

    async def get_download_status(self, external_id: str) -> DownloadStatus:
        """Get status of a specific torrent by hash."""
        log = logger.bind(external_id=external_id)
        log.debug("qbittorrent_get_status")

        response = await self._request("GET", "/torrents/info", params={"hashes": external_id})
        torrents: list[dict[str, Any]] = response.json()

        if not torrents:
            raise QBittorrentError(f"Torrent not found: {external_id}")

        return _map_torrent(torrents[0])

    async def get_queue(self) -> list[DownloadStatus]:
        """Get all active torrents."""
        logger.debug("qbittorrent_get_queue")

        response = await self._request(
            "GET",
            "/torrents/info",
            params={"filter": "all"},
        )
        torrents: list[dict[str, Any]] = response.json()

        results = [_map_torrent(t) for t in torrents]
        logger.debug("qbittorrent_queue_fetched", count=len(results))
        return results

    async def remove_download(
        self,
        external_id: str,
        delete_files: bool = False,
    ) -> bool:
        """Remove a torrent by hash, optionally deleting files."""
        log = logger.bind(external_id=external_id, delete_files=delete_files)
        log.info("qbittorrent_remove_download")

        try:
            await self._request(
                "POST",
                "/torrents/delete",
                data={
                    "hashes": external_id,
                    "deleteFiles": "true" if delete_files else "false",
                },
            )
            log.info("qbittorrent_torrent_removed")
            return True
        except QBittorrentError:
            log.warning("qbittorrent_remove_failed")
            return False

    async def test_connection(self) -> ProviderHealthResult:
        """Verify connectivity by logging in and fetching the API version."""
        start = time.monotonic()
        try:
            response = await self._request("GET", "/app/version")
            elapsed_ms = (time.monotonic() - start) * 1000
            version = response.text.strip()
            return ProviderHealthResult(
                healthy=True,
                message=f"qBittorrent {version}",
                response_time_ms=elapsed_ms,
                details={"version": version},
            )
        except QBittorrentError as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return ProviderHealthResult(
                healthy=False,
                message=f"qBittorrent error: {exc}",
                response_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.exception("qbittorrent_test_connection_failed")
            return ProviderHealthResult(
                healthy=False,
                message=f"Connection failed: {exc}",
                response_time_ms=elapsed_ms,
            )

    async def get_options(self) -> ClientOptions:
        """Fetch available categories from qBittorrent."""
        response = await self._request("GET", "/torrents/categories")
        cat_data: dict[str, Any] = response.json()
        categories = sorted(cat_data.keys())
        directories = [v.get("savePath", "") for v in cat_data.values() if v.get("savePath")]
        return ClientOptions(
            categories=categories,
            download_directories=directories or None,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _map_torrent(torrent: dict[str, Any]) -> DownloadStatus:
    """Map a qBittorrent torrent info dict to a DownloadStatus DTO."""
    raw_state = torrent.get("state", "")
    state = _map_torrent_state(raw_state)
    client_state = _human_readable_state(raw_state)
    progress = torrent.get("progress", 0.0)
    size_bytes = torrent.get("total_size") or torrent.get("size")
    speed_bytes = torrent.get("dlspeed")
    eta_seconds = torrent.get("eta")

    # qBittorrent uses 8640000 as "infinity" for ETA
    if eta_seconds and eta_seconds >= 8640000:
        eta_seconds = None

    # Zero out speed/ETA for non-active states (stalled, metadata, etc.)
    if raw_state in ("stalledDL", "metaDL", "allocating", "checkingDL", "checkingResumeData"):
        speed_bytes = 0
        eta_seconds = None

    # content_path is the full path to the downloaded content
    content_path = torrent.get("content_path") or torrent.get("save_path") or None

    return DownloadStatus(
        external_id=torrent.get("hash", ""),
        title=torrent.get("name", "Unknown"),
        state=state,
        progress=progress,
        size_bytes=size_bytes,
        speed_bytes=speed_bytes,
        eta_seconds=eta_seconds,
        error_message=None if state != "failed" else "Download error",
        downloaded_path=content_path,
        client_state=client_state,
    )


def _extract_magnet_hash(url: str) -> str | None:
    """Extract the info-hash from a magnet URI, or None for non-magnet URLs.

    Supports both v1 (btih, 40-char hex or base32) and v2 (btmh) hashes.
    qBittorrent normalises hashes to lowercase hex, so we do the same.
    """
    if not url.lower().startswith("magnet:"):
        return None

    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    xt_list = params.get("xt", [])

    for xt in xt_list:
        # v1: urn:btih:<hex-or-base32>
        if xt.lower().startswith("urn:btih:"):
            raw = xt[9:]  # strip "urn:btih:"
            if len(raw) == 40:
                # Already hex
                return raw.lower()
            if len(raw) == 32:
                # Base32 encoded — decode to hex
                import base64

                try:
                    decoded = base64.b32decode(raw, casefold=True)
                    return decoded.hex()
                except Exception:
                    return raw.lower()
            # Unexpected length — return as-is
            return raw.lower()

    return None


def _torrent_info_hashes(content: bytes) -> frozenset[str]:
    """Return exact v1/v2 info hashes from one bounded bencoded descriptor."""
    try:
        return torrent_info_hashes(content)
    except ValueError as exc:
        raise QBittorrentError(
            "Invalid torrent descriptor: missing valid bencoded info data"
        ) from exc


def _map_torrent_state(state: str) -> str:
    """Map qBittorrent torrent state to normalized state."""
    mapping = {
        "downloading": "downloading",
        "stalledDL": "downloading",
        "metaDL": "downloading",
        "forcedDL": "downloading",
        "allocating": "downloading",
        "uploading": "completed",
        "stalledUP": "completed",
        "forcedUP": "completed",
        "pausedDL": "paused",
        "pausedUP": "paused",
        "queuedDL": "queued",
        "queuedUP": "queued",
        "checkingDL": "downloading",
        "checkingUP": "completed",
        "checkingResumeData": "queued",
        "moving": "downloading",
        "error": "failed",
        "missingFiles": "failed",
        "unknown": "queued",
    }
    return mapping.get(state, state.lower())


def _human_readable_state(state: str) -> str | None:
    """Map qBittorrent raw state to a human-readable label for the UI.

    Returns ``None`` for states where the normalized name is sufficient
    (e.g. "downloading" → no extra label needed).
    """
    labels: dict[str, str] = {
        "stalledDL": "Stalled",
        "metaDL": "Downloading Metadata",
        "allocating": "Allocating",
        "checkingDL": "Checking",
        "checkingUP": "Checking",
        "checkingResumeData": "Checking",
        "forcedDL": "Forced Download",
        "moving": "Moving",
        "missingFiles": "Missing Files",
    }
    return labels.get(state)
