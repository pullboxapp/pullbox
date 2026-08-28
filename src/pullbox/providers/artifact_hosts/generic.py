"""Generic HTTPS adapter for URLs that already return a final file."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pullbox.models.direct_acquisition import (
    DirectArtifactFailureClass,
    DirectArtifactHostKind,
)
from pullbox.providers.artifact_hosts.contract import (
    ArtifactHostResolutionError,
    HostResolutionRequest,
    ResolvedTransfer,
    sanitize_provider_headers,
)
from pullbox.providers.artifact_hosts.helpers import (
    filename_from_content_disposition,
    filename_from_url,
    response_size,
    validate_resolution_request,
)
from pullbox.providers.artifact_hosts.http import request_bounded

if TYPE_CHECKING:
    from collections.abc import Mapping

    import httpx

    from pullbox.providers.artifact_hosts.contract import ArtifactResolutionProgressCallback
    from pullbox.providers.artifact_hosts.http import ArtifactUrlResolver

_HTML_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/xhtml+xml",
        "application/xml",
        "text/html",
    }
)
_GENERIC_HTTPS_USER_AGENT = "Mozilla/5.0 (compatible; Pullbox/1.1; +https://pullbox.app)"
_UNRELIABLE_RANGE_HOST_SUFFIXES = ("booksdl.lc",)


class GenericHttpsAdapter:
    """Probe an unknown HTTPS URL without treating a landing page as a file."""

    host_kind = DirectArtifactHostKind.GENERIC_HTTPS

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        resolver: ArtifactUrlResolver | None = None,
    ) -> None:
        self._http_client = http_client
        self._resolver = resolver

    async def resolve(
        self,
        request: HostResolutionRequest,
        *,
        credentials: Mapping[str, str],
        progress_callback: ArtifactResolutionProgressCallback | None = None,
    ) -> ResolvedTransfer:
        url = validate_resolution_request(
            request,
            expected_kind=self.host_kind,
            credentials=credentials,
        )
        provider_headers = sanitize_provider_headers(request.provider_headers)
        transfer_headers = {
            **provider_headers,
            "User-Agent": _GENERIC_HTTPS_USER_AGENT,
        }
        response = await request_bounded(
            self._http_client,
            "GET",
            url,
            resolver=self._resolver,
            allowed_domains=None,
            headers={
                **transfer_headers,
                "Accept": "application/octet-stream",
                "Range": "bytes=0-0",
            },
            read_body=False,
        )
        if (
            response.status_code == 403
            and response.headers.get("cf-mitigated", "").casefold() == "challenge"
        ):
            raise ArtifactHostResolutionError(
                code="artifact_host_challenge_required",
                message="The artifact host requires browser verification.",
                failure_class=DirectArtifactFailureClass.ARTIFACT_HOST_CHALLENGE,
                retryable=False,
                intervention=True,
                http_status=response.status_code,
            )
        if response.status_code in {404, 410}:
            raise ArtifactHostResolutionError(
                code="artifact_file_unavailable",
                message=(
                    "The selected file is no longer available at this secure download location. "
                    "Choose another search result."
                ),
                failure_class=DirectArtifactFailureClass.PERMANENT_MIRROR,
                retryable=False,
                intervention=True,
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            raise ArtifactHostResolutionError(
                code="artifact_host_unavailable",
                message="The final artifact URL is unavailable.",
                failure_class=DirectArtifactFailureClass.TRANSIENT_HOST,
                retryable=response.status_code >= 500 or response.status_code == 429,
                intervention=response.status_code < 500 and response.status_code != 429,
                http_status=response.status_code,
            )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type.startswith("text/") or content_type in _HTML_CONTENT_TYPES:
            raise ArtifactHostResolutionError(
                code="unsupported_landing_page",
                message="This URL is a landing page rather than a final downloadable file.",
                failure_class=DirectArtifactFailureClass.UNSUPPORTED_ARTIFACT_HOST,
                retryable=False,
                intervention=True,
            )
        return ResolvedTransfer(
            host_kind=self.host_kind,
            url=response.url,
            headers=transfer_headers,
            expected_size=response_size(response, request.expected_size),
            checksum=request.checksum,
            etag=response.headers.get("etag") or request.etag,
            last_modified=response.headers.get("last-modified") or request.last_modified,
            expires_at=request.expires_at,
            filename_hint=(
                filename_from_content_disposition(response.headers.get("content-disposition"))
                or filename_from_url(response.url)
            ),
            range_supported=(
                response.status_code == 206
                or response.headers.get("accept-ranges", "").lower() == "bytes"
            ),
            prefer_single_response=_has_unreliable_range_support(response.url),
            allowed_domains=((urlsplit(response.url).hostname or "").lower().rstrip("."),),
        )


def _has_unreliable_range_support(url: object) -> bool:
    host = (urlsplit(str(url)).hostname or "").lower().rstrip(".")
    return any(
        host == suffix or host.endswith(f".{suffix}") for suffix in _UNRELIABLE_RANGE_HOST_SUFFIXES
    )
