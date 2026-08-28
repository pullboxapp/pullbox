"""Bounded asynchronous REST client for the AirDC++ Web API."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from pullbox.core.url_validation import normalize_peer_base_url
from pullbox.providers.airdcpp.contracts import (
    AirDcppAuthenticationInfo,
    AirDcppConnectivityInfo,
    AirDcppHub,
    AirDcppQueueBundle,
    AirDcppQueueBundleAddInfo,
    AirDcppQueueFile,
    AirDcppSearchDownloadResponse,
    AirDcppSearchInstance,
    AirDcppSearchResult,
    AirDcppSession,
    AirDcppSystemInfo,
)
from pullbox.providers.airdcpp.errors import (
    AirDcppAuthenticationError,
    AirDcppConflictError,
    AirDcppEntityNotFoundError,
    AirDcppPermissionError,
    AirDcppRateLimitError,
    AirDcppResponseError,
    AirDcppUnavailableError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_ModelT = TypeVar("_ModelT", bound=BaseModel)

_KNOWN_PERMISSIONS = frozenset(
    {
        "admin",
        "download",
        "search",
        "transfers",
        "hubs_view",
        "hubs_edit",
        "hubs_send",
        "queue_edit",
        "queue_view",
        "settings_view",
        "settings_edit",
    }
)
_READ_ONLY_SETTING_KEYS = frozenset({"min_search_interval"})
_PERMISSION_PATTERN = re.compile(r"permission\s+([a-z_]+)\s+is\s+required", re.IGNORECASE)


class AirDcppApiClient:
    """One reusable session-oriented AirDC++ REST transport."""

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout_seconds: float,
        max_response_bytes: int = 1_048_576,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized = normalize_peer_base_url(base_url, reject_query_or_fragment=True)
        self._api_base_url = f"{normalized.rstrip('/')}/api/v1"
        self._username = SecretStr(username)
        self._password = SecretStr(password)
        self._auth_token: SecretStr | None = None
        self._max_response_bytes = max_response_bytes
        self._max_connections = 4
        self._max_keepalive_connections = 2
        timeout = httpx.Timeout(timeout_seconds)
        self._client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=self._max_connections,
                max_keepalive_connections=self._max_keepalive_connections,
            ),
            follow_redirects=False,
            transport=transport,
        )

    @property
    def timeout(self) -> httpx.Timeout:
        return self._client.timeout

    @property
    def max_connections(self) -> int:
        return self._max_connections

    @property
    def max_keepalive_connections(self) -> int:
        return self._max_keepalive_connections

    async def authorize(self) -> AirDcppAuthenticationInfo:
        payload = await self._request_json(
            "POST",
            "/sessions/authorize",
            json={
                "username": self._username.get_secret_value(),
                "password": self._password.get_secret_value(),
            },
            authenticated=False,
        )
        auth = self._validate(AirDcppAuthenticationInfo, payload)
        self._auth_token = auth.auth_token
        return auth

    async def get_current_session(self) -> AirDcppSession:
        return self._validate(
            AirDcppSession,
            await self._request_json("GET", "/sessions/self"),
        )

    async def delete_current_session(self) -> None:
        try:
            await self._request_no_content("DELETE", "/sessions/self")
        finally:
            self._auth_token = None

    async def get_system_info(self) -> AirDcppSystemInfo:
        return self._validate(
            AirDcppSystemInfo,
            await self._request_json("GET", "/system/system_info"),
        )

    async def get_hubs(self) -> list[AirDcppHub]:
        payload = await self._request_json("GET", "/hubs")
        if not isinstance(payload, list):
            raise AirDcppResponseError("AirDC++ returned an invalid hubs response")
        return [self._validate(AirDcppHub, item) for item in payload]

    async def get_connectivity_status(self) -> AirDcppConnectivityInfo:
        return self._validate(
            AirDcppConnectivityInfo,
            await self._request_json("GET", "/connectivity/status"),
        )

    async def get_settings(self, keys: Sequence[str]) -> list[str | bool | int]:
        requested = list(keys)
        if not requested or any(key not in _READ_ONLY_SETTING_KEYS for key in requested):
            raise ValueError("Unsupported AirDC++ read-only setting key")
        payload = await self._request_json(
            "POST",
            "/settings/get",
            json={"keys": requested},
        )
        if not isinstance(payload, dict) or set(payload) != set(requested):
            raise AirDcppResponseError("AirDC++ returned an invalid settings response")
        values = [payload[key] for key in requested]
        if any(type(value) not in {str, bool, int} for value in values):
            raise AirDcppResponseError("AirDC++ returned an invalid settings value")
        return values

    async def get_queue_bundles(self, *, start: int, count: int) -> list[AirDcppQueueBundle]:
        if start < 0 or not 1 <= count <= 1000:
            raise ValueError("Invalid AirDC++ queue page")
        payload = await self._request_json("GET", f"/queue/bundles/{start}/{count}")
        if not isinstance(payload, list):
            raise AirDcppResponseError("AirDC++ returned an invalid queue response")
        return [self._validate(AirDcppQueueBundle, item) for item in payload]

    async def get_queue_bundle(self, bundle_id: int) -> AirDcppQueueBundle:
        if bundle_id <= 0:
            raise ValueError("Invalid AirDC++ bundle ID")
        return self._validate(
            AirDcppQueueBundle,
            await self._request_json("GET", f"/queue/bundles/{bundle_id}"),
        )

    async def get_queue_files_by_tth(self, tth: str) -> list[AirDcppQueueFile]:
        if not re.fullmatch(r"[A-Z2-7]{39}", tth):
            raise ValueError("Invalid AirDC++ TTH")
        payload = await self._request_json("GET", f"/queue/files/{tth}")
        if not isinstance(payload, list) or len(payload) > 100:
            raise AirDcppResponseError("AirDC++ returned an invalid queue-file response")
        return [self._validate(AirDcppQueueFile, item) for item in payload]

    async def download_search_result(
        self,
        instance_id: int,
        result_id: str,
        *,
        target_name: str,
        priority: int | None,
    ) -> AirDcppQueueBundleAddInfo:
        if instance_id <= 0 or not 1 <= len(result_id) <= 1000:
            raise ValueError("Invalid AirDC++ search route")
        if not target_name or len(target_name) > 255 or "/" in target_name or "\\" in target_name:
            raise ValueError("Invalid AirDC++ target name")
        if priority is not None and not -1 <= priority <= 6:
            raise ValueError("Invalid AirDC++ queue priority")
        body: dict[str, object] = {"target_name": target_name}
        if priority is not None:
            body["priority"] = priority
        payload = await self._request_json(
            "POST",
            f"/search/{instance_id}/results/{quote(result_id, safe='')}/download",
            json=body,
        )
        response = self._validate(AirDcppSearchDownloadResponse, payload)
        if response.bundle_info is None:  # pragma: no cover - enforced by model validation
            raise AirDcppResponseError("AirDC++ returned an invalid file download response")
        return response.bundle_info

    async def create_file_bundle(
        self,
        *,
        tth: str,
        size: int,
        target_name: str,
        priority: int | None,
    ) -> AirDcppQueueBundleAddInfo:
        if not re.fullmatch(r"[A-Z2-7]{39}", tth) or size <= 0:
            raise ValueError("Invalid AirDC++ file identity")
        if not target_name or len(target_name) > 255 or "/" in target_name or "\\" in target_name:
            raise ValueError("Invalid AirDC++ target name")
        if priority is not None and not -1 <= priority <= 6:
            raise ValueError("Invalid AirDC++ queue priority")
        body: dict[str, object] = {
            "tth": tth,
            "size": size,
            "target_name": target_name,
        }
        if priority is not None:
            body["priority"] = priority
        return self._validate(
            AirDcppQueueBundleAddInfo,
            await self._request_json("POST", "/queue/bundles/file", json=body),
        )

    async def search_queue_bundle(self, bundle_id: int) -> None:
        if bundle_id <= 0:
            raise ValueError("Invalid AirDC++ bundle ID")
        await self._request_no_content("POST", f"/queue/bundles/{bundle_id}/search")

    async def remove_queue_bundle(self, bundle_id: int) -> None:
        if bundle_id <= 0:
            raise ValueError("Invalid AirDC++ bundle ID")
        response = await self._request(
            "POST",
            f"/queue/bundles/{bundle_id}/remove",
            json={"remove_finished": False},
        )
        if response.status_code != 204:
            raise AirDcppResponseError("AirDC++ returned an unexpected response")

    async def create_search_instance(
        self,
        *,
        expiration_minutes: int,
        owner_suffix: str,
    ) -> AirDcppSearchInstance:
        if not 1 <= expiration_minutes <= 60 or not re.fullmatch(r"[a-z0-9_-]{1,32}", owner_suffix):
            raise ValueError("Invalid AirDC++ search instance options")
        return self._validate(
            AirDcppSearchInstance,
            await self._request_json(
                "POST",
                "/search",
                json={
                    "expiration": expiration_minutes,
                    "owner_suffix": owner_suffix,
                },
            ),
        )

    async def get_search_instance(self, instance_id: int) -> AirDcppSearchInstance:
        if instance_id <= 0:
            raise ValueError("Invalid AirDC++ search instance ID")
        return self._validate(
            AirDcppSearchInstance,
            await self._request_json("GET", f"/search/{instance_id}"),
        )

    async def get_search_results(
        self,
        instance_id: int,
        *,
        start: int,
        count: int,
    ) -> list[AirDcppSearchResult]:
        if instance_id <= 0 or start < 0 or not 1 <= count <= 100:
            raise ValueError("Invalid AirDC++ search result page")
        payload = await self._request_json(
            "GET",
            f"/search/{instance_id}/results/{start}/{count}",
        )
        if not isinstance(payload, list):
            raise AirDcppResponseError("AirDC++ returned an invalid search results response")
        return [self._validate(AirDcppSearchResult, item) for item in payload]

    async def delete_search_instance(self, instance_id: int) -> None:
        if instance_id <= 0:
            raise ValueError("Invalid AirDC++ search instance ID")
        await self._request_no_content("DELETE", f"/search/{instance_id}")

    async def aclose(self) -> None:
        self._auth_token = None
        await self._client.aclose()

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: object | None = None,
        authenticated: bool = True,
    ) -> Any:
        response = await self._request(
            method,
            path,
            json=json,
            authenticated=authenticated,
        )
        if not response.content:
            raise AirDcppResponseError("AirDC++ returned an empty response")
        try:
            return response.json()
        except ValueError as exc:
            raise AirDcppResponseError("AirDC++ returned malformed JSON") from exc

    async def _request_no_content(self, method: str, path: str) -> None:
        response = await self._request(method, path)
        if response.status_code != 204:
            raise AirDcppResponseError("AirDC++ returned an unexpected response")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: object | None = None,
        authenticated: bool = True,
    ) -> httpx.Response:
        headers: dict[str, str] = {"Accept": "application/json"}
        if authenticated:
            if self._auth_token is None:
                raise AirDcppAuthenticationError
            headers["Authorization"] = f"Bearer {self._auth_token.get_secret_value()}"
        try:
            async with self._client.stream(
                method,
                f"{self._api_base_url}{path}",
                headers=headers,
                json=json,
            ) as streamed_response:
                content = bytearray()
                async for chunk in streamed_response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self._max_response_bytes:
                        raise AirDcppResponseError(
                            "AirDC++ response exceeded the configured size limit"
                        )
                response = httpx.Response(
                    streamed_response.status_code,
                    headers=streamed_response.headers,
                    content=bytes(content),
                    request=streamed_response.request,
                    extensions=streamed_response.extensions,
                )
        except httpx.HTTPError as exc:
            raise AirDcppUnavailableError from exc

        self._raise_for_status(response)
        return response

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if 300 <= status < 400:
            raise AirDcppResponseError("AirDC++ redirects are not accepted")
        if status == 401:
            raise AirDcppAuthenticationError
        if status == 403:
            raise AirDcppPermissionError(self._safe_missing_permission(response))
        if status == 404:
            raise AirDcppEntityNotFoundError
        if status in {409, 422}:
            raise AirDcppConflictError
        if status == 429:
            raise AirDcppRateLimitError(self._safe_retry_after(response))
        if status >= 500:
            raise AirDcppUnavailableError
        raise AirDcppResponseError("AirDC++ rejected the request")

    def _safe_missing_permission(self, response: httpx.Response) -> str | None:
        if len(response.content) > self._max_response_bytes:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        message: object = payload.get("message") if isinstance(payload, dict) else None
        if message is None and isinstance(payload, dict):
            error = payload.get("error")
            message = error.get("message") if isinstance(error, dict) else None
        if not isinstance(message, str):
            return None
        match = _PERMISSION_PATTERN.search(message)
        if not match:
            return None
        permission = match.group(1).lower()
        return permission if permission in _KNOWN_PERMISSIONS else None

    @staticmethod
    def _safe_retry_after(response: httpx.Response) -> int | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if 0 <= parsed <= 3600 else None

    @staticmethod
    def _validate(model: type[_ModelT], payload: object) -> _ModelT:
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            errors = exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
            first_error = errors[0] if errors else None
            location = ".".join(
                str(part)
                for part in (first_error["loc"] if first_error is not None else ())
                if isinstance(part, int)
                or (isinstance(part, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", part))
            )
            error_type = first_error["type"] if first_error is not None else "validation_error"
            if not isinstance(error_type, str) or not re.fullmatch(r"[a-z0-9_]{1,100}", error_type):
                error_type = "validation_error"
            detail = f" ({location}: {error_type})" if location else ""
            raise AirDcppResponseError(
                f"AirDC++ returned an incompatible {model.__name__} response{detail}"
            ) from exc
