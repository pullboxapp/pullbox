"""Validated, resumable HTTP streaming into app-owned quarantine."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import httpx

from pullbox.models.direct_acquisition import DirectArtifactFailureClass
from pullbox.providers.artifact_hosts.contract import (
    ArtifactHostResolutionError,
    ResolvedTransfer,
)
from pullbox.providers.artifact_hosts.helpers import (
    filename_from_content_disposition,
    filename_from_url,
)
from pullbox.providers.artifact_hosts.http import (
    ArtifactUrlResolver,
    pinned_request_target,
    validate_artifact_url,
)
from pullbox.providers.artifact_hosts.quarantine import (
    open_quarantine_file,
    remove_quarantine_file,
    validate_quarantine_file,
)
from pullbox.providers.artifact_hosts.transfer_safety import (
    DiskFreeProvider,
    artifact_too_large_error,
    check_disk_budget,
    disk_free_bytes,
    disk_space_insufficient_error,
    parse_checksum,
    quarantine_write_failed_error,
    validate_expected_size,
    verify_checksum,
)
from pullbox.providers.artifact_hosts.transport_contract import (
    ArtifactTransferCancelledError,
    ArtifactTransferError,
    ArtifactTransferPausedError,
    ArtifactTransferPolicy,
    ArtifactTransferResult,
    HttpTransferCheckpoint,
    TransferProgressSnapshot,
)

if TYPE_CHECKING:
    from pathlib import Path
    from typing import BinaryIO

ProgressCallback = Callable[[TransferProgressSnapshot], Awaitable[None] | None]
RefreshTransfer = Callable[[], Awaitable[ResolvedTransfer]]
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_EXPIRED_URL_STATUSES = frozenset({401, 403, 410})
_SENSITIVE_HEADER_NAMES = frozenset({"authorization", "cookie", "proxy-authorization"})
_SINGLE_RESPONSE_CHUNK_SIZE_BYTES = 64 * 1024
__all__ = [
    "ArtifactTransferCancelledError",
    "ArtifactTransferError",
    "ArtifactTransferPausedError",
    "ArtifactTransferPolicy",
    "ArtifactTransferResult",
    "HttpArtifactTransport",
    "HttpTransferCheckpoint",
    "TransferProgressSnapshot",
]


class HttpArtifactTransport:
    """Stream one resolved HTTPS artifact with bounded restart-safe behavior."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        resolver: ArtifactUrlResolver | None = None,
        policy: ArtifactTransferPolicy | None = None,
        disk_free_provider: DiskFreeProvider | None = None,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._policy = policy or ArtifactTransferPolicy()
        self._disk_free_provider = disk_free_provider or disk_free_bytes

    async def transfer(
        self,
        *,
        resolved: ResolvedTransfer,
        destination: Path,
        quarantine_root: Path,
        checkpoint: HttpTransferCheckpoint | None = None,
        progress_callback: ProgressCallback | None = None,
        cancel_event: asyncio.Event | None = None,
        pause_event: asyncio.Event | None = None,
        refresh_transfer: RefreshTransfer | None = None,
    ) -> ArtifactTransferResult:
        """Stream a resolved transfer, preserving only safe retry checkpoints."""
        safe_path = validate_quarantine_file(
            destination,
            quarantine_root,
            allow_existing=checkpoint is not None,
        )
        validate_expected_size(resolved.expected_size, self._policy)
        parse_checksum(resolved.checksum)
        _validate_checkpoint(safe_path, checkpoint)
        check_disk_budget(
            self._disk_free_provider,
            quarantine_root,
            expected_size=resolved.expected_size,
            existing_size=checkpoint.bytes_transferred if checkpoint else 0,
            policy=self._policy,
        )

        active = resolved
        if _is_expired(active):
            refreshed_transfer = await _refresh_or_raise(refresh_transfer)
            _validate_refreshed_identity(resolved, refreshed_transfer)
            active = refreshed_transfer
            validate_expected_size(active.expected_size, self._policy)

        try:
            if _use_bounded_ranges(active, self._policy):
                result = await self._transfer_bounded_ranges(
                    resolved=active,
                    destination=safe_path,
                    quarantine_root=quarantine_root,
                    checkpoint=checkpoint,
                    progress_callback=progress_callback,
                    cancel_event=cancel_event,
                    pause_event=pause_event,
                    refresh_transfer=refresh_transfer,
                )
            else:
                result = await self._transfer_with_response_recovery(
                    resolved=active,
                    destination=safe_path,
                    quarantine_root=quarantine_root,
                    checkpoint=checkpoint,
                    progress_callback=progress_callback,
                    cancel_event=cancel_event,
                    pause_event=pause_event,
                    refresh_transfer=refresh_transfer,
                )
            await verify_checksum(safe_path, active.checksum)
            return result
        except ArtifactTransferCancelledError:
            remove_quarantine_file(safe_path)
            raise
        except ArtifactTransferPausedError:
            raise
        except ArtifactTransferError as exc:
            if not exc.retryable:
                remove_quarantine_file(safe_path)
            raise
        except OSError as exc:
            remove_quarantine_file(safe_path)
            raise quarantine_write_failed_error() from exc
        except asyncio.CancelledError:
            # Process shutdown keeps a regular partial so restart recovery can
            # prove identity and resume or restart it safely.
            raise

    async def _transfer_with_response_recovery(
        self,
        *,
        resolved: ResolvedTransfer,
        destination: Path,
        quarantine_root: Path,
        checkpoint: HttpTransferCheckpoint | None,
        progress_callback: ProgressCallback | None,
        cancel_event: asyncio.Event | None,
        pause_event: asyncio.Event | None,
        refresh_transfer: RefreshTransfer | None,
    ) -> ArtifactTransferResult:
        resume_offset = _eligible_resume_offset(resolved, checkpoint)
        active = resolved
        refreshed = False
        restarted_bad_range = False
        stream_retries = 0
        started_at = time.monotonic()
        deadline = started_at + self._policy.total_timeout_seconds

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _timeout_error("artifact_transfer_total_timeout")
            try:
                response = await _await_with_cancel(
                    partial(
                        self._open_response,
                        active,
                        resume_offset=resume_offset,
                    ),
                    cancel_event,
                    timeout=remaining,
                )
            except TimeoutError as exc:
                raise _timeout_error("artifact_transfer_total_timeout") from exc
            try:
                if response.status_code in _EXPIRED_URL_STATUSES and refresh_transfer is not None:
                    if refreshed:
                        raise _http_status_error(response.status_code)
                    await response.aclose()
                    refreshed_transfer = await _refresh_or_raise(refresh_transfer)
                    _validate_refreshed_identity(resolved, refreshed_transfer)
                    active = refreshed_transfer
                    validate_expected_size(active.expected_size, self._policy)
                    resume_offset = _eligible_resume_offset(active, checkpoint)
                    refreshed = True
                    continue

                if resume_offset > 0 and not _valid_partial_response(
                    response,
                    resume_offset=resume_offset,
                    checkpoint=checkpoint,
                ):
                    if response.status_code == 200:
                        resume_offset = 0
                    elif not restarted_bad_range:
                        await response.aclose()
                        resume_offset = 0
                        restarted_bad_range = True
                        continue
                    else:
                        raise _object_changed_error()
                elif resume_offset == 0 and response.status_code != 200:
                    raise _http_status_error(response.status_code)

                try:
                    return await self._stream_response(
                        response=response,
                        resolved=active,
                        destination=destination,
                        quarantine_root=quarantine_root,
                        resume_offset=resume_offset,
                        progress_callback=progress_callback,
                        cancel_event=cancel_event,
                        pause_event=pause_event,
                        started_at=started_at,
                        deadline=deadline,
                    )
                except ArtifactTransferError as exc:
                    if not active.prefer_single_response or not _can_retry_range_failure(
                        exc,
                        resolved=active,
                        retries=stream_retries,
                        policy=self._policy,
                    ):
                        raise
                    partial_size = destination.stat().st_size if destination.exists() else 0
                    if partial_size < resume_offset or (
                        active.expected_size is not None and partial_size > active.expected_size
                    ):
                        raise _object_changed_error() from exc
                    made_progress = partial_size > resume_offset
                    stream_retries += 1
                    checkpoint = HttpTransferCheckpoint(
                        bytes_transferred=partial_size,
                        expected_size=active.expected_size,
                        etag=response.headers.get("etag") or active.etag,
                        last_modified=(
                            response.headers.get("last-modified") or active.last_modified
                        ),
                    )
                    resume_offset = _eligible_resume_offset(active, checkpoint)
                    if not made_progress:
                        await _wait_for_range_retry(self._policy, stream_retries)
                    continue
            finally:
                await response.aclose()

    async def _transfer_bounded_ranges(
        self,
        *,
        resolved: ResolvedTransfer,
        destination: Path,
        quarantine_root: Path,
        checkpoint: HttpTransferCheckpoint | None,
        progress_callback: ProgressCallback | None,
        cancel_event: asyncio.Event | None,
        pause_event: asyncio.Event | None,
        refresh_transfer: RefreshTransfer | None,
    ) -> ArtifactTransferResult:
        """Download a known-size artifact through finite sequential ranges."""
        total = resolved.expected_size
        if total is None:
            raise AssertionError("bounded range transfer requires a known size")
        initial_offset = _eligible_resume_offset(resolved, checkpoint)
        transferred = initial_offset
        active = resolved
        started_at = time.monotonic()
        deadline = started_at + self._policy.total_timeout_seconds
        etag = resolved.etag
        last_modified = resolved.last_modified
        filename = resolved.filename_hint or filename_from_url(resolved.url)
        range_request_bytes = _range_request_bytes(resolved, self._policy)
        range_chunk_size_bytes = _range_chunk_size_bytes(resolved, self._policy)
        range_open_timeout_seconds = _range_open_timeout_seconds(resolved, self._policy)
        range_idle_timeout_seconds = _range_idle_timeout_seconds(resolved, self._policy)
        range_retries = 0

        while transferred < total:
            range_end = min(total - 1, transferred + range_request_bytes - 1)
            try:
                response = await _await_with_cancel(
                    partial(
                        self._open_response,
                        active,
                        resume_offset=transferred,
                        range_end=range_end,
                        force_range=True,
                    ),
                    cancel_event,
                    timeout=range_open_timeout_seconds,
                )
            except (ArtifactTransferError, TimeoutError) as exc:
                error = (
                    exc
                    if isinstance(exc, ArtifactTransferError)
                    else _artifact_host_unavailable_error()
                )
                if not _can_retry_range_failure(
                    error,
                    resolved=resolved,
                    retries=range_retries,
                    policy=self._policy,
                ):
                    if isinstance(exc, ArtifactTransferError):
                        raise
                    raise error from exc
                range_retries += 1
                active = await _refresh_range_source(
                    original=resolved,
                    active=active,
                    refresh_transfer=refresh_transfer,
                    policy=self._policy,
                )
                await _wait_for_range_retry(self._policy, range_retries)
                continue
            try:
                if response.status_code in _EXPIRED_URL_STATUSES:
                    if (
                        refresh_transfer is None
                        or range_retries >= self._policy.range_stall_retries
                    ):
                        raise _http_status_error(response.status_code)
                    range_retries += 1
                    active = await _refresh_range_source(
                        original=resolved,
                        active=active,
                        refresh_transfer=refresh_transfer,
                        policy=self._policy,
                    )
                    await _wait_for_range_retry(self._policy, range_retries)
                    continue

                if response.status_code == 200:
                    # A host that stops honoring ranges is still safe to use from
                    # byte zero; truncate the partial and validate the full object.
                    return await self._stream_response(
                        response=response,
                        resolved=active,
                        destination=destination,
                        quarantine_root=quarantine_root,
                        resume_offset=0,
                        progress_callback=progress_callback,
                        cancel_event=cancel_event,
                        pause_event=pause_event,
                        started_at=started_at,
                        deadline=deadline,
                    )
                if not _valid_bounded_response(
                    response,
                    range_start=transferred,
                    range_end=range_end,
                    total=total,
                    etag=etag,
                    last_modified=last_modified,
                ):
                    raise _object_changed_error()

                etag = response.headers.get("etag") or etag
                last_modified = response.headers.get("last-modified") or last_modified
                filename = (
                    filename_from_content_disposition(response.headers.get("content-disposition"))
                    or filename
                )
                try:
                    transferred = await self._stream_bounded_response(
                        response=response,
                        destination=destination,
                        range_start=transferred,
                        range_end=range_end,
                        total=total,
                        etag=etag,
                        last_modified=last_modified,
                        progress_callback=progress_callback,
                        cancel_event=cancel_event,
                        pause_event=pause_event,
                        started_at=started_at,
                        deadline=deadline,
                        chunk_size_bytes=range_chunk_size_bytes,
                        idle_timeout_seconds=range_idle_timeout_seconds,
                    )
                except ArtifactTransferError as exc:
                    if not _can_retry_range_failure(
                        exc,
                        resolved=resolved,
                        retries=range_retries,
                        policy=self._policy,
                    ):
                        raise
                    partial_size = destination.stat().st_size if destination.exists() else 0
                    if partial_size < transferred or partial_size > range_end + 1:
                        raise _object_changed_error() from exc
                    transferred = partial_size
                    range_retries += 1
                    active = await _refresh_range_source(
                        original=resolved,
                        active=active,
                        refresh_transfer=refresh_transfer,
                        policy=self._policy,
                    )
                    await _wait_for_range_retry(self._policy, range_retries)
                    continue
                range_retries = 0
            finally:
                await response.aclose()

        await _emit_progress(
            progress_callback,
            transferred=transferred,
            total=total,
            started_at=started_at,
            now=time.monotonic(),
        )
        return ArtifactTransferResult(
            path=destination,
            bytes_transferred=transferred,
            expected_size=total,
            etag=etag,
            last_modified=last_modified,
            filename_hint=filename,
            resumed=initial_offset > 0,
        )

    async def _open_response(
        self,
        resolved: ResolvedTransfer,
        *,
        resume_offset: int,
        range_end: int | None = None,
        force_range: bool = False,
    ) -> httpx.Response:
        current_url = resolved.url
        credential_origin: tuple[str, int] | None = None
        for redirect_count in range(self._policy.max_redirects + 1):
            try:
                target = await validate_artifact_url(
                    current_url,
                    allowed_domains=resolved.allowed_domains or None,
                    resolver=self._resolver,
                )
            except ArtifactHostResolutionError as exc:
                raise _from_resolution_error(exc) from exc

            if credential_origin is None:
                credential_origin = (target.host, target.port)
            include_sensitive = (target.host, target.port) == credential_origin

            request_url, host_header = pinned_request_target(target)
            request_headers = {
                **{
                    name: value
                    for name, value in resolved.headers.items()
                    if include_sensitive or name.lower() not in _SENSITIVE_HEADER_NAMES
                },
                "Accept-Encoding": "identity",
                "Host": host_header,
            }
            if resume_offset > 0 or force_range:
                end = "" if range_end is None else str(range_end)
                request_headers["Range"] = f"bytes={resume_offset}-{end}"
                validator = resolved.etag or resolved.last_modified
                if validator:
                    request_headers["If-Range"] = validator
            request = self._client.build_request(
                "GET",
                request_url,
                headers=request_headers,
                extensions={"sni_hostname": target.host},
            )
            try:
                response = await self._client.send(
                    request,
                    stream=True,
                    follow_redirects=False,
                )
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, TimeoutError) as exc:
                raise _artifact_host_unavailable_error() from exc

            if response.status_code not in _REDIRECT_STATUSES:
                return response
            if redirect_count == self._policy.max_redirects:
                await response.aclose()
                raise ArtifactTransferError(
                    code="artifact_host_redirect_limit",
                    message="The artifact host returned too many redirects.",
                    failure_class=DirectArtifactFailureClass.TRANSIENT_HOST,
                    retryable=True,
                    intervention=False,
                )
            location = response.headers.get("location")
            await response.aclose()
            if not location:
                raise ArtifactTransferError(
                    code="artifact_host_contract_changed",
                    message="The artifact host response no longer matches its supported contract.",
                    failure_class=DirectArtifactFailureClass.PERMANENT_MIRROR,
                    retryable=False,
                    intervention=True,
                )
            current_url = urljoin(target.url, location)

        raise AssertionError("redirect loop must return or raise")

    async def _stream_bounded_response(
        self,
        *,
        response: httpx.Response,
        destination: Path,
        range_start: int,
        range_end: int,
        total: int,
        etag: str | None,
        last_modified: str | None,
        progress_callback: ProgressCallback | None,
        cancel_event: asyncio.Event | None,
        pause_event: asyncio.Event | None,
        started_at: float,
        deadline: float,
        chunk_size_bytes: int,
        idle_timeout_seconds: float,
    ) -> int:
        transferred = range_start
        last_progress_at = time.monotonic()
        last_progress_bytes = transferred
        handle: BinaryIO | None = None
        iterator = response.aiter_bytes(chunk_size_bytes).__aiter__()
        try:
            handle = open_quarantine_file(destination, append=range_start > 0)
            while True:
                checkpoint = HttpTransferCheckpoint(
                    bytes_transferred=transferred,
                    expected_size=total,
                    etag=etag,
                    last_modified=last_modified,
                )
                _raise_for_control(
                    cancel_event=cancel_event,
                    pause_event=pause_event,
                    checkpoint=checkpoint,
                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _timeout_error("artifact_transfer_total_timeout")
                try:
                    chunk = await _next_chunk_with_cancel(
                        iterator,
                        timeout=min(idle_timeout_seconds, remaining),
                        cancel_event=cancel_event,
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError as exc:
                    raise _timeout_error("artifact_transfer_idle_timeout") from exc
                except httpx.HTTPError as exc:
                    raise _artifact_host_unavailable_error() from exc
                if not chunk:
                    continue
                if transferred + len(chunk) > range_end + 1:
                    raise _object_changed_error()
                await asyncio.to_thread(handle.write, chunk)
                transferred += len(chunk)
                if transferred == range_end + 1:
                    # A finite range is complete once every promised byte is
                    # present; some artifact hosts keep the response open.
                    break
                now = time.monotonic()
                if (
                    now - last_progress_at >= self._policy.progress_interval_seconds
                    or transferred - last_progress_bytes >= self._policy.progress_bytes
                ):
                    await _emit_progress(
                        progress_callback,
                        transferred=transferred,
                        total=total,
                        started_at=started_at,
                        now=now,
                    )
                    last_progress_at = now
                    last_progress_bytes = transferred
        finally:
            if handle is not None:
                handle.close()
        if transferred != range_end + 1:
            raise _object_changed_error()
        await _emit_progress(
            progress_callback,
            transferred=transferred,
            total=total,
            started_at=started_at,
            now=time.monotonic(),
        )
        return transferred

    async def _stream_response(
        self,
        *,
        response: httpx.Response,
        resolved: ResolvedTransfer,
        destination: Path,
        quarantine_root: Path,
        resume_offset: int,
        progress_callback: ProgressCallback | None,
        cancel_event: asyncio.Event | None,
        pause_event: asyncio.Event | None,
        started_at: float,
        deadline: float,
    ) -> ArtifactTransferResult:
        total = _response_total(response, resume_offset=resume_offset)
        expected = total if total is not None else resolved.expected_size
        validate_expected_size(expected, self._policy)
        if (
            resolved.expected_size is not None
            and expected is not None
            and resolved.expected_size != expected
        ):
            raise _object_changed_error()
        free_at_start = check_disk_budget(
            self._disk_free_provider,
            quarantine_root,
            expected_size=expected,
            existing_size=resume_offset,
            policy=self._policy,
        )
        unknown_size_ceiling = (
            resume_offset + max(0, free_at_start - self._policy.min_free_bytes)
            if expected is None
            else None
        )

        transferred = resume_offset
        last_progress_at = time.monotonic()
        last_progress_bytes = transferred
        etag = response.headers.get("etag") or resolved.etag
        last_modified = response.headers.get("last-modified") or resolved.last_modified
        filename = (
            filename_from_content_disposition(response.headers.get("content-disposition"))
            or resolved.filename_hint
            or filename_from_url(resolved.url)
        )
        handle: BinaryIO | None = None
        iterator = response.aiter_bytes(
            _stream_chunk_size_bytes(resolved, self._policy)
        ).__aiter__()

        try:
            handle = open_quarantine_file(destination, append=resume_offset > 0)
            while True:
                _raise_for_control(
                    cancel_event=cancel_event,
                    pause_event=pause_event,
                    checkpoint=HttpTransferCheckpoint(
                        bytes_transferred=transferred,
                        expected_size=expected,
                        etag=etag,
                        last_modified=last_modified,
                    ),
                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _timeout_error("artifact_transfer_total_timeout")
                timeout = min(self._policy.idle_timeout_seconds, remaining)
                try:
                    chunk = await _next_chunk_with_cancel(
                        iterator,
                        timeout=timeout,
                        cancel_event=cancel_event,
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError as exc:
                    raise _timeout_error("artifact_transfer_idle_timeout") from exc
                except httpx.HTTPError as exc:
                    raise _artifact_host_unavailable_error() from exc

                _raise_for_control(
                    cancel_event=cancel_event,
                    pause_event=pause_event,
                    checkpoint=HttpTransferCheckpoint(
                        bytes_transferred=transferred,
                        expected_size=expected,
                        etag=etag,
                        last_modified=last_modified,
                    ),
                )
                if not chunk:
                    continue
                next_transferred = transferred + len(chunk)
                if next_transferred > self._policy.max_artifact_bytes:
                    raise artifact_too_large_error()
                if unknown_size_ceiling is not None:
                    if next_transferred > unknown_size_ceiling:
                        raise disk_space_insufficient_error()
                    check_disk_budget(
                        self._disk_free_provider,
                        quarantine_root,
                        expected_size=next_transferred,
                        existing_size=transferred,
                        policy=self._policy,
                    )
                await asyncio.to_thread(handle.write, chunk)
                transferred = next_transferred
                now = time.monotonic()
                if (
                    now - last_progress_at >= self._policy.progress_interval_seconds
                    or transferred - last_progress_bytes >= self._policy.progress_bytes
                ):
                    await _emit_progress(
                        progress_callback,
                        transferred=transferred,
                        total=expected,
                        started_at=started_at,
                        now=now,
                    )
                    last_progress_at = now
                    last_progress_bytes = transferred
        finally:
            if handle is not None:
                handle.close()

        if expected is not None and transferred != expected:
            raise _object_changed_error()
        await _emit_progress(
            progress_callback,
            transferred=transferred,
            total=expected,
            started_at=started_at,
            now=time.monotonic(),
        )
        return ArtifactTransferResult(
            path=destination,
            bytes_transferred=transferred,
            expected_size=expected,
            etag=etag,
            last_modified=last_modified,
            filename_hint=filename,
            resumed=resume_offset > 0,
        )


def _use_bounded_ranges(
    resolved: ResolvedTransfer,
    policy: ArtifactTransferPolicy,
) -> bool:
    return bool(
        not resolved.prefer_single_response
        and resolved.range_supported
        and resolved.expected_size is not None
        and resolved.expected_size > _range_request_bytes(resolved, policy)
    )


def _stream_chunk_size_bytes(
    resolved: ResolvedTransfer,
    policy: ArtifactTransferPolicy,
) -> int:
    if resolved.prefer_single_response:
        return min(policy.chunk_size_bytes, _SINGLE_RESPONSE_CHUNK_SIZE_BYTES)
    return policy.chunk_size_bytes


def _range_request_bytes(
    resolved: ResolvedTransfer,
    policy: ArtifactTransferPolicy,
) -> int:
    if resolved.checksum and not (resolved.etag or resolved.last_modified):
        return min(policy.range_request_bytes, policy.checksum_range_request_bytes)
    return policy.range_request_bytes


def _range_chunk_size_bytes(
    resolved: ResolvedTransfer,
    policy: ArtifactTransferPolicy,
) -> int:
    if _uses_checksum_range_policy(resolved):
        return min(policy.chunk_size_bytes, policy.checksum_range_chunk_size_bytes)
    return policy.chunk_size_bytes


def _range_idle_timeout_seconds(
    resolved: ResolvedTransfer,
    policy: ArtifactTransferPolicy,
) -> float:
    if _uses_checksum_range_policy(resolved):
        return min(
            policy.idle_timeout_seconds,
            policy.checksum_range_idle_timeout_seconds,
        )
    return policy.idle_timeout_seconds


def _range_open_timeout_seconds(
    resolved: ResolvedTransfer,
    policy: ArtifactTransferPolicy,
) -> float:
    if _uses_checksum_range_policy(resolved):
        return min(
            policy.idle_timeout_seconds,
            policy.checksum_range_open_timeout_seconds,
        )
    return policy.idle_timeout_seconds


def _uses_checksum_range_policy(resolved: ResolvedTransfer) -> bool:
    return bool(resolved.checksum and not (resolved.etag or resolved.last_modified))


def _can_retry_range_failure(
    error: ArtifactTransferError,
    *,
    resolved: ResolvedTransfer,
    retries: int,
    policy: ArtifactTransferPolicy,
) -> bool:
    return bool(
        error.code in {"artifact_host_unavailable", "artifact_transfer_idle_timeout"}
        and error.retryable
        and resolved.checksum
        and retries < policy.range_stall_retries
    )


async def _refresh_range_source(
    *,
    original: ResolvedTransfer,
    active: ResolvedTransfer,
    refresh_transfer: RefreshTransfer | None,
    policy: ArtifactTransferPolicy,
) -> ResolvedTransfer:
    if refresh_transfer is None:
        return active
    try:
        refreshed = await _refresh_or_raise(refresh_transfer)
    except ArtifactTransferError as exc:
        if exc.retryable:
            return active
        raise
    _validate_refreshed_identity(original, refreshed)
    validate_expected_size(refreshed.expected_size, policy)
    return refreshed


async def _wait_for_range_retry(
    policy: ArtifactTransferPolicy,
    retries: int,
) -> None:
    if policy.range_retry_backoff_seconds:
        await asyncio.sleep(policy.range_retry_backoff_seconds * retries)


async def _next_chunk_with_cancel(
    iterator: AsyncIterator[bytes],
    *,
    timeout: float,
    cancel_event: asyncio.Event | None,
) -> bytes:
    next_chunk = iterator.__anext__
    if cancel_event is None:
        return await asyncio.wait_for(next_chunk(), timeout=timeout)
    if cancel_event.is_set():
        raise ArtifactTransferCancelledError

    async def read_next_chunk() -> bytes:
        return await next_chunk()

    async def wait_until_cancelled() -> bytes:
        await cancel_event.wait()
        return b""

    read_task = asyncio.create_task(read_next_chunk())
    cancel_task = asyncio.create_task(wait_until_cancelled())
    try:
        done, _pending = await asyncio.wait(
            {read_task, cancel_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        read_task.cancel()
        cancel_task.cancel()
        await asyncio.gather(read_task, cancel_task, return_exceptions=True)
        raise
    if cancel_task in done:
        read_task.cancel()
        await asyncio.gather(read_task, return_exceptions=True)
        raise ArtifactTransferCancelledError
    if read_task not in done:
        read_task.cancel()
        cancel_task.cancel()
        await asyncio.gather(read_task, cancel_task, return_exceptions=True)
        raise TimeoutError

    cancel_task.cancel()
    await asyncio.gather(cancel_task, return_exceptions=True)
    return read_task.result()


async def _await_with_cancel[T](
    operation: Callable[[], Awaitable[T]],
    cancel_event: asyncio.Event | None,
    *,
    timeout: float | None = None,
) -> T:
    if cancel_event is None:
        if timeout is None:
            return await operation()
        return await asyncio.wait_for(operation(), timeout=timeout)
    if cancel_event.is_set():
        raise ArtifactTransferCancelledError

    async def run_operation() -> T:
        return await operation()

    operation_task = asyncio.create_task(run_operation())
    cancel_task = asyncio.create_task(cancel_event.wait())
    waiters: set[asyncio.Task[Any]] = {operation_task, cancel_task}
    try:
        done, _pending = await asyncio.wait(
            waiters,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        operation_task.cancel()
        cancel_task.cancel()
        await asyncio.gather(operation_task, cancel_task, return_exceptions=True)
        raise
    if not done:
        operation_task.cancel()
        cancel_task.cancel()
        await asyncio.gather(operation_task, cancel_task, return_exceptions=True)
        raise TimeoutError
    if cancel_task in done:
        operation_task.cancel()
        await asyncio.gather(operation_task, return_exceptions=True)
        raise ArtifactTransferCancelledError

    cancel_task.cancel()
    await asyncio.gather(cancel_task, return_exceptions=True)
    return operation_task.result()


def _eligible_resume_offset(
    resolved: ResolvedTransfer,
    checkpoint: HttpTransferCheckpoint | None,
) -> int:
    if checkpoint is None or checkpoint.bytes_transferred <= 0:
        return 0
    validator_matches = bool(
        (checkpoint.etag and checkpoint.etag == resolved.etag)
        or (checkpoint.last_modified and checkpoint.last_modified == resolved.last_modified)
    )
    checksum_protected = bool(
        resolved.checksum
        and resolved.expected_size is not None
        and checkpoint.expected_size == resolved.expected_size
    )
    return (
        checkpoint.bytes_transferred
        if resolved.range_supported and (validator_matches or checksum_protected)
        else 0
    )


def _valid_bounded_response(
    response: httpx.Response,
    *,
    range_start: int,
    range_end: int,
    total: int,
    etag: str | None,
    last_modified: str | None,
) -> bool:
    if response.status_code != 206:
        return False
    parsed = _parse_content_range(response.headers.get("content-range", ""))
    if parsed != (range_start, range_end, total):
        return False
    content_length = response.headers.get("content-length")
    if content_length and (
        not content_length.isdigit() or int(content_length) != range_end - range_start + 1
    ):
        return False
    response_etag = response.headers.get("etag")
    response_modified = response.headers.get("last-modified")
    if etag and response_etag and etag != response_etag:
        return False
    return not (last_modified and response_modified and last_modified != response_modified)


def _parse_content_range(value: str) -> tuple[int, int, int] | None:
    if not value.lower().startswith("bytes "):
        return None
    raw_range, separator, raw_total = value[6:].partition("/")
    if separator != "/" or not raw_total.isdigit():
        return None
    raw_start, dash, raw_end = raw_range.partition("-")
    if dash != "-" or not raw_start.isdigit() or not raw_end.isdigit():
        return None
    return int(raw_start), int(raw_end), int(raw_total)


def _valid_partial_response(
    response: httpx.Response,
    *,
    resume_offset: int,
    checkpoint: HttpTransferCheckpoint | None,
) -> bool:
    if response.status_code != 206 or checkpoint is None:
        return False
    content_range = response.headers.get("content-range", "")
    prefix = f"bytes {resume_offset}-"
    if not content_range.lower().startswith(prefix):
        return False
    response_etag = response.headers.get("etag")
    response_modified = response.headers.get("last-modified")
    if checkpoint.etag and response_etag and checkpoint.etag != response_etag:
        return False
    return not (
        checkpoint.last_modified
        and response_modified
        and checkpoint.last_modified != response_modified
    )


def _response_total(response: httpx.Response, *, resume_offset: int) -> int | None:
    content_range = response.headers.get("content-range", "")
    if "/" in content_range:
        raw_total = content_range.rsplit("/", 1)[1]
        if raw_total.isdigit():
            return int(raw_total)
    content_length = response.headers.get("content-length")
    if content_length and content_length.isdigit():
        return resume_offset + int(content_length)
    return None


def _validate_checkpoint(path: Path, checkpoint: HttpTransferCheckpoint | None) -> None:
    if checkpoint is None:
        return
    if checkpoint.bytes_transferred < 0:
        raise _object_changed_error()
    try:
        actual = path.stat().st_size
    except OSError as exc:
        raise _object_changed_error() from exc
    if actual != checkpoint.bytes_transferred:
        raise _object_changed_error()


def _raise_for_control(
    *,
    cancel_event: asyncio.Event | None,
    pause_event: asyncio.Event | None,
    checkpoint: HttpTransferCheckpoint,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ArtifactTransferCancelledError
    if pause_event is not None and pause_event.is_set():
        raise ArtifactTransferPausedError(checkpoint)


async def _emit_progress(
    callback: ProgressCallback | None,
    *,
    transferred: int,
    total: int | None,
    started_at: float,
    now: float,
) -> None:
    if callback is None:
        return
    elapsed = max(0.0, now - started_at)
    rate = transferred / elapsed if elapsed > 0 else None
    eta = (
        max(0.0, (total - transferred) / rate)
        if total is not None and rate and transferred <= total
        else None
    )
    percent = min(100, int((transferred * 100) / total)) if total and total > 0 else None
    result = callback(
        TransferProgressSnapshot(
            bytes_transferred=transferred,
            total_bytes=total,
            percent=percent,
            bytes_per_second=rate,
            eta_seconds=eta,
        )
    )
    if inspect.isawaitable(result):
        await result


async def _refresh_or_raise(refresh: RefreshTransfer | None) -> ResolvedTransfer:
    if refresh is None:
        raise ArtifactTransferError(
            code="artifact_url_expired",
            message="The artifact URL expired and must be resolved again.",
            failure_class=DirectArtifactFailureClass.TRANSIENT_HOST,
            retryable=True,
            intervention=False,
        )
    try:
        return await refresh()
    except ArtifactHostResolutionError as exc:
        raise _from_resolution_error(exc) from exc


def _validate_refreshed_identity(
    original: ResolvedTransfer,
    refreshed: ResolvedTransfer,
) -> None:
    parse_checksum(refreshed.checksum)
    if original.host_kind is not refreshed.host_kind:
        raise _object_changed_error()
    if original.expected_size is not None and refreshed.expected_size != original.expected_size:
        raise _object_changed_error()
    if original.checksum and refreshed.checksum != original.checksum:
        raise _object_changed_error()
    if original.range_supported and not refreshed.range_supported:
        raise _object_changed_error()


def _is_expired(resolved: ResolvedTransfer) -> bool:
    expires_at = resolved.expires_at
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC)


def _from_resolution_error(exc: ArtifactHostResolutionError) -> ArtifactTransferError:
    return ArtifactTransferError(
        code=exc.code,
        message=exc.message,
        failure_class=exc.failure_class,
        retryable=exc.retryable,
        intervention=exc.intervention,
    )


def _artifact_host_unavailable_error() -> ArtifactTransferError:
    return ArtifactTransferError(
        code="artifact_host_unavailable",
        message="The artifact host is temporarily unavailable.",
        failure_class=DirectArtifactFailureClass.TRANSIENT_HOST,
        retryable=True,
        intervention=False,
    )


def _object_changed_error() -> ArtifactTransferError:
    return ArtifactTransferError(
        code="artifact_object_changed",
        message="The remote artifact changed during transfer.",
        failure_class=DirectArtifactFailureClass.TRANSIENT_HOST,
        retryable=True,
        intervention=False,
    )


def _timeout_error(code: str) -> ArtifactTransferError:
    return ArtifactTransferError(
        code=code,
        message="The artifact transfer stopped making progress.",
        failure_class=DirectArtifactFailureClass.TRANSIENT_HOST,
        retryable=True,
        intervention=False,
    )


def _http_status_error(status_code: int) -> ArtifactTransferError:
    if status_code in _EXPIRED_URL_STATUSES:
        return ArtifactTransferError(
            code="artifact_url_expired",
            message="The artifact URL expired and must be resolved again.",
            failure_class=DirectArtifactFailureClass.TRANSIENT_HOST,
            retryable=True,
            intervention=False,
        )
    if status_code == 429:
        return ArtifactTransferError(
            code="artifact_host_rate_limited",
            message="The artifact host is temporarily rate limited.",
            failure_class=DirectArtifactFailureClass.TRANSIENT_HOST,
            retryable=True,
            intervention=False,
        )
    return ArtifactTransferError(
        code="artifact_host_download_failed",
        message="The artifact host did not return a downloadable file.",
        failure_class=DirectArtifactFailureClass.PERMANENT_MIRROR,
        retryable=500 <= status_code < 600,
        intervention=status_code < 500,
    )
