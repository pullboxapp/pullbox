"""Host-specific resolution contracts for native direct-download adapters."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence

from pullbox.models.direct_acquisition import (
    DirectArtifactFailureClass,
    DirectArtifactHostKind,
)
from pullbox.providers.artifact_hosts.contract import (
    ArtifactHostResolutionError,
    HostResolutionRequest,
)
from pullbox.providers.artifact_hosts.datanodes import DataNodesAdapter
from pullbox.providers.artifact_hosts.generic import GenericHttpsAdapter
from pullbox.providers.artifact_hosts.mediafire import MediaFireAdapter
from pullbox.providers.artifact_hosts.pixeldrain import PixelDrainAdapter
from pullbox.providers.artifact_hosts.rootz import RootzAdapter
from pullbox.providers.artifact_hosts.terabox import TeraBoxAdapter
from pullbox.providers.direct.resolver import DirectResolverCookie, DirectResolverResult

NOW = datetime(2026, 7, 27, 12, tzinfo=UTC)
PIXELDRAIN_KEY = "pixeldrain-secret"
TERABOX_SESSION = "terabox-secret"
DATANODES_USERNAME = "reader@example.test"
DATANODES_PASSWORD = "datanodes-password"


async def _resolve_public(_host: str, _port: int) -> Sequence[str]:
    return ["8.8.8.8"]


def _request(
    host_kind: DirectArtifactHostKind,
    url: str,
    *,
    final: bool = False,
    checksum: str | None = None,
    expected_size: int | None = None,
) -> HostResolutionRequest:
    return HostResolutionRequest(
        artifact_identity="fixture-artifact",
        host_kind=host_kind,
        share_url=None if final else url,
        final_url=url if final else None,
        expected_size=expected_size,
        checksum=checksum,
    )


async def test_generic_https_accepts_a_probed_final_file_with_resume_validators() -> None:
    observed_user_agent: str | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_user_agent
        assert request.method == "GET"
        assert request.headers["Range"] == "bytes=0-0"
        observed_user_agent = request.headers["User-Agent"]
        assert observed_user_agent.startswith("Mozilla/5.0")
        return httpx.Response(
            206,
            headers={
                "Content-Type": "application/vnd.comicbook+zip",
                "Content-Range": "bytes 0-0/4096",
                "Content-Disposition": 'attachment; filename="fixture.cbz"',
                "ETag": '"fixture-etag"',
                "Last-Modified": "Mon, 27 Jul 2026 00:00:00 GMT",
            },
            content=b"P",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = await GenericHttpsAdapter(client, resolver=_resolve_public).resolve(
            _request(
                DirectArtifactHostKind.GENERIC_HTTPS,
                "https://files.example.test/fixture.cbz",
                final=True,
                checksum="md5:11111111111111111111111111111111",
            ),
            credentials={},
        )

    assert transfer.expected_size == 4096
    assert transfer.filename_hint == "fixture.cbz"
    assert transfer.etag == '"fixture-etag"'
    assert transfer.checksum == "md5:11111111111111111111111111111111"
    assert transfer.range_supported is True
    assert transfer.headers["User-Agent"] == observed_user_agent


async def test_generic_https_accepts_large_files_when_server_ignores_range_probe() -> None:
    file_size = 3 * 1024 * 1024
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={
                "Content-Type": "application/vnd.comicbook+zip",
                "Content-Length": str(file_size),
                "Content-Disposition": 'attachment; filename="large.cbz"',
            },
            content=b"P" * file_size,
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        transfer = await GenericHttpsAdapter(client, resolver=_resolve_public).resolve(
            _request(
                DirectArtifactHostKind.GENERIC_HTTPS,
                "https://files.example.test/large.cbz",
                final=True,
            ),
            credentials={},
        )

    assert transfer.expected_size == file_size
    assert transfer.filename_hint == "large.cbz"
    assert transfer.range_supported is False


async def test_generic_https_prefers_one_stream_for_booksdl_ranges() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            206,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": "bytes 0-0/59247008",
                "Content-Length": "1",
                "ETag": '"stable"',
                "Last-Modified": "Mon, 24 Aug 2026 00:00:00 GMT",
            },
            content=b"P",
            request=httpx.Request(
                "GET",
                "https://cdn4.booksdl.lc/get.php?token=opaque",
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = await GenericHttpsAdapter(client, resolver=_resolve_public).resolve(
            _request(
                DirectArtifactHostKind.GENERIC_HTTPS,
                "https://cdn4.booksdl.lc/get.php?token=opaque",
                final=True,
                checksum="md5:11111111111111111111111111111111",
                expected_size=59_247_008,
            ),
            credentials={},
        )

    assert transfer.expected_size == 59_247_008
    assert transfer.etag == '"stable"'
    assert transfer.range_supported is True
    assert transfer.prefer_single_response is True


async def test_generic_https_rejects_an_html_landing_page() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=b"<html><title>Download</title></html>",
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ArtifactHostResolutionError) as raised:
            await GenericHttpsAdapter(client, resolver=_resolve_public).resolve(
                _request(
                    DirectArtifactHostKind.GENERIC_HTTPS,
                    "https://files.example.test/download?id=fixture",
                    final=True,
                ),
                credentials={},
            )

    assert raised.value.code == "unsupported_landing_page"
    assert raised.value.failure_class is DirectArtifactFailureClass.UNSUPPORTED_ARTIFACT_HOST
    assert raised.value.intervention is True


async def test_generic_https_classifies_cloudflare_as_a_route_challenge() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            403,
            headers={
                "CF-Mitigated": "challenge",
                "Content-Type": "text/html; charset=UTF-8",
                "Server": "cloudflare",
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ArtifactHostResolutionError) as raised:
            await GenericHttpsAdapter(client, resolver=_resolve_public).resolve(
                _request(
                    DirectArtifactHostKind.GENERIC_HTTPS,
                    "https://files.example.test/challenged.cbz",
                    final=True,
                ),
                credentials={},
            )

    assert raised.value.code == "artifact_host_challenge_required"
    assert raised.value.failure_class is DirectArtifactFailureClass.ARTIFACT_HOST_CHALLENGE
    assert raised.value.retryable is False
    assert raised.value.intervention is True
    assert raised.value.http_status == 403


@pytest.mark.parametrize("status", [404, 410])
async def test_generic_https_classifies_a_missing_file_as_a_permanent_mirror(
    status: int,
) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(status, headers={"Content-Length": "0"})
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ArtifactHostResolutionError) as raised:
            await GenericHttpsAdapter(client, resolver=_resolve_public).resolve(
                _request(
                    DirectArtifactHostKind.GENERIC_HTTPS,
                    "https://files.example.test/missing.cbz",
                    final=True,
                ),
                credentials={},
            )

    assert raised.value.code == "artifact_file_unavailable"
    assert str(raised.value) == (
        "The selected file is no longer available at this secure download location. "
        "Choose another search result."
    )
    assert raised.value.failure_class is DirectArtifactFailureClass.PERMANENT_MIRROR
    assert raised.value.retryable is False
    assert raised.value.intervention is True
    assert raised.value.http_status == status


@pytest.mark.parametrize(
    "url",
    [
        "https://www12.zippyshare.com/v/example/file.html",
        "https://dropapk.to/example",
    ],
)
async def test_generic_https_rejects_retired_hosts_without_network_access(url: str) -> None:
    requests_made = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests_made
        requests_made += 1
        return httpx.Response(200, content=b"not-reached")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArtifactHostResolutionError) as raised:
            await GenericHttpsAdapter(client, resolver=_resolve_public).resolve(
                _request(DirectArtifactHostKind.GENERIC_HTTPS, url, final=True),
                credentials={},
            )

    assert raised.value.code == "unsupported_artifact_host"
    assert requests_made == 0


async def test_pixeldrain_resolves_public_or_account_downloads_from_file_info() -> None:
    seen_authorization: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers.get("Authorization"))
        assert request.url.path == "/api/file/AbC123/info"
        return httpx.Response(
            200,
            json={
                "success": True,
                "id": "AbC123",
                "name": "fixture.cbz",
                "size": 8192,
                "mime_type": "application/vnd.comicbook+zip",
                "availability": "",
                "can_download": True,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        anonymous = await PixelDrainAdapter(client, resolver=_resolve_public).resolve(
            _request(DirectArtifactHostKind.PIXELDRAIN, "https://pixeldrain.com/u/AbC123"),
            credentials={},
        )
        account = await PixelDrainAdapter(client, resolver=_resolve_public).resolve(
            _request(DirectArtifactHostKind.PIXELDRAIN, "https://pixeldrain.com/u/AbC123"),
            credentials={"api_key": PIXELDRAIN_KEY},
        )

    expected_auth = "Basic " + base64.b64encode(f":{PIXELDRAIN_KEY}".encode()).decode()
    assert seen_authorization == [None, expected_auth]
    assert anonymous.expected_size == 8192
    assert anonymous.headers == {}
    assert account.headers == {"Authorization": expected_auth}
    assert account.filename_hint == "fixture.cbz"
    assert PIXELDRAIN_KEY not in repr(account)


@pytest.mark.parametrize(
    ("status", "code", "expected_class", "intervention"),
    [
        (
            403,
            "file_rate_limited_captcha_required",
            DirectArtifactFailureClass.ARTIFACT_HOST_CHALLENGE,
            True,
        ),
        (403, "transfer_limit_exceeded", DirectArtifactFailureClass.HOST_QUOTA, True),
        (
            401,
            "authentication_failed",
            DirectArtifactFailureClass.ARTIFACT_HOST_AUTH_REQUIRED,
            True,
        ),
        (500, "internal", DirectArtifactFailureClass.TRANSIENT_HOST, False),
    ],
)
async def test_pixeldrain_maps_stable_error_codes(
    status: int,
    code: str,
    expected_class: DirectArtifactFailureClass,
    intervention: bool,
) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            status,
            json={"success": False, "value": code, "message": "sensitive provider text"},
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ArtifactHostResolutionError) as raised:
            await PixelDrainAdapter(client, resolver=_resolve_public).resolve(
                _request(DirectArtifactHostKind.PIXELDRAIN, "https://pixeldrain.com/u/AbC123"),
                credentials={"api_key": PIXELDRAIN_KEY},
            )

    assert raised.value.code == code
    assert raised.value.failure_class is expected_class
    assert raised.value.intervention is intervention
    assert "sensitive provider text" not in str(raised.value)
    assert PIXELDRAIN_KEY not in repr(raised.value)


async def test_rootz_resolves_short_id_to_uuid_then_ephemeral_signed_url() -> None:
    seen_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/d/short123":
            return httpx.Response(
                200,
                text=('<script>self.__next_f.push([1,"pageToken\\":\\"page-token-1\\"])</script>'),
                headers={"Content-Type": "text/html"},
            )
        if request.url.path == "/api/files/download-by-short":
            assert request.url.params["shortId"] == "short123"
            assert request.headers["X-Page-Token"] == "page-token-1"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "fileId": "550e8400-e29b-41d4-a716-446655440000",
                        "name": "fixture.cbz",
                        "size": 16384,
                        "status": "active",
                        "downloadAllowed": True,
                    },
                },
            )
        assert request.url.path == "/api/files/download/550e8400-e29b-41d4-a716-446655440000"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "url": "https://cdn-files.alcyone.so/signed/fixture?token=secret",
                    "fileName": "fixture.cbz",
                    "size": 16384,
                    "mimeType": "application/octet-stream",
                    "expiresIn": 86400,
                    "expiresAt": None,
                    "shortId": "short123",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = await RootzAdapter(
            client,
            resolver=_resolve_public,
            clock=lambda: NOW,
        ).resolve(
            _request(DirectArtifactHostKind.ROOTZ, "https://rootz.so/d/short123"),
            credentials={},
        )

    assert seen_paths == [
        "/d/short123",
        "/api/files/download-by-short",
        "/api/files/download/550e8400-e29b-41d4-a716-446655440000",
    ]
    assert transfer.expected_size == 16384
    assert transfer.expires_at == NOW + timedelta(days=1)
    assert transfer.filename_hint == "fixture.cbz"
    assert "token=secret" not in repr(transfer)


async def test_rootz_contract_drift_fails_without_guessing() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, text="<html>new layout</html>")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ArtifactHostResolutionError) as raised:
            await RootzAdapter(client, resolver=_resolve_public).resolve(
                _request(DirectArtifactHostKind.ROOTZ, "https://rootz.so/d/short123"),
                credentials={},
            )

    assert raised.value.code == "artifact_host_contract_changed"
    assert raised.value.intervention is True


async def test_mediafire_resolves_the_bounded_public_download_anchor() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Cookie") is None
        return httpx.Response(
            200,
            text=(
                '<html><a id="downloadButton" '
                'href="https://download123.mediafire.com/a1/b2/fixture.cbz">Download</a></html>'
            ),
            headers={"Content-Type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = await MediaFireAdapter(client, resolver=_resolve_public).resolve(
            _request(
                DirectArtifactHostKind.MEDIAFIRE,
                "https://www.mediafire.com/file/example/fixture.cbz/file",
            ),
            credentials={},
        )

    assert transfer.filename_hint == "fixture.cbz"
    assert transfer.headers == {}


async def test_mediafire_rejects_unsupported_browser_session_credentials() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network request made"))
    ) as client:
        with pytest.raises(ArtifactHostResolutionError) as raised:
            await MediaFireAdapter(client, resolver=_resolve_public).resolve(
                _request(
                    DirectArtifactHostKind.MEDIAFIRE,
                    "https://www.mediafire.com/file/example/fixture.cbz/file",
                ),
                credentials={"session": "mediafire-secret"},
            )

    assert raised.value.code == "invalid_host_credentials"


async def test_terabox_follows_the_current_official_share_redirect() -> None:
    seen_hosts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.headers["Host"])
        if request.headers["Host"] == "www.1024terabox.com":
            assert request.headers["Cookie"] == f"ndus={TERABOX_SESSION}"
            return httpx.Response(
                302,
                headers={"Location": "https://www.terabox.app/s/1fixture"},
            )
        if request.headers["Host"] == "www.terabox.app":
            assert request.headers.get("Cookie") is None
            return httpx.Response(
                200,
                text='<script>window.jsToken = "js-token";</script>',
                headers={"Content-Type": "text/html"},
            )
        assert request.headers["Cookie"] == f"ndus={TERABOX_SESSION}"
        assert request.url.path == "/share/list"
        return httpx.Response(
            200,
            json={
                "errno": 0,
                "list": [
                    {
                        "isdir": 0,
                        "server_filename": "fixture.cbz",
                        "size": 32768,
                        "dlink": "https://d.terabox.com/file/signed?token=secret",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = await TeraBoxAdapter(client, resolver=_resolve_public).resolve(
            _request(
                DirectArtifactHostKind.TERABOX,
                "https://1024terabox.com/s/1fixture",
            ),
            credentials={"session_token": TERABOX_SESSION},
        )

    assert seen_hosts == ["www.1024terabox.com", "www.terabox.app", "www.terabox.com"]
    assert transfer.expected_size == 32768


async def test_terabox_canonicalizes_share_aliases_before_an_insecure_redirect() -> None:
    seen_hosts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.headers["Host"])
        assert request.headers["Cookie"] == f"ndus={TERABOX_SESSION}"
        if request.url.path == "/s/1fixture":
            assert request.headers["Host"] == "www.1024terabox.com"
            return httpx.Response(
                200,
                text='<script>window.jsToken = "js-token";</script>',
                headers={"Content-Type": "text/html"},
            )
        assert request.headers["Host"] == "www.terabox.com"
        return httpx.Response(
            200,
            json={
                "errno": 0,
                "list": [
                    {
                        "isdir": 0,
                        "server_filename": "fixture.cbz",
                        "size": 32768,
                        "dlink": "https://d.terabox.com/file/signed?token=secret",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = await TeraBoxAdapter(client, resolver=_resolve_public).resolve(
            _request(
                DirectArtifactHostKind.TERABOX,
                "https://teraboxapp.com/s/1fixture",
            ),
            credentials={"session_token": TERABOX_SESSION},
        )

    assert seen_hosts == ["www.1024terabox.com", "www.terabox.com"]
    assert transfer.expected_size == 32768


async def test_terabox_link_share_alias_is_supported() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Cookie"] == f"ndus={TERABOX_SESSION}"
        if request.url.path == "/s/1fixture":
            return httpx.Response(
                200,
                text='<script>window.jsToken = "js-token";</script>',
                headers={"Content-Type": "text/html"},
            )
        return httpx.Response(
            200,
            json={
                "errno": 0,
                "list": [
                    {
                        "isdir": 0,
                        "server_filename": "fixture.cbz",
                        "size": 32768,
                        "dlink": "https://d.terabox.com/file/signed?token=secret",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = await TeraBoxAdapter(client, resolver=_resolve_public).resolve(
            _request(DirectArtifactHostKind.TERABOX, "https://terabox.link/s/1fixture"),
            credentials={"session_token": TERABOX_SESSION},
        )

    assert transfer.expected_size == 32768


async def test_terabox_extracts_the_current_percent_encoded_js_token() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Cookie"] == f"ndus={TERABOX_SESSION}"
        if request.url.path == "/s/1fixture":
            return httpx.Response(
                200,
                text=(
                    "<script>try { eval(decodeURIComponent(`"
                    "function%20fn(a)%7Bwindow.jsToken%20%3D%20a%7D%3B"
                    "fn(%22encoded-js-token%22)`)) } catch (ex) {}</script>"
                ),
                headers={"Content-Type": "text/html"},
            )
        assert request.url.path == "/share/list"
        assert request.url.params["jsToken"] == "encoded-js-token"
        return httpx.Response(
            200,
            json={
                "errno": 0,
                "list": [
                    {
                        "isdir": 0,
                        "server_filename": "fixture.cbz",
                        "size": 32768,
                        "dlink": "https://d.terabox.com/file/signed?token=secret",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = await TeraBoxAdapter(client, resolver=_resolve_public).resolve(
            _request(DirectArtifactHostKind.TERABOX, "https://www.1024tera.com/s/1fixture"),
            credentials={"session_token": TERABOX_SESSION},
        )

    assert transfer.expected_size == 32768


async def test_terabox_session_resolves_share_metadata_to_a_direct_link() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Cookie"] == f"ndus={TERABOX_SESSION}"
        if request.url.path == "/s/1fixture":
            return httpx.Response(
                200,
                text='<script>window.jsToken = "js-token";</script>',
                headers={"Content-Type": "text/html"},
            )
        assert request.url.path == "/share/list"
        assert request.url.params["shorturl"] == "fixture"
        assert request.url.params["jsToken"] == "js-token"
        return httpx.Response(
            200,
            json={
                "errno": 0,
                "list": [
                    {
                        "isdir": 0,
                        "fs_id": 123,
                        "server_filename": "fixture.cbz",
                        "size": 32768,
                        "dlink": "https://d.terabox.com/file/signed?token=secret",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = await TeraBoxAdapter(client, resolver=_resolve_public).resolve(
            _request(DirectArtifactHostKind.TERABOX, "https://www.terabox.com/s/1fixture"),
            credentials={"cookie": TERABOX_SESSION},
        )

    assert transfer.expected_size == 32768
    assert transfer.filename_hint == "fixture.cbz"
    assert transfer.headers == {"Cookie": f"ndus={TERABOX_SESSION}"}
    assert TERABOX_SESSION not in repr(transfer)


async def test_terabox_expired_session_requires_reauthentication() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            401,
            json={"errno": -6, "errmsg": "session expired"},
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ArtifactHostResolutionError) as raised:
            await TeraBoxAdapter(client, resolver=_resolve_public).resolve(
                _request(DirectArtifactHostKind.TERABOX, "https://terabox.com/s/1fixture"),
                credentials={"cookie": TERABOX_SESSION},
            )

    assert raised.value.code == "artifact_host_auth_required"
    assert raised.value.failure_class is DirectArtifactFailureClass.ARTIFACT_HOST_AUTH_REQUIRED
    assert TERABOX_SESSION not in repr(raised.value)


async def test_terabox_missing_direct_link_requires_reauthentication() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/s/1fixture":
            return httpx.Response(
                200,
                text='<script>window.jsToken = "js-token";</script>',
                headers={"Content-Type": "text/html"},
            )
        return httpx.Response(
            200,
            json={
                "errno": 0,
                "list": [
                    {
                        "isdir": 0,
                        "server_filename": "fixture.cbz",
                        "size": 32768,
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArtifactHostResolutionError) as raised:
            await TeraBoxAdapter(client, resolver=_resolve_public).resolve(
                _request(DirectArtifactHostKind.TERABOX, "https://terabox.com/s/1fixture"),
                credentials={"cookie": TERABOX_SESSION},
            )

    assert raised.value.code == "artifact_host_auth_required"
    assert raised.value.failure_class is DirectArtifactFailureClass.ARTIFACT_HOST_AUTH_REQUIRED
    assert TERABOX_SESSION not in repr(raised.value)


async def test_datanodes_requires_account_credentials_before_network_access() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArtifactHostResolutionError) as raised:
            await DataNodesAdapter(client, resolver=_resolve_public).resolve(
                _request(
                    DirectArtifactHostKind.DATANODES,
                    "https://datanodes.to/fixture/fixture.cbz",
                ),
                credentials={},
            )

    assert raised.value.code == "artifact_host_auth_required"
    assert calls == 0


async def test_datanodes_uses_trawl_login_solution_without_sharing_credentials() -> None:
    solver_calls: list[tuple[object, ...]] = []
    progress_events: list[dict[str, object]] = []

    async def solve_login(*args: object) -> DirectResolverResult:
        solver_calls.append(args)
        return DirectResolverResult(
            final_url="https://datanodes.to/login.html",
            status_code=200,
            html=(
                '<form name="FL" method="POST" action="/">'
                '<input type="hidden" name="op" value="login">'
                '<input type="hidden" name="token" value="login-token">'
                '<input type="hidden" name="cf-turnstile-response" value="solved-token">'
                "</form>"
            ),
            cookies=(
                DirectResolverCookie(
                    name="cf_clearance",
                    value="trawl-clearance",
                    domain=".datanodes.to",
                    path="/",
                ),
            ),
            user_agent="Trawl Browser",
        )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["User-Agent"] == "Trawl Browser"
        assert "cf_clearance=trawl-clearance" in request.headers["Cookie"]
        if request.method == "POST":
            assert b"login=reader%40example.test" in request.content
            assert b"password=datanodes-password" in request.content
            assert b"cf-turnstile-response=solved-token" in request.content
            return httpx.Response(
                200,
                text='<a href="/logout">Logout</a>',
                headers={"Set-Cookie": "account=premium; Domain=datanodes.to; Path=/"},
            )
        assert request.url.path == "/fixture/fixture.cbz"
        assert "account=premium" in request.headers["Cookie"]
        return httpx.Response(
            200,
            text=(
                '<a id="downloadbtn" '
                'href="https://s1.datanodes.to/d/fixture/fixture.cbz">Download</a>'
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = await DataNodesAdapter(
            client,
            resolver=_resolve_public,
            login_solver=solve_login,
        ).resolve(
            _request(
                DirectArtifactHostKind.DATANODES,
                "https://datanodes.to/fixture/fixture.cbz",
            ),
            credentials={
                "username": DATANODES_USERNAME,
                "password": DATANODES_PASSWORD,
            },
            progress_callback=progress_events.append,
        )

    assert solver_calls == [
        ("https://datanodes.to/login.html", progress_events.append),
    ]
    assert transfer.url == "https://s1.datanodes.to/d/fixture/fixture.cbz"
    assert transfer.headers["User-Agent"] == "Trawl Browser"
    assert DATANODES_USERNAME not in repr(solver_calls)
    assert DATANODES_PASSWORD not in repr(solver_calls)


async def test_datanodes_registered_account_honors_wait_and_resolves_free_form() -> None:
    seen: list[tuple[str, str, str | None]] = []
    sleep_calls: list[float] = []

    async def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("Cookie")))
        if request.method == "GET" and request.url.path == "/login.html":
            return httpx.Response(
                200,
                text=(
                    '<form name="FL" method="POST" action="/">'
                    '<input type="hidden" name="op" value="login">'
                    '<input type="hidden" name="token" value="login-token">'
                    '<input type="hidden" name="redirect" value="/">'
                    "</form>"
                ),
                headers={"Set-Cookie": "bootstrap=one; Domain=datanodes.to; Path=/"},
            )
        if request.method == "POST" and request.url.path == "/":
            assert "bootstrap=one" in request.headers["Cookie"]
            assert b"login=reader%40example.test" in request.content
            assert b"password=datanodes-password" in request.content
            return httpx.Response(
                200,
                text='<a href="/logout">Logout</a>',
                headers={"Set-Cookie": "account=registered; Domain=datanodes.to; Path=/"},
            )
        if request.method == "GET" and request.url.path == "/fixture/fixture.cbz":
            assert "account=registered" in request.headers["Cookie"]
            return httpx.Response(
                200,
                text=(
                    "<script>var countdown = 2;</script>"
                    '<form id="downloadForm" method="POST" action="/download">'
                    '<input type="hidden" name="op" value="download1">'
                    '<input type="hidden" name="id" value="fixture">'
                    '<input type="hidden" name="rand" value="fixture-rand">'
                    "</form>"
                ),
            )
        assert request.method == "POST"
        assert request.url.path == "/download"
        assert "account=registered" in request.headers["Cookie"]
        assert b"method_free=Free+Download" in request.content
        return httpx.Response(
            200,
            text=(
                '<a id="downloadbtn" '
                'href="https://s1.datanodes.to/d/fixture/fixture.cbz">Download</a>'
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = await DataNodesAdapter(
            client,
            resolver=_resolve_public,
            sleep=sleep,
        ).resolve(
            _request(
                DirectArtifactHostKind.DATANODES,
                "https://datanodes.to/fixture/fixture.cbz",
            ),
            credentials={
                "username": DATANODES_USERNAME,
                "password": DATANODES_PASSWORD,
            },
        )

    assert [item[:2] for item in seen] == [
        ("GET", "/login.html"),
        ("POST", "/"),
        ("GET", "/fixture/fixture.cbz"),
        ("POST", "/download"),
    ]
    assert sleep_calls == [2.0]
    assert transfer.url == "https://s1.datanodes.to/d/fixture/fixture.cbz"
    assert "account=registered" in transfer.headers["Cookie"]
    assert transfer.filename_hint == "fixture.cbz"
    assert DATANODES_USERNAME not in repr(transfer)
    assert DATANODES_PASSWORD not in repr(transfer)


async def test_datanodes_registered_account_reconstructs_dynamic_download_form() -> None:
    seen_download_posts: list[bytes] = []
    sleep_calls: list[float] = []

    async def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Referer") == "https://datanodes.to/users"
        if request.method == "GET" and request.url.path == "/login.html":
            return httpx.Response(
                200,
                text=(
                    '<form name="FL" method="POST" action="/">'
                    '<input type="hidden" name="op" value="login">'
                    "</form>"
                ),
            )
        if request.method == "POST" and request.url.path == "/":
            return httpx.Response(
                200,
                text='<a href="/logout">Logout</a>',
                headers={"Set-Cookie": "account=registered; Domain=datanodes.to; Path=/"},
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                text=(
                    '<form id="downloadForm" method="POST" action="/download">'
                    '<input type="hidden" name="op" value="download1">'
                    '<input type="hidden" name="id" value="fixture">'
                    "</form>"
                ),
            )
        seen_download_posts.append(request.content)
        if len(seen_download_posts) == 1:
            return httpx.Response(
                200,
                text=('<div countdown="2"></div><script>const rand="fixture-rand";</script>'),
            )
        return httpx.Response(
            200,
            json={"url": "/d/fixture/fixture.cbz"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = await DataNodesAdapter(
            client,
            resolver=_resolve_public,
            sleep=sleep,
        ).resolve(
            _request(
                DirectArtifactHostKind.DATANODES,
                "https://datanodes.to/fixture/fixture.cbz",
            ),
            credentials={
                "username": DATANODES_USERNAME,
                "password": DATANODES_PASSWORD,
            },
        )

    assert sleep_calls == [2.0]
    assert len(seen_download_posts) == 2
    assert b"op=download1" in seen_download_posts[0]
    assert b"method_free=Free+Download" in seen_download_posts[0]
    assert b"op=download2" in seen_download_posts[1]
    assert b"id=fixture" in seen_download_posts[1]
    assert b"rand=fixture-rand" in seen_download_posts[1]
    assert b"method_free=Free+Download+%3E%3E" in seen_download_posts[1]
    assert transfer.url == "https://datanodes.to/d/fixture/fixture.cbz"
    assert transfer.headers["Referer"] == "https://datanodes.to/fixture/fixture.cbz"


async def test_datanodes_premium_account_uses_immediate_download_link() -> None:
    seen_paths: list[str] = []
    sleep_calls: list[float] = []

    async def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/login.html":
            return httpx.Response(
                200,
                text=(
                    '<form name="FL" method="POST" action="/">'
                    '<input type="hidden" name="op" value="login">'
                    "</form>"
                ),
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                text='<a href="/logout">Logout</a>',
                headers={"Set-Cookie": "account=premium; Domain=datanodes.to; Path=/"},
            )
        assert request.url.path == "/fixture/fixture.cbz"
        return httpx.Response(
            200,
            text=(
                '<a id="downloadbtn" '
                'href="https://s1.datanodes.to/d/fixture/fixture.cbz">Download</a>'
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = await DataNodesAdapter(
            client,
            resolver=_resolve_public,
            sleep=sleep,
        ).resolve(
            _request(
                DirectArtifactHostKind.DATANODES,
                "https://datanodes.to/fixture/fixture.cbz",
            ),
            credentials={
                "username": DATANODES_USERNAME,
                "password": DATANODES_PASSWORD,
            },
        )

    assert seen_paths == ["/login.html", "/", "/fixture/fixture.cbz"]
    assert sleep_calls == []
    assert transfer.url == "https://s1.datanodes.to/d/fixture/fixture.cbz"
    assert "account=premium" in transfer.headers["Cookie"]


async def test_datanodes_premium_account_resolves_vue_download_contract() -> None:
    seen_requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append((request.method, request.url.path))
        if request.url.path == "/login.html":
            return httpx.Response(
                200,
                text=(
                    '<form name="FL" method="POST" action="/">'
                    '<input type="hidden" name="op" value="login">'
                    "</form>"
                ),
            )
        if request.method == "POST" and request.url.path == "/":
            return httpx.Response(
                200,
                text='<a href="/logout">Logout</a>',
                headers={"Set-Cookie": "account=premium; Domain=datanodes.to; Path=/"},
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                text=(
                    '<download-countdown :countdown="0" code="fixture" '
                    'referer="https://getcomics.org/fixture" rand="" free-method="" '
                    'premium-method="1" :has-password="false" :has-captcha="false" '
                    ':is-premium="true" :size-gated="false" '
                    'dl-token="ephemeral-download-token"></download-countdown>'
                ),
            )

        assert request.headers["X-Dn-Dl"] == "1"
        assert request.headers["Referer"] == "https://datanodes.to/fixture/fixture.cbz"
        assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
        assert b'name="op"' in request.content
        assert b"download2" in request.content
        assert b'name="id"' in request.content
        assert b"fixture" in request.content
        assert b'name="method_premium"' in request.content
        assert b'name="dl_token"' in request.content
        assert b"ephemeral-download-token" in request.content
        return httpx.Response(
            200,
            json={"url": ("https%3A%2F%2Ftunnel5.dlproxy.uk%2Fsigned%2Fopaque-download")},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transfer = await DataNodesAdapter(client, resolver=_resolve_public).resolve(
            _request(
                DirectArtifactHostKind.DATANODES,
                "https://datanodes.to/fixture/fixture.cbz",
                expected_size=67,
            ),
            credentials={
                "username": DATANODES_USERNAME,
                "password": DATANODES_PASSWORD,
            },
        )

    assert seen_requests == [
        ("GET", "/login.html"),
        ("POST", "/"),
        ("GET", "/fixture/fixture.cbz"),
        ("POST", "/fixture/fixture.cbz"),
    ]
    assert transfer.url == "https://tunnel5.dlproxy.uk/signed/opaque-download"
    assert transfer.expected_size is None
    assert transfer.filename_hint == "fixture.cbz"
    assert transfer.headers["Referer"] == "https://datanodes.to/fixture/fixture.cbz"
    assert "Cookie" not in transfer.headers
    assert transfer.allowed_domains == ("datanodes.to", "dlproxy.uk")


@pytest.mark.parametrize(
    ("attribute", "value", "expected_code"),
    [
        ("code", "different-file", "artifact_host_contract_changed"),
        (":has-captcha", "true", "artifact_host_challenge"),
        (":is-premium", "false", "artifact_host_auth_required"),
    ],
)
async def test_datanodes_vue_contract_rejects_unsafe_or_unsupported_states(
    attribute: str,
    value: str,
    expected_code: str,
) -> None:
    component_posts = 0
    attributes = {
        "code": "fixture",
        "referer": "https://getcomics.org/fixture",
        "rand": "",
        "free-method": "",
        "premium-method": "1",
        ":has-password": "false",
        ":has-captcha": "false",
        ":is-premium": "true",
        ":size-gated": "false",
        "dl-token": "ephemeral-download-token",
    }
    attributes[attribute] = value
    component_attributes = " ".join(
        f'{name}="{attribute_value}"' for name, attribute_value in attributes.items()
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal component_posts
        if request.url.path == "/login.html":
            return httpx.Response(
                200,
                text=(
                    '<form name="FL" method="POST" action="/">'
                    '<input type="hidden" name="op" value="login">'
                    "</form>"
                ),
            )
        if request.method == "POST" and request.url.path == "/":
            return httpx.Response(
                200,
                text='<a href="/logout">Logout</a>',
                headers={"Set-Cookie": "account=premium; Domain=datanodes.to; Path=/"},
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                text=f"<download-countdown {component_attributes}></download-countdown>",
            )
        component_posts += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArtifactHostResolutionError) as raised:
            await DataNodesAdapter(client, resolver=_resolve_public).resolve(
                _request(
                    DirectArtifactHostKind.DATANODES,
                    "https://datanodes.to/fixture/fixture.cbz",
                ),
                credentials={
                    "username": DATANODES_USERNAME,
                    "password": DATANODES_PASSWORD,
                },
            )

    assert raised.value.code == expected_code
    assert component_posts == 0


async def test_datanodes_invalid_login_requires_visible_reauthentication() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                text=(
                    '<form name="FL" method="POST" action="/">'
                    '<input type="hidden" name="op" value="login">'
                    "</form>"
                ),
            )
        return httpx.Response(
            200,
            text=(
                "<p>Incorrect Login or Password</p>"
                '<form name="FL" method="POST" action="/">'
                '<input type="hidden" name="op" value="login">'
                "</form>"
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArtifactHostResolutionError) as raised:
            await DataNodesAdapter(client, resolver=_resolve_public).resolve(
                _request(
                    DirectArtifactHostKind.DATANODES,
                    "https://datanodes.to/fixture/fixture.cbz",
                ),
                credentials={
                    "username": DATANODES_USERNAME,
                    "password": DATANODES_PASSWORD,
                },
            )

    assert raised.value.code == "artifact_host_auth_required"
    assert raised.value.failure_class is DirectArtifactFailureClass.ARTIFACT_HOST_AUTH_REQUIRED
    assert raised.value.message == "DataNodes rejected the configured username or password."
    assert DATANODES_USERNAME not in repr(raised.value)
    assert DATANODES_PASSWORD not in repr(raised.value)


async def test_datanodes_unexpected_challenge_fails_without_browser_bypass() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login.html":
            return httpx.Response(
                200,
                text=(
                    '<form name="FL" method="POST" action="/">'
                    '<input type="hidden" name="op" value="login">'
                    "</form>"
                ),
            )
        if request.method == "POST":
            return httpx.Response(200, text='<a href="/logout">Logout</a>')
        return httpx.Response(
            200,
            text='<div class="cf-turnstile" data-sitekey="fixture"></div>',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArtifactHostResolutionError) as raised:
            await DataNodesAdapter(client, resolver=_resolve_public).resolve(
                _request(
                    DirectArtifactHostKind.DATANODES,
                    "https://datanodes.to/fixture/fixture.cbz",
                ),
                credentials={
                    "username": DATANODES_USERNAME,
                    "password": DATANODES_PASSWORD,
                },
            )

    assert raised.value.code == "artifact_host_challenge"
    assert raised.value.failure_class is DirectArtifactFailureClass.ARTIFACT_HOST_CHALLENGE
    assert raised.value.intervention is True
