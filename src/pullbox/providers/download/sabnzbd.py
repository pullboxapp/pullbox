"""SABnzbd download client implementation.

Integrates with the SABnzbd API to manage Usenet NZB downloads.
Implements the DownloadClient protocol with API key authentication,
category support, and structured logging.

SABnzbd API docs: https://sabnzbd.org/wiki/advanced/api
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import structlog

from pullbox.core.config_resolver import resolve_runtime_service_url
from pullbox.providers.base import ClientOptions, DownloadStatus, ProviderHealthResult

logger = structlog.get_logger(__name__)

_REQUEST_TIMEOUT = 10.0
_NZB_FETCH_TIMEOUT = httpx.Timeout(10.0, read=60.0)
_NZB_FETCH_DEADLINE = 90.0


class SABnzbdError(Exception):
    """Raised when the SABnzbd API returns an error."""


class SABnzbdClient:
    """SABnzbd implementation of DownloadClient.

    Args:
        url: Base URL of the SABnzbd instance (e.g. ``http://localhost:8080``).
        api_key: SABnzbd API key for authentication.
        category: Default download category (optional).
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        category: str | None = None,
        priority: str | None = None,
        post_processing: str | None = None,
    ) -> None:
        self._base_url = resolve_runtime_service_url(url).rstrip("/")
        self._api_key = api_key
        self._default_category = category
        self._default_priority = priority
        self._default_post_processing = post_processing
        self._client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)

    @property
    def name(self) -> str:
        return "sabnzbd"

    @property
    def client_type(self) -> str:
        return "sabnzbd"

    # -- internal request plumbing ------------------------------------------

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        """Make an authenticated GET request to the SABnzbd API."""
        request_params: dict[str, Any] = {
            "apikey": self._api_key,
            "output": "json",
            **params,
        }
        url = f"{self._base_url}/api"

        log = logger.bind(mode=params.get("mode"))
        log.debug("sabnzbd_request")

        try:
            response = await self._client.get(url, params=request_params)
            response.raise_for_status()
        except httpx.TimeoutException:
            log.error("sabnzbd_timeout")
            raise SABnzbdError("Request timed out") from None
        except httpx.HTTPStatusError as exc:
            log.error("sabnzbd_http_error", status=exc.response.status_code)
            raise SABnzbdError(f"HTTP {exc.response.status_code}") from None
        except httpx.HTTPError as exc:
            log.error("sabnzbd_request_failed", error=str(exc))
            raise SABnzbdError(f"Request failed: {exc}") from None

        data: dict[str, Any] = response.json()

        # SABnzbd returns {"status": false, "error": "..."} on failure
        if data.get("status") is False:
            error_msg = data.get("error", "Unknown error")
            log.error("sabnzbd_api_error", error=error_msg)
            raise SABnzbdError(error_msg)

        return data

    # -- DownloadClient implementation --------------------------------------

    @staticmethod
    def _response_looks_like_nzb(content_type: str, body: bytes) -> bool:
        """Return True when a response body appears to contain NZB XML."""
        normalized_type = content_type.lower()
        if "xml" in normalized_type or "nzb" in normalized_type:
            return True

        body_start = body[:100].lstrip()
        return body_start.startswith((b"<?xml", b"<nzb", b"<!DOCTYPE nzb"))

    async def _download_nzb_bytes(self, url: str) -> bytes:
        """Fetch NZB bytes locally so SAB does not need direct indexer access."""
        try:
            # Indexer proxies may retry upstream; keep local SAB control calls short.
            async with asyncio.timeout(_NZB_FETCH_DEADLINE):
                response = await self._client.get(
                    url, follow_redirects=True, timeout=_NZB_FETCH_TIMEOUT
                )
            response.raise_for_status()
        except (TimeoutError, httpx.TimeoutException):
            raise SABnzbdError("Failed to download NZB from URL: Request timed out") from None
        except httpx.HTTPStatusError as exc:
            raise SABnzbdError(
                f"Failed to download NZB from URL: HTTP {exc.response.status_code}"
            ) from None
        except httpx.HTTPError as exc:
            raise SABnzbdError(f"Failed to download NZB from URL: {exc}") from None

        if not self._response_looks_like_nzb(
            response.headers.get("content-type", ""),
            response.content,
        ):
            content_type = response.headers.get("content-type", "")
            raise SABnzbdError(f"URL did not return NZB content (content-type: {content_type})")

        return response.content

    async def _upload_nzb_bytes(
        self,
        nzb_bytes: bytes,
        title: str,
        category: str | None = None,
    ) -> str:
        """Upload validated NZB bytes to SABnzbd using addfile mode."""
        nzb_filename = title if title.lower().endswith(".nzb") else f"{title}.nzb"
        params: dict[str, Any] = {
            "apikey": self._api_key,
            "output": "json",
            "mode": "addfile",
            "nzbname": title,
        }
        cat = category or self._default_category
        if cat:
            params["cat"] = cat
        if self._default_priority:
            params["priority"] = self._default_priority
        if self._default_post_processing:
            params["pp"] = self._default_post_processing

        try:
            upload = await self._client.post(
                f"{self._base_url}/api",
                params=params,
                files={"nzbfile": (nzb_filename, nzb_bytes, "application/x-nzb")},
            )
            upload.raise_for_status()
        except httpx.TimeoutException:
            raise SABnzbdError("Request timed out") from None
        except httpx.HTTPStatusError as exc:
            raise SABnzbdError(f"HTTP {exc.response.status_code}") from None
        except httpx.HTTPError as exc:
            raise SABnzbdError(f"Request failed: {exc}") from None

        try:
            data: dict[str, Any] = upload.json()
        except ValueError:
            raise SABnzbdError("Invalid JSON response from SABnzbd") from None

        if data.get("status") is False:
            error_msg = data.get("error", "Unknown error")
            raise SABnzbdError(error_msg)

        nzo_ids = data.get("nzo_ids", [])
        if not nzo_ids:
            raise SABnzbdError("No nzo_id returned after adding NZB")

        return str(nzo_ids[0])

    async def add_nzb(
        self,
        url: str,
        title: str,
        category: str | None = None,
    ) -> str:
        """Download an NZB locally, then upload it to SABnzbd.

        This avoids relying on SABnzbd being able to reach the indexer URL
        directly, which can fail when Pullbox and SAB live on different
        networks or have different routing.
        """
        log = logger.bind(title=title, category=category)
        log.info("sabnzbd_add_nzb")

        nzb_bytes = await self._download_nzb_bytes(url)
        nzo_id = await self._upload_nzb_bytes(nzb_bytes, title, category=category)
        log.info("sabnzbd_nzb_added", nzo_id=nzo_id)
        return nzo_id

    async def add_torrent(
        self,
        url: str,
        title: str,
        category: str | None = None,
    ) -> str:
        """Not supported — SABnzbd is Usenet-only."""
        raise NotImplementedError("SABnzbd does not support torrent downloads")

    async def add_torrent_data(
        self,
        content: bytes,
        title: str,
        category: str | None = None,
    ) -> str | None:
        """Not supported - SABnzbd is Usenet-only."""
        raise NotImplementedError("SABnzbd does not support torrent downloads")

    async def get_download_status(self, external_id: str) -> DownloadStatus:
        """Get status of a specific download by nzo_id.

        Checks the active queue first, then falls back to history.
        """
        log = logger.bind(external_id=external_id)
        log.debug("sabnzbd_get_status")

        # Check active queue
        queue_data = await self._request({"mode": "queue"})
        for slot in queue_data.get("queue", {}).get("slots", []):
            if slot.get("nzo_id") == external_id:
                return self._map_queue_slot(slot)

        # Check history
        history_data = await self._request({"mode": "history", "limit": 50})
        for slot in history_data.get("history", {}).get("slots", []):
            if slot.get("nzo_id") == external_id:
                return self._map_history_slot(slot)

        raise SABnzbdError(f"Download not found: {external_id}")

    async def get_queue(self) -> list[DownloadStatus]:
        """Get all active downloads from the SABnzbd queue."""
        logger.debug("sabnzbd_get_queue")

        data = await self._request({"mode": "queue"})
        slots = data.get("queue", {}).get("slots", [])

        results = [self._map_queue_slot(slot) for slot in slots]
        logger.debug("sabnzbd_queue_fetched", count=len(results))
        return results

    async def remove_download(
        self,
        external_id: str,
        delete_files: bool = False,
    ) -> bool:
        """Remove a download from queue or history."""
        log = logger.bind(external_id=external_id, delete_files=delete_files)
        log.info("sabnzbd_remove_download")

        # Try removing from queue first
        try:
            del_files = "delete" if delete_files else ""
            await self._request(
                {
                    "mode": "queue",
                    "name": "delete",
                    "value": external_id,
                    "del_files": del_files,
                }
            )
            log.info("sabnzbd_removed_from_queue")
            return True
        except SABnzbdError:
            pass

        # Try removing from history
        try:
            await self._request(
                {
                    "mode": "history",
                    "name": "delete",
                    "value": external_id,
                    "del_files": "1" if delete_files else "",
                }
            )
            log.info("sabnzbd_removed_from_history")
            return True
        except SABnzbdError:
            log.warning("sabnzbd_remove_failed", external_id=external_id)
            return False

    async def test_connection(self) -> ProviderHealthResult:
        """Verify connectivity by calling the SABnzbd version endpoint."""
        start = time.monotonic()
        try:
            data = await self._request({"mode": "version"})
            elapsed_ms = (time.monotonic() - start) * 1000
            version = data.get("version", "unknown")
            return ProviderHealthResult(
                healthy=True,
                message=f"SABnzbd v{version}",
                response_time_ms=elapsed_ms,
                details={"version": str(version)},
            )
        except SABnzbdError as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return ProviderHealthResult(
                healthy=False,
                message=f"SABnzbd error: {exc}",
                response_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.exception("sabnzbd_test_connection_failed")
            return ProviderHealthResult(
                healthy=False,
                message=f"Connection failed: {exc}",
                response_time_ms=elapsed_ms,
            )

    async def get_options(self) -> ClientOptions:
        """Fetch available categories from SABnzbd."""
        data = await self._request({"mode": "get_cats"})
        categories: list[str] = data.get("categories", ["Default"])
        if not categories:
            categories = ["Default"]
        return ClientOptions(categories=categories)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # -- mapping helpers ----------------------------------------------------

    @staticmethod
    def _map_queue_slot(slot: dict[str, Any]) -> DownloadStatus:
        """Map a SABnzbd queue slot to a DownloadStatus DTO."""
        raw_status = slot.get("status", "")

        # SABnzbd progress is a string like "45.2" (percentage). During
        # post-download phases this becomes phase progress for repair/extract.
        post_download_phases = {"Repairing", "Extracting", "Verifying", "Moving"}
        percentage = _safe_float(slot.get("percentage", 0))
        if raw_status in post_download_phases:
            progress = percentage / 100.0 if percentage else 1.0
        else:
            progress = percentage / 100.0 if percentage else 0.0

        # Size in MB
        mb_total = _safe_float(slot.get("mb", 0))
        size_bytes = int(mb_total * 1024 * 1024) if mb_total else None

        # Speed in KB/s from the slot's kbpersec or 0
        kbps = _safe_float(slot.get("kbpersec", 0))
        speed_bytes = int(kbps * 1024) if kbps else None

        # ETA: SABnzbd returns timeleft as "HH:MM:SS" or "0:00:00"
        eta_seconds = _parse_timeleft(slot.get("timeleft"))

        state = _map_queue_state(raw_status)

        # Pass the raw SABnzbd status as client_state so the UI can show
        # sub-phases like "Repairing", "Extracting", "Verifying"
        client_state = raw_status if raw_status != "Downloading" else None

        return DownloadStatus(
            external_id=slot.get("nzo_id", ""),
            title=slot.get("filename", "Unknown"),
            state=state,
            progress=progress,
            size_bytes=size_bytes,
            speed_bytes=speed_bytes,
            eta_seconds=eta_seconds,
            error_message=None,
            client_state=client_state,
        )

    @staticmethod
    def _map_history_slot(slot: dict[str, Any]) -> DownloadStatus:
        """Map a SABnzbd history slot to a DownloadStatus DTO.

        SABnzbd moves items to history during client-side finalization phases
        (Repairing, Extracting, Verifying, etc.) — not just on completion.
        These in-progress history entries are mapped to state="finalizing".
        """
        raw_status = slot.get("status", "")

        # Post-processing phases appear in history, not queue
        _history_post_phases = {
            "Repairing",
            "Extracting",
            "Verifying",
            "Moving",
            "Running",
            "QuickCheck",
        }

        if raw_status == "Completed":
            state = "completed"
            progress = 1.0
        elif raw_status == "Failed":
            state = "failed"
            progress = 0.0
        elif raw_status in _history_post_phases:
            # Download finished, client-side repair/extract/move still in progress.
            state = "finalizing"
            progress = 1.0
        else:
            state = raw_status.lower()
            progress = 0.0

        size_bytes = _safe_int(slot.get("bytes", 0))

        # Pass raw status as client_state for post-processing phases
        # so the UI can show "Extracting", "Verifying", etc.
        client_state = raw_status if raw_status in _history_post_phases else None

        return DownloadStatus(
            external_id=slot.get("nzo_id", ""),
            title=slot.get("name", "Unknown"),
            state=state,
            progress=progress,
            size_bytes=size_bytes,
            speed_bytes=None,
            eta_seconds=None,
            error_message=slot.get("fail_message") or None,
            downloaded_path=slot.get("storage") or None,
            client_state=client_state,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(value: Any) -> float:
    """Convert a value to float, returning 0.0 on failure."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(value: Any) -> int | None:
    """Convert a value to int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _parse_timeleft(timeleft: str | None) -> int | None:
    """Parse SABnzbd timeleft string (HH:MM:SS) to seconds."""
    if not timeleft:
        return None
    parts = timeleft.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
        return hours * 3600 + minutes * 60 + seconds
    except ValueError:
        return None


def _map_queue_state(status: str) -> str:
    """Map SABnzbd queue status string to normalized state."""
    mapping = {
        "Downloading": "downloading",
        "Paused": "paused",
        "Queued": "queued",
        "Idle": "queued",
        "Fetching": "downloading",
        "Grabbing": "downloading",
        "Propagating": "queued",
        "Repairing": "finalizing",
        "Extracting": "finalizing",
        "Verifying": "finalizing",
        "Moving": "finalizing",
        "Running": "finalizing",
        "QuickCheck": "finalizing",
    }
    return mapping.get(status, status.lower())
