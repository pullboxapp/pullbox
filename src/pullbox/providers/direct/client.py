"""Bounded authenticated HTTP client for external direct providers."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TypeVar
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from pullbox.providers.direct.contract import (
    DirectHealthResponse,
    DirectManifestResponse,
    DirectResolveRequest,
    DirectResolveResponse,
    DirectSearchRequest,
    DirectSearchResponse,
)
from pullbox.providers.direct.endpoint import (
    ProviderEndpointError,
    ProviderEndpointResolver,
    ValidatedProviderEndpoint,
    validate_provider_endpoint,
)

_ResponseModel = TypeVar("_ResponseModel", bound=BaseModel)
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MIN_TOKEN_LENGTH = 32
_PROVIDER_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,99}\Z")

logger = structlog.get_logger(__name__)


class DirectProviderClientError(RuntimeError):
    """A classified, redacted provider communication failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = (
            retry_after_seconds
            if isinstance(retry_after_seconds, int)
            and not isinstance(retry_after_seconds, bool)
            and 0 <= retry_after_seconds <= 86_400
            else None
        )


class DirectProviderClient:
    """Call the four-operation provider protocol through one safe boundary."""

    def __init__(
        self,
        *,
        endpoint: str,
        bearer_token: str,
        allow_private_http: bool = False,
        resolver: ProviderEndpointResolver | None = None,
        http_client: httpx.AsyncClient | None = None,
        request_timeout_seconds: float = 30.0,
        provider_id: str | None = None,
    ) -> None:
        if len(bearer_token) < _MIN_TOKEN_LENGTH:
            raise ValueError("Provider bearer token must contain at least 32 characters.")
        if request_timeout_seconds <= 0 or request_timeout_seconds > 300:
            raise ValueError("Provider request timeout must be between 0 and 300 seconds.")
        self._endpoint = endpoint
        self._bearer_token = bearer_token
        self._allow_private_http = allow_private_http
        self._resolver = resolver
        self._request_timeout_seconds = request_timeout_seconds
        self._provider_id = provider_id
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=5.0,
                read=request_timeout_seconds,
                write=10.0,
                pool=5.0,
            ),
            follow_redirects=False,
            trust_env=False,
        )

    def __repr__(self) -> str:
        return f"DirectProviderClient(endpoint={self._endpoint!r})"

    async def __aenter__(self) -> DirectProviderClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def manifest(self) -> DirectManifestResponse:
        return await self._request("GET", "/v1/manifest", DirectManifestResponse)

    async def validate_endpoint(self) -> ValidatedProviderEndpoint:
        """Return the current normalized endpoint after applying network policy."""
        try:
            return await validate_provider_endpoint(
                self._endpoint,
                allow_private_http=self._allow_private_http,
                resolver=self._resolver,
            )
        except ProviderEndpointError as exc:
            raise DirectProviderClientError("provider_endpoint_rejected", str(exc)) from exc

    async def health(self) -> DirectHealthResponse:
        return await self._request("GET", "/v1/health", DirectHealthResponse)

    async def search(self, request: DirectSearchRequest) -> DirectSearchResponse:
        response = await self._request(
            "POST",
            "/v1/search",
            DirectSearchResponse,
            request=request,
        )
        if response.request_id != request.request_id:
            raise DirectProviderClientError(
                "provider_response_identity_mismatch",
                "Provider search response did not match the request.",
            )
        return response

    async def resolve(self, request: DirectResolveRequest) -> DirectResolveResponse:
        response = await self._request(
            "POST",
            "/v1/resolve",
            DirectResolveResponse,
            request=request,
        )
        if response.request_id != request.request_id:
            raise DirectProviderClientError(
                "provider_response_identity_mismatch",
                "Provider resolve response did not match the request.",
            )
        return response

    async def _request(
        self,
        method: str,
        path: str,
        response_model: type[_ResponseModel],
        *,
        request: BaseModel | None = None,
    ) -> _ResponseModel:
        operation = path.rsplit("/", maxsplit=1)[-1]
        started_at = time.monotonic()
        context = self._log_context(operation, request)
        try:
            response = await self._perform_request(
                method,
                path,
                response_model,
                request=request,
            )
        except asyncio.CancelledError:
            logger.info(
                "direct_provider_request_cancelled",
                **context,
                duration_ms=round((time.monotonic() - started_at) * 1000, 2),
            )
            raise
        except DirectProviderClientError as exc:
            logger.warning(
                "direct_provider_request_failed",
                **context,
                duration_ms=round((time.monotonic() - started_at) * 1000, 2),
                failure_code=exc.code,
                retryable=exc.retryable,
            )
            raise

        response_provider_id = getattr(response, "provider_id", None)
        if isinstance(response_provider_id, str):
            self._provider_id = response_provider_id
            context["provider_id"] = response_provider_id
        logger.info(
            "direct_provider_request_completed",
            **context,
            duration_ms=round((time.monotonic() - started_at) * 1000, 2),
            protocol_version=getattr(response, "protocol_version", None),
            result_count=_response_result_count(response),
        )
        return response

    def _log_context(
        self,
        operation: str,
        request: BaseModel | None,
    ) -> dict[str, str | None]:
        request_id = getattr(request, "request_id", None)
        return {
            "operation": operation,
            "provider_id": self._provider_id,
            "request_id": str(request_id) if request_id is not None else None,
        }

    async def _perform_request(
        self,
        method: str,
        path: str,
        response_model: type[_ResponseModel],
        *,
        request: BaseModel | None = None,
    ) -> _ResponseModel:
        endpoint = await self.validate_endpoint()
        request_url, host_header = _pinned_request_target(endpoint, path)

        headers = {
            "Authorization": f"Bearer {self._bearer_token}",
            "Accept": "application/json",
            "Host": host_header,
        }
        extensions = {"sni_hostname": endpoint.host} if not endpoint.insecure_transport else None
        payload = request.model_dump(mode="json") if request is not None else None
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                async with self._http_client.stream(
                    method,
                    request_url,
                    headers=headers,
                    json=payload,
                    extensions=extensions,
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise DirectProviderClientError(
                            "provider_redirect_rejected",
                            "Provider redirects are not permitted.",
                        )
                    content = await _read_bounded_response(response)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise DirectProviderClientError(
                "provider_timed_out",
                "Provider request timed out.",
                retryable=True,
            ) from exc
        except httpx.TimeoutException as exc:
            raise DirectProviderClientError(
                "provider_timed_out",
                "Provider request timed out.",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise DirectProviderClientError(
                "provider_unavailable",
                "Provider request failed.",
                retryable=True,
            ) from exc

        remote_failure = _safe_provider_error(content)
        if remote_failure is not None:
            remote_code, message, retryable, retry_after_seconds = remote_failure
            raise DirectProviderClientError(
                remote_code,
                message,
                retryable=retryable,
                retry_after_seconds=retry_after_seconds,
            )
        if response.status_code == 401:
            raise DirectProviderClientError(
                "provider_authentication_failed",
                "Provider rejected its bearer token.",
            )
        if response.status_code == 408:
            raise DirectProviderClientError(
                "provider_timed_out",
                "Provider request deadline elapsed.",
                retryable=True,
            )
        if response.status_code == 409:
            raise DirectProviderClientError(
                "provider_incompatible",
                "Provider protocol is incompatible.",
            )
        if response.status_code >= 400:
            raise DirectProviderClientError(
                "provider_request_failed",
                f"Provider returned HTTP {response.status_code}.",
                retryable=response.status_code >= 500,
            )
        try:
            decoded = json.loads(content)
            return response_model.model_validate(decoded)
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
            raise DirectProviderClientError(
                "provider_malformed_response",
                "Provider returned an invalid response.",
            ) from exc


def _response_result_count(response: BaseModel) -> int | None:
    candidates = getattr(response, "candidates", None)
    if isinstance(candidates, list):
        return len(candidates)
    artifacts = getattr(response, "artifacts", None)
    if isinstance(artifacts, list):
        return len(artifacts)
    return None


def _safe_provider_error(content: bytes) -> tuple[str, str, bool, int | None] | None:
    try:
        decoded = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, dict) or not isinstance(decoded.get("error"), dict):
        return None
    code = decoded["error"].get("code")
    if not isinstance(code, str) or _PROVIDER_ERROR_CODE.fullmatch(code) is None:
        return None
    safe_failures = {
        "source_quota_limited": ("Provider source quota is unavailable.", False),
        "source_authentication_required": (
            "Provider source authentication is required.",
            False,
        ),
        "source_unavailable": ("Provider source is temporarily unavailable.", True),
        "source_malformed_response": ("Provider source returned an invalid response.", False),
        "source_contract_changed": (
            "Provider source layout no longer matches its supported contract.",
            False,
        ),
        "candidate_not_found": ("Provider candidate is no longer available.", False),
        "browser_challenge_required": (
            "Provider source access requires browser challenge handling.",
            True,
        ),
    }
    raw_retry_after = decoded["error"].get("retry_after_seconds")
    retry_after_seconds = (
        raw_retry_after
        if isinstance(raw_retry_after, int)
        and not isinstance(raw_retry_after, bool)
        and 0 <= raw_retry_after <= 86_400
        else None
    )
    if code in safe_failures:
        message, retryable = safe_failures[code]
        return code, message, retryable, retry_after_seconds
    if code.startswith("resolver_"):
        return code, "Provider browser resolver attempt failed.", True, retry_after_seconds
    return None


async def _read_bounded_response(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > _MAX_RESPONSE_BYTES:
            raise DirectProviderClientError(
                "provider_response_too_large",
                "Provider response exceeded the 2 MiB limit.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _pinned_request_target(
    endpoint: ValidatedProviderEndpoint,
    path: str,
) -> tuple[str, str]:
    """Connect to a validated IP while preserving virtual-host and TLS identity."""
    scheme = urlsplit(endpoint.url).scheme
    default_port = 443 if scheme == "https" else 80

    address = endpoint.addresses[0]
    address_host = f"[{address}]" if ":" in address else address
    original_host = f"[{endpoint.host}]" if ":" in endpoint.host else endpoint.host
    if endpoint.port != default_port:
        address_host = f"{address_host}:{endpoint.port}"
        original_host = f"{original_host}:{endpoint.port}"

    return urlunsplit((scheme, address_host, path, "", "")), original_host
