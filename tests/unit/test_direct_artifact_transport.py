from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from pullbox.models.direct_acquisition import (
    DirectArtifactFailureClass,
    DirectArtifactHostKind,
)
from pullbox.providers.artifact_hosts.contract import (
    ArtifactHostResolutionError,
    ResolvedTransfer,
)
from pullbox.providers.artifact_hosts.transport import (
    ArtifactTransferCancelledError,
    ArtifactTransferError,
    ArtifactTransferPausedError,
    ArtifactTransferPolicy,
    HttpArtifactTransport,
    HttpTransferCheckpoint,
    TransferProgressSnapshot,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


_PUBLIC_IP = "93.184.216.34"


@pytest.mark.asyncio
async def test_http_transport_streams_to_quarantine_and_reports_final_progress(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={
                "content-length": "6",
                "etag": '"v1"',
                "content-disposition": 'attachment; filename="issue.cbz"',
            },
            content=b"abcdef",
        )

    root, destination = _quarantine_paths(tmp_path)
    progress: list[TransferProgressSnapshot] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
            policy=ArtifactTransferPolicy(progress_interval_seconds=0),
        ).transfer(
            resolved=_resolved(expected_size=6),
            destination=destination,
            quarantine_root=root,
            progress_callback=progress.append,
        )

    assert destination.read_bytes() == b"abcdef"
    assert result.bytes_transferred == 6
    assert result.etag == '"v1"'
    assert result.filename_hint == "issue.cbz"
    assert result.resumed is False
    assert progress[-1].bytes_transferred == 6
    assert progress[-1].percent == 100
    assert requests[0].headers["host"] == "files.example.com"
    assert requests[0].url.host == _PUBLIC_IP


@pytest.mark.asyncio
async def test_http_transport_resumes_only_with_stable_validator(
    tmp_path: Path,
) -> None:
    seen_headers: list[httpx.Headers] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(
            206,
            headers={
                "content-range": "bytes 3-5/6",
                "content-length": "3",
                "etag": '"stable"',
            },
            content=b"def",
        )

    root, destination = _quarantine_paths(tmp_path)
    destination.write_bytes(b"abc")
    checkpoint = HttpTransferCheckpoint(
        bytes_transferred=3,
        expected_size=6,
        etag='"stable"',
        last_modified=None,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
        ).transfer(
            resolved=_resolved(expected_size=6, etag='"stable"', range_supported=True),
            destination=destination,
            quarantine_root=root,
            checkpoint=checkpoint,
        )

    assert destination.read_bytes() == b"abcdef"
    assert seen_headers[0]["range"] == "bytes=3-"
    assert seen_headers[0]["if-range"] == '"stable"'
    assert result.resumed is True


@pytest.mark.asyncio
async def test_http_transport_resumes_single_response_after_protocol_disconnect(
    tmp_path: Path,
) -> None:
    payload = b"abcdefghijkl"
    seen_ranges: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested = request.headers.get("range")
        seen_ranges.append(requested)
        if requested is None:
            return httpx.Response(
                200,
                headers={
                    "content-length": str(len(payload)),
                    "etag": '"stable"',
                },
                stream=_PartialThenProtocolErrorStream(payload[:4]),
            )
        assert requested == "bytes=4-"
        return httpx.Response(
            206,
            headers={
                "content-range": f"bytes 4-{len(payload) - 1}/{len(payload)}",
                "content-length": str(len(payload) - 4),
                "etag": '"stable"',
            },
            content=payload[4:],
        )

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
            policy=ArtifactTransferPolicy(
                chunk_size_bytes=4,
                range_retry_backoff_seconds=0,
            ),
        ).transfer(
            resolved=_resolved(
                expected_size=len(payload),
                etag='"stable"',
                checksum=f"md5:{hashlib.md5(payload, usedforsecurity=False).hexdigest()}",
                range_supported=True,
                prefer_single_response=True,
            ),
            destination=destination,
            quarantine_root=root,
        )

    assert seen_ranges == [None, "bytes=4-"]
    assert destination.read_bytes() == payload
    assert result.bytes_transferred == len(payload)


@pytest.mark.asyncio
async def test_http_transport_limits_retries_when_single_response_host_restarts_partial(
    tmp_path: Path,
) -> None:
    payload = b"abcdefghijkl"
    seen_ranges: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_ranges.append(request.headers.get("range"))
        return httpx.Response(
            200,
            headers={
                "content-length": str(len(payload)),
                "etag": '"stable"',
            },
            stream=_PartialThenProtocolErrorStream(payload[:4]),
        )

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArtifactTransferError) as captured:
            async with asyncio.timeout(0.5):
                await HttpArtifactTransport(
                    client=client,
                    resolver=_public_resolver,
                    policy=ArtifactTransferPolicy(
                        chunk_size_bytes=4,
                        range_stall_retries=2,
                        range_retry_backoff_seconds=0,
                    ),
                ).transfer(
                    resolved=_resolved(
                        expected_size=len(payload),
                        etag='"stable"',
                        checksum=f"md5:{hashlib.md5(payload, usedforsecurity=False).hexdigest()}",
                        range_supported=True,
                        prefer_single_response=True,
                    ),
                    destination=destination,
                    quarantine_root=root,
                )

    assert captured.value.code == "artifact_host_unavailable"
    assert seen_ranges == [None, "bytes=4-", "bytes=4-"]


@pytest.mark.asyncio
async def test_http_transport_reports_intermediate_progress_for_single_response_host(
    tmp_path: Path,
) -> None:
    payload = b"x" * (128 * 1024)
    snapshots: list[TransferProgressSnapshot] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-length": str(len(payload)),
                "etag": '"stable"',
            },
            content=payload,
        )

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
            policy=ArtifactTransferPolicy(progress_interval_seconds=0),
        ).transfer(
            resolved=_resolved(
                expected_size=len(payload),
                checksum=f"md5:{hashlib.md5(payload, usedforsecurity=False).hexdigest()}",
                range_supported=True,
                prefer_single_response=True,
            ),
            destination=destination,
            quarantine_root=root,
            progress_callback=snapshots.append,
        )

    assert any(0 < snapshot.bytes_transferred < len(payload) for snapshot in snapshots)


@pytest.mark.asyncio
async def test_http_transport_slices_large_range_capable_artifact(
    tmp_path: Path,
) -> None:
    payload = b"%PDF-1.7\nlarge synthetic fixture"
    seen_ranges: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested = request.headers.get("range")
        seen_ranges.append(requested)
        assert requested is not None
        start, end = _requested_range(requested)
        return httpx.Response(
            206,
            headers={
                "content-range": f"bytes {start}-{end}/{len(payload)}",
                "content-length": str(end - start + 1),
                "content-type": "application/pdf",
                "content-disposition": 'inline; filename="issue.pdf"',
            },
            content=payload[start : end + 1],
        )

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
            policy=ArtifactTransferPolicy(range_request_bytes=8),
        ).transfer(
            resolved=_resolved(
                expected_size=len(payload),
                range_supported=True,
                checksum=f"md5:{hashlib.md5(payload, usedforsecurity=False).hexdigest()}",
            ),
            destination=destination,
            quarantine_root=root,
        )

    assert seen_ranges == ["bytes=0-7", "bytes=8-15", "bytes=16-23", "bytes=24-31"]
    assert destination.read_bytes() == payload
    assert result.filename_hint == "issue.pdf"
    assert result.bytes_transferred == len(payload)


@pytest.mark.asyncio
async def test_http_transport_finishes_range_when_promised_bytes_arrive(
    tmp_path: Path,
) -> None:
    payload = b"abcdef"
    seen_ranges: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested = request.headers.get("range")
        seen_ranges.append(requested)
        assert requested is not None
        start, end = _requested_range(requested)
        return httpx.Response(
            206,
            headers={
                "content-range": f"bytes {start}-{end}/{len(payload)}",
                "content-length": str(end - start + 1),
            },
            stream=_CompleteThenStalledStream(payload[start : end + 1]),
        )

    root, destination = _quarantine_paths(tmp_path)
    checksum = f"md5:{hashlib.md5(payload, usedforsecurity=False).hexdigest()}"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
            policy=ArtifactTransferPolicy(
                chunk_size_bytes=3,
                range_request_bytes=3,
                checksum_range_request_bytes=3,
                idle_timeout_seconds=0.01,
                range_stall_retries=0,
            ),
        ).transfer(
            resolved=_resolved(
                expected_size=len(payload),
                range_supported=True,
                checksum=checksum,
            ),
            destination=destination,
            quarantine_root=root,
        )

    assert seen_ranges == ["bytes=0-2", "bytes=3-5"]
    assert destination.read_bytes() == payload
    assert result.bytes_transferred == len(payload)


@pytest.mark.asyncio
async def test_http_transport_uses_conservative_ranges_for_checksum_only_identity(
    tmp_path: Path,
) -> None:
    payload = b"abcdefghijkl"
    seen_ranges: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested = request.headers.get("range")
        seen_ranges.append(requested)
        assert requested is not None
        start, end = _requested_range(requested)
        return httpx.Response(
            206,
            headers={
                "content-range": f"bytes {start}-{end}/{len(payload)}",
                "content-length": str(end - start + 1),
            },
            content=payload[start : end + 1],
        )

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
            policy=ArtifactTransferPolicy(
                range_request_bytes=8,
                checksum_range_request_bytes=3,
            ),
        ).transfer(
            resolved=_resolved(
                expected_size=len(payload),
                range_supported=True,
                checksum=f"md5:{hashlib.md5(payload, usedforsecurity=False).hexdigest()}",
            ),
            destination=destination,
            quarantine_root=root,
        )

    assert seen_ranges == ["bytes=0-2", "bytes=3-5", "bytes=6-8", "bytes=9-11"]
    assert destination.read_bytes() == payload


@pytest.mark.asyncio
async def test_http_transport_uses_checksum_range_stream_policy(
    tmp_path: Path,
) -> None:
    payload = b"abcdef"

    async def handler(request: httpx.Request) -> httpx.Response:
        start, end = _requested_range(request.headers["range"])
        return httpx.Response(
            206,
            headers={
                "content-range": f"bytes {start}-{end}/{len(payload)}",
                "content-length": str(end - start + 1),
            },
            stream=_DelayedByteStream(payload[start : end + 1], delay_seconds=0.01),
        )

    root, destination = _quarantine_paths(tmp_path)
    checksum = f"md5:{hashlib.md5(payload, usedforsecurity=False).hexdigest()}"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
            policy=ArtifactTransferPolicy(
                chunk_size_bytes=3,
                range_request_bytes=3,
                checksum_range_request_bytes=3,
                checksum_range_chunk_size_bytes=1,
                idle_timeout_seconds=1,
                checksum_range_idle_timeout_seconds=0.02,
                range_stall_retries=0,
            ),
        ).transfer(
            resolved=_resolved(
                expected_size=len(payload),
                range_supported=True,
                checksum=checksum,
            ),
            destination=destination,
            quarantine_root=root,
        )

    assert destination.read_bytes() == payload
    assert result.bytes_transferred == len(payload)


@pytest.mark.asyncio
async def test_http_transport_retries_stalled_checksum_range_in_place(
    tmp_path: Path,
) -> None:
    payload = b"abcdef"
    seen_ranges: list[str | None] = []
    stalled = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stalled
        requested = request.headers.get("range")
        seen_ranges.append(requested)
        assert requested is not None
        start, end = _requested_range(requested)
        headers = {
            "content-range": f"bytes {start}-{end}/{len(payload)}",
            "content-length": str(end - start + 1),
        }
        if not stalled:
            stalled = True
            return httpx.Response(206, headers=headers, stream=_StalledStream())
        return httpx.Response(206, headers=headers, content=payload[start : end + 1])

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
            policy=ArtifactTransferPolicy(
                range_request_bytes=3,
                checksum_range_request_bytes=3,
                idle_timeout_seconds=0.01,
                range_stall_retries=1,
                range_retry_backoff_seconds=0,
            ),
        ).transfer(
            resolved=_resolved(
                expected_size=len(payload),
                range_supported=True,
                checksum=f"md5:{hashlib.md5(payload, usedforsecurity=False).hexdigest()}",
            ),
            destination=destination,
            quarantine_root=root,
        )

    assert seen_ranges == ["bytes=0-2", "bytes=0-2", "bytes=3-5"]
    assert destination.read_bytes() == payload
    assert result.bytes_transferred == len(payload)


@pytest.mark.asyncio
async def test_http_transport_refreshes_after_transient_range_open_failure(
    tmp_path: Path,
) -> None:
    payload = b"abcdef"
    seen: list[tuple[str, str | None]] = []
    failed = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal failed
        requested = request.headers.get("range")
        seen.append((request.headers["host"], requested))
        if not failed:
            failed = True
            raise httpx.ConnectError("temporary failure", request=request)
        assert requested is not None
        start, end = _requested_range(requested)
        return httpx.Response(
            206,
            headers={
                "content-range": f"bytes {start}-{end}/{len(payload)}",
                "content-length": str(end - start + 1),
            },
            content=payload[start : end + 1],
        )

    checksum = f"md5:{hashlib.md5(payload, usedforsecurity=False).hexdigest()}"
    refresh_calls = 0

    async def refresh() -> ResolvedTransfer:
        nonlocal refresh_calls
        refresh_calls += 1
        return _resolved(
            url="https://fresh.example.com/issue.pdf",
            expected_size=len(payload),
            range_supported=True,
            checksum=checksum,
            allowed_domains=("example.com",),
        )

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
            policy=ArtifactTransferPolicy(
                range_request_bytes=3,
                checksum_range_request_bytes=3,
                range_retry_backoff_seconds=0,
            ),
        ).transfer(
            resolved=_resolved(
                expected_size=len(payload),
                range_supported=True,
                checksum=checksum,
            ),
            destination=destination,
            quarantine_root=root,
            refresh_transfer=refresh,
        )

    assert seen == [
        ("files.example.com", "bytes=0-2"),
        ("fresh.example.com", "bytes=0-2"),
        ("fresh.example.com", "bytes=3-5"),
    ]
    assert refresh_calls == 1
    assert destination.read_bytes() == payload
    assert result.bytes_transferred == len(payload)


@pytest.mark.asyncio
async def test_http_transport_retries_timed_out_range_open(
    tmp_path: Path,
) -> None:
    payload = b"abcdef"
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(30)
        start, end = _requested_range(request.headers["range"])
        return httpx.Response(
            206,
            headers={
                "content-range": f"bytes {start}-{end}/{len(payload)}",
                "content-length": str(end - start + 1),
            },
            content=payload[start : end + 1],
        )

    root, destination = _quarantine_paths(tmp_path)
    checksum = f"md5:{hashlib.md5(payload, usedforsecurity=False).hexdigest()}"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
            policy=ArtifactTransferPolicy(
                range_request_bytes=3,
                checksum_range_request_bytes=3,
                checksum_range_open_timeout_seconds=0.01,
                range_retry_backoff_seconds=0,
            ),
        ).transfer(
            resolved=_resolved(
                expected_size=len(payload),
                range_supported=True,
                checksum=checksum,
            ),
            destination=destination,
            quarantine_root=root,
        )

    assert calls == 3
    assert destination.read_bytes() == payload
    assert result.bytes_transferred == len(payload)


@pytest.mark.asyncio
async def test_http_transport_retries_when_range_url_refresh_is_unavailable(
    tmp_path: Path,
) -> None:
    payload = b"abcdef"
    seen_hosts: list[str] = []
    original_failures = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal original_failures
        host = request.headers["host"]
        seen_hosts.append(host)
        if host == "files.example.com":
            original_failures += 1
            raise httpx.ConnectError("temporary failure", request=request)
        requested = request.headers["range"]
        start, end = _requested_range(requested)
        return httpx.Response(
            206,
            headers={
                "content-range": f"bytes {start}-{end}/{len(payload)}",
                "content-length": str(end - start + 1),
            },
            content=payload[start : end + 1],
        )

    checksum = f"md5:{hashlib.md5(payload, usedforsecurity=False).hexdigest()}"
    refresh_calls = 0

    async def refresh() -> ResolvedTransfer:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 1:
            raise ArtifactHostResolutionError(
                code="artifact_host_unavailable",
                message="The provider could not refresh the artifact URL.",
                failure_class=DirectArtifactFailureClass.TRANSIENT_HOST,
                retryable=True,
                intervention=False,
            )
        return _resolved(
            url="https://fresh.example.com/issue.pdf",
            expected_size=len(payload),
            range_supported=True,
            checksum=checksum,
            allowed_domains=("example.com",),
        )

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
            policy=ArtifactTransferPolicy(
                range_request_bytes=3,
                checksum_range_request_bytes=3,
                range_retry_backoff_seconds=0,
            ),
        ).transfer(
            resolved=_resolved(
                expected_size=len(payload),
                range_supported=True,
                checksum=checksum,
            ),
            destination=destination,
            quarantine_root=root,
            refresh_transfer=refresh,
        )

    assert original_failures == 2
    assert refresh_calls == 2
    assert seen_hosts == [
        "files.example.com",
        "files.example.com",
        "fresh.example.com",
        "fresh.example.com",
    ]
    assert destination.read_bytes() == payload
    assert result.bytes_transferred == len(payload)


@pytest.mark.asyncio
async def test_http_transport_refreshes_and_resumes_after_partial_range_stall(
    tmp_path: Path,
) -> None:
    payload = b"abcdef"
    seen: list[tuple[str, str | None]] = []
    stalled = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stalled
        requested = request.headers.get("range")
        seen.append((request.headers["host"], requested))
        assert requested is not None
        start, end = _requested_range(requested)
        headers = {
            "content-range": f"bytes {start}-{end}/{len(payload)}",
            "content-length": str(end - start + 1),
        }
        if not stalled:
            stalled = True
            return httpx.Response(
                206,
                headers=headers,
                stream=_PartialThenStalledStream(payload[start : start + 1]),
            )
        return httpx.Response(206, headers=headers, content=payload[start : end + 1])

    checksum = f"md5:{hashlib.md5(payload, usedforsecurity=False).hexdigest()}"
    refresh_calls = 0

    async def refresh() -> ResolvedTransfer:
        nonlocal refresh_calls
        refresh_calls += 1
        return _resolved(
            url="https://fresh.example.com/issue.pdf",
            expected_size=len(payload),
            range_supported=True,
            checksum=checksum,
            allowed_domains=("example.com",),
        )

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
            policy=ArtifactTransferPolicy(
                chunk_size_bytes=1,
                range_request_bytes=3,
                checksum_range_request_bytes=3,
                idle_timeout_seconds=0.01,
                range_retry_backoff_seconds=0,
            ),
        ).transfer(
            resolved=_resolved(
                expected_size=len(payload),
                range_supported=True,
                checksum=checksum,
            ),
            destination=destination,
            quarantine_root=root,
            refresh_transfer=refresh,
        )

    assert seen == [
        ("files.example.com", "bytes=0-2"),
        ("fresh.example.com", "bytes=1-3"),
        ("fresh.example.com", "bytes=4-5"),
    ]
    assert refresh_calls == 1
    assert destination.read_bytes() == payload
    assert result.bytes_transferred == len(payload)


@pytest.mark.asyncio
async def test_http_transport_resumes_checksum_protected_partial_without_http_validator(
    tmp_path: Path,
) -> None:
    payload = b"abcdefghij"
    seen_ranges: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested = request.headers.get("range")
        seen_ranges.append(requested)
        assert requested is not None
        start, end = _requested_range(requested)
        return httpx.Response(
            206,
            headers={
                "content-range": f"bytes {start}-{end}/{len(payload)}",
                "content-length": str(end - start + 1),
            },
            content=payload[start : end + 1],
        )

    root, destination = _quarantine_paths(tmp_path)
    destination.write_bytes(payload[:5])
    checksum = f"md5:{hashlib.md5(payload, usedforsecurity=False).hexdigest()}"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
            policy=ArtifactTransferPolicy(range_request_bytes=3),
        ).transfer(
            resolved=_resolved(
                expected_size=len(payload),
                range_supported=True,
                checksum=checksum,
            ),
            destination=destination,
            quarantine_root=root,
            checkpoint=HttpTransferCheckpoint(
                bytes_transferred=5,
                expected_size=len(payload),
                etag=None,
                last_modified=None,
            ),
        )

    assert seen_ranges == ["bytes=5-7", "bytes=8-9"]
    assert destination.read_bytes() == payload
    assert result.resumed is True


@pytest.mark.asyncio
async def test_http_transport_refreshes_expired_url_during_bounded_ranges(
    tmp_path: Path,
) -> None:
    payload = b"abcdefghij"
    seen: list[tuple[str, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested = request.headers.get("range")
        seen.append((request.headers["host"], requested))
        if request.headers["host"] == "files.example.com" and requested == "bytes=5-9":
            return httpx.Response(403)
        assert requested is not None
        start, end = _requested_range(requested)
        return httpx.Response(
            206,
            headers={
                "content-range": f"bytes {start}-{end}/{len(payload)}",
                "content-length": str(end - start + 1),
            },
            content=payload[start : end + 1],
        )

    refresh_calls = 0

    async def refresh() -> ResolvedTransfer:
        nonlocal refresh_calls
        refresh_calls += 1
        return _resolved(
            url="https://fresh.example.com/issue.pdf",
            expected_size=len(payload),
            range_supported=True,
            allowed_domains=("example.com",),
        )

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
            policy=ArtifactTransferPolicy(range_request_bytes=5),
        ).transfer(
            resolved=_resolved(expected_size=len(payload), range_supported=True),
            destination=destination,
            quarantine_root=root,
            refresh_transfer=refresh,
        )

    assert seen == [
        ("files.example.com", "bytes=0-4"),
        ("files.example.com", "bytes=5-9"),
        ("fresh.example.com", "bytes=5-9"),
    ]
    assert refresh_calls == 1
    assert destination.read_bytes() == payload
    assert result.bytes_transferred == len(payload)


@pytest.mark.asyncio
async def test_http_transport_rejects_changed_total_or_checksum(
    tmp_path: Path,
) -> None:
    payload = b"abcdefghij"

    async def changed_total(request: httpx.Request) -> httpx.Response:
        requested = request.headers["range"]
        start, end = _requested_range(requested)
        return httpx.Response(
            206,
            headers={"content-range": f"bytes {start}-{end}/{len(payload) + 1}"},
            content=payload[start : end + 1],
        )

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(changed_total)) as client:
        with pytest.raises(ArtifactTransferError) as changed:
            await HttpArtifactTransport(
                client=client,
                resolver=_public_resolver,
                policy=ArtifactTransferPolicy(range_request_bytes=5),
            ).transfer(
                resolved=_resolved(expected_size=len(payload), range_supported=True),
                destination=destination,
                quarantine_root=root,
            )
    assert changed.value.code == "artifact_object_changed"

    async def wrong_checksum(request: httpx.Request) -> httpx.Response:
        requested = request.headers["range"]
        start, end = _requested_range(requested)
        return httpx.Response(
            206,
            headers={"content-range": f"bytes {start}-{end}/{len(payload)}"},
            content=payload[start : end + 1],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(wrong_checksum)) as client:
        with pytest.raises(ArtifactTransferError) as checksum:
            await HttpArtifactTransport(
                client=client,
                resolver=_public_resolver,
                policy=ArtifactTransferPolicy(range_request_bytes=5),
            ).transfer(
                resolved=_resolved(
                    expected_size=len(payload),
                    range_supported=True,
                    checksum="md5:00000000000000000000000000000000",
                ),
                destination=destination,
                quarantine_root=root,
            )
    assert checksum.value.code == "artifact_checksum_mismatch"
    assert not destination.exists()


@pytest.mark.asyncio
async def test_http_transport_restarts_when_server_returns_changed_object(
    tmp_path: Path,
) -> None:
    seen_ranges: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_ranges.append(request.headers.get("range"))
        return httpx.Response(
            200,
            headers={"content-length": "6", "etag": '"changed"'},
            content=b"uvwxyz",
        )

    root, destination = _quarantine_paths(tmp_path)
    destination.write_bytes(b"abc")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
        ).transfer(
            resolved=_resolved(expected_size=6, etag='"stable"', range_supported=True),
            destination=destination,
            quarantine_root=root,
            checkpoint=HttpTransferCheckpoint(
                bytes_transferred=3,
                expected_size=6,
                etag='"stable"',
                last_modified=None,
            ),
        )

    assert seen_ranges == ["bytes=3-"]
    assert destination.read_bytes() == b"uvwxyz"
    assert result.etag == '"changed"'
    assert result.resumed is False


@pytest.mark.asyncio
async def test_http_transport_retries_without_range_for_invalid_partial_response(
    tmp_path: Path,
) -> None:
    seen_ranges: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_ranges.append(request.headers.get("range"))
        if len(seen_ranges) == 1:
            return httpx.Response(
                206,
                headers={"content-range": "bytes 0-2/6", "etag": '"stable"'},
                content=b"abc",
            )
        return httpx.Response(
            200,
            headers={"content-length": "6", "etag": '"stable"'},
            content=b"abcdef",
        )

    root, destination = _quarantine_paths(tmp_path)
    destination.write_bytes(b"abc")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
        ).transfer(
            resolved=_resolved(expected_size=6, etag='"stable"', range_supported=True),
            destination=destination,
            quarantine_root=root,
            checkpoint=HttpTransferCheckpoint(
                bytes_transferred=3,
                expected_size=6,
                etag='"stable"',
                last_modified=None,
            ),
        )

    assert seen_ranges == ["bytes=3-", None]
    assert destination.read_bytes() == b"abcdef"
    assert result.resumed is False


@pytest.mark.asyncio
async def test_http_transport_refreshes_expired_url_without_new_logical_attempt(
    tmp_path: Path,
) -> None:
    hosts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.headers["host"])
        return httpx.Response(200, headers={"content-length": "3"}, content=b"new")

    refresh_calls = 0

    async def refresh() -> ResolvedTransfer:
        nonlocal refresh_calls
        refresh_calls += 1
        return _resolved(url="https://fresh.example.com/issue.cbz", expected_size=3)

    root, destination = _quarantine_paths(tmp_path)
    expired = _resolved(expected_size=3)
    expired = ResolvedTransfer(
        host_kind=expired.host_kind,
        url=expired.url,
        expected_size=expired.expected_size,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        allowed_domains=expired.allowed_domains,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
        ).transfer(
            resolved=expired,
            destination=destination,
            quarantine_root=root,
            refresh_transfer=refresh,
        )

    assert refresh_calls == 1
    assert hosts == ["fresh.example.com"]
    assert result.bytes_transferred == 3


@pytest.mark.asyncio
async def test_http_transport_cancel_removes_partial_and_pause_preserves_it(
    tmp_path: Path,
) -> None:
    cancel_event = asyncio.Event()
    pause_event = asyncio.Event()

    async def cancel_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_TwoChunkStream(b"abc", b"def", cancel_event.set))

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(cancel_handler)) as client:
        with pytest.raises(ArtifactTransferCancelledError):
            await HttpArtifactTransport(
                client=client,
                resolver=_public_resolver,
                policy=ArtifactTransferPolicy(chunk_size_bytes=3),
            ).transfer(
                resolved=_resolved(expected_size=6),
                destination=destination,
                quarantine_root=root,
                cancel_event=cancel_event,
            )
    assert not destination.exists()

    async def pause_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_TwoChunkStream(b"abc", b"def", pause_event.set))

    async with httpx.AsyncClient(transport=httpx.MockTransport(pause_handler)) as client:
        with pytest.raises(ArtifactTransferPausedError) as caught:
            await HttpArtifactTransport(
                client=client,
                resolver=_public_resolver,
                policy=ArtifactTransferPolicy(chunk_size_bytes=3),
            ).transfer(
                resolved=_resolved(expected_size=6),
                destination=destination,
                quarantine_root=root,
                pause_event=pause_event,
            )
    assert destination.read_bytes() == b"abc"
    assert caught.value.checkpoint.bytes_transferred == 3


@pytest.mark.asyncio
async def test_http_transport_cancel_interrupts_stalled_chunk_read(
    tmp_path: Path,
) -> None:
    cancel_event = asyncio.Event()
    read_started = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_SignalledStalledStream(read_started))

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = asyncio.create_task(
            HttpArtifactTransport(
                client=client,
                resolver=_public_resolver,
                policy=ArtifactTransferPolicy(idle_timeout_seconds=30),
            ).transfer(
                resolved=_resolved(expected_size=6),
                destination=destination,
                quarantine_root=root,
                cancel_event=cancel_event,
            )
        )
        await asyncio.wait_for(read_started.wait(), timeout=1)
        cancel_event.set()
        with pytest.raises(ArtifactTransferCancelledError):
            await asyncio.wait_for(transfer, timeout=0.25)

    assert not destination.exists()


@pytest.mark.asyncio
async def test_http_transport_cancel_interrupts_stalled_response_open(
    tmp_path: Path,
) -> None:
    cancel_event = asyncio.Event()
    request_started = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        request_started.set()
        await asyncio.sleep(30)
        return httpx.Response(200, content=b"never")

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = asyncio.create_task(
            HttpArtifactTransport(
                client=client,
                resolver=_public_resolver,
                policy=ArtifactTransferPolicy(idle_timeout_seconds=30),
            ).transfer(
                resolved=_resolved(expected_size=5),
                destination=destination,
                quarantine_root=root,
                cancel_event=cancel_event,
            )
        )
        await asyncio.wait_for(request_started.wait(), timeout=1)
        cancel_event.set()
        with pytest.raises(ArtifactTransferCancelledError):
            await asyncio.wait_for(transfer, timeout=0.25)

    assert not destination.exists()


@pytest.mark.asyncio
async def test_http_transport_rejects_oversize_or_insufficient_disk(
    tmp_path: Path,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "100"}, content=b"x" * 100)

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArtifactTransferError) as oversize:
            await HttpArtifactTransport(
                client=client,
                resolver=_public_resolver,
                policy=ArtifactTransferPolicy(max_artifact_bytes=50),
            ).transfer(
                resolved=_resolved(expected_size=None),
                destination=destination,
                quarantine_root=root,
            )
    assert oversize.value.code == "artifact_too_large"
    assert oversize.value.failure_class is DirectArtifactFailureClass.SAFETY

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArtifactTransferError) as disk:
            await HttpArtifactTransport(
                client=client,
                resolver=_public_resolver,
                disk_free_provider=lambda _path: 10,
                policy=ArtifactTransferPolicy(min_free_bytes=5),
            ).transfer(
                resolved=_resolved(expected_size=100),
                destination=destination,
                quarantine_root=root,
            )
    assert disk.value.code == "artifact_disk_space_insufficient"


@pytest.mark.asyncio
async def test_http_transport_unknown_size_preserves_disk_reserve(
    tmp_path: Path,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_TwoChunkStream(b"abcd", b"efgh", lambda: None))

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArtifactTransferError) as caught:
            await HttpArtifactTransport(
                client=client,
                resolver=_public_resolver,
                disk_free_provider=lambda _path: 10,
                policy=ArtifactTransferPolicy(min_free_bytes=5),
            ).transfer(
                resolved=_resolved(expected_size=None),
                destination=destination,
                quarantine_root=root,
            )

    assert caught.value.code == "artifact_disk_space_insufficient"
    assert not destination.exists()


@pytest.mark.asyncio
async def test_http_transport_write_oserror_is_classified_and_removes_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"abcdef")

    root, destination = _quarantine_paths(tmp_path)

    class _FailingWriter:
        def __init__(self) -> None:
            self._handle = destination.open("wb")

        def write(self, chunk: bytes) -> int:
            self._handle.write(chunk[:1])
            raise OSError("synthetic disk full")

        def close(self) -> None:
            self._handle.close()

    monkeypatch.setattr(
        "pullbox.providers.artifact_hosts.transport.open_quarantine_file",
        lambda _path, *, append: _FailingWriter(),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArtifactTransferError) as caught:
            await HttpArtifactTransport(
                client=client,
                resolver=_public_resolver,
            ).transfer(
                resolved=_resolved(expected_size=None),
                destination=destination,
                quarantine_root=root,
            )

    assert caught.value.code == "artifact_quarantine_write_failed"
    assert not destination.exists()


@pytest.mark.asyncio
async def test_http_transport_revalidates_redirect_and_rejects_private_target(
    tmp_path: Path,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://private.example/file.cbz"})

    async def resolver(host: str, _port: int) -> Sequence[str]:
        return ("127.0.0.1",) if host == "private.example" else (_PUBLIC_IP,)

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArtifactTransferError) as caught:
            await HttpArtifactTransport(client=client, resolver=resolver).transfer(
                resolved=_resolved(expected_size=None, allowed_domains=()),
                destination=destination,
                quarantine_root=root,
            )

    assert caught.value.code == "unsafe_artifact_url"
    assert not destination.exists()


@pytest.mark.asyncio
async def test_http_transport_strips_sensitive_headers_on_cross_host_redirect(
    tmp_path: Path,
) -> None:
    seen_headers: list[httpx.Headers] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        if request.headers["host"] == "files.example.com":
            return httpx.Response(
                302,
                headers={"location": "https://cdn.example.com/issue.cbz"},
            )
        assert request.headers["host"] == "cdn.example.com"
        return httpx.Response(200, content=b"fixture")

    root, destination = _quarantine_paths(tmp_path)
    resolved = ResolvedTransfer(
        host_kind=DirectArtifactHostKind.TERABOX,
        url="https://files.example.com/issue.cbz",
        headers={
            "Cookie": "session=secret",
            "Authorization": "Bearer secret",
            "X-Artifact-Request": "fixture",
        },
        expected_size=7,
        allowed_domains=("example.com",),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await HttpArtifactTransport(
            client=client,
            resolver=_public_resolver,
        ).transfer(
            resolved=resolved,
            destination=destination,
            quarantine_root=root,
        )

    assert seen_headers[0]["cookie"] == "session=secret"
    assert seen_headers[0]["authorization"] == "Bearer secret"
    assert seen_headers[0]["x-artifact-request"] == "fixture"
    assert "cookie" not in seen_headers[1]
    assert "authorization" not in seen_headers[1]
    assert seen_headers[1]["x-artifact-request"] == "fixture"
    assert destination.read_bytes() == b"fixture"


@pytest.mark.asyncio
async def test_http_transport_idle_timeout_is_retryable_and_preserves_crash_checkpoint(
    tmp_path: Path,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_StalledStream())

    root, destination = _quarantine_paths(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArtifactTransferError) as caught:
            await HttpArtifactTransport(
                client=client,
                resolver=_public_resolver,
                policy=ArtifactTransferPolicy(idle_timeout_seconds=0.01),
            ).transfer(
                resolved=_resolved(expected_size=None),
                destination=destination,
                quarantine_root=root,
            )

    assert caught.value.code == "artifact_transfer_idle_timeout"
    assert caught.value.retryable is True


def _resolved(
    *,
    url: str = "https://files.example.com/issue.cbz",
    expected_size: int | None,
    etag: str | None = None,
    checksum: str | None = None,
    range_supported: bool = False,
    prefer_single_response: bool = False,
    allowed_domains: tuple[str, ...] = ("example.com",),
) -> ResolvedTransfer:
    return ResolvedTransfer(
        host_kind=DirectArtifactHostKind.GENERIC_HTTPS,
        url=url,
        expected_size=expected_size,
        etag=etag,
        checksum=checksum,
        range_supported=range_supported,
        prefer_single_response=prefer_single_response,
        allowed_domains=allowed_domains,
    )


def _quarantine_paths(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "quarantine"
    root.mkdir(exist_ok=True)
    return root, root / "attempt.part"


async def _public_resolver(_host: str, _port: int) -> Sequence[str]:
    return (_PUBLIC_IP,)


def _requested_range(value: str) -> tuple[int, int]:
    start, end = value.removeprefix("bytes=").split("-", 1)
    return int(start), int(end)


class _TwoChunkStream(httpx.AsyncByteStream):
    def __init__(self, first: bytes, second: bytes, between: object) -> None:
        self._first = first
        self._second = second
        self._between = between

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield self._first
        callback = self._between
        assert callable(callback)
        callback()
        yield self._second


class _StalledStream(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[no-untyped-def]
        await asyncio.sleep(30)
        yield b"never"


class _SignalledStalledStream(httpx.AsyncByteStream):
    def __init__(self, started: asyncio.Event) -> None:
        self._started = started

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        self._started.set()
        await asyncio.sleep(30)
        yield b"never"


class _PartialThenStalledStream(httpx.AsyncByteStream):
    def __init__(self, partial: bytes) -> None:
        self._partial = partial

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield self._partial
        await asyncio.sleep(30)
        yield b"never"


class _PartialThenProtocolErrorStream(httpx.AsyncByteStream):
    def __init__(self, partial: bytes) -> None:
        self._partial = partial

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield self._partial
        raise httpx.RemoteProtocolError("peer closed the response early")


class _CompleteThenStalledStream(httpx.AsyncByteStream):
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield self._payload
        await asyncio.sleep(30)
        yield b"never"


class _DelayedByteStream(httpx.AsyncByteStream):
    def __init__(self, payload: bytes, *, delay_seconds: float) -> None:
        self._payload = payload
        self._delay_seconds = delay_seconds

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for byte in self._payload:
            await asyncio.sleep(self._delay_seconds)
            yield bytes((byte,))
