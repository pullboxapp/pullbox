"""Tests for SABnzbd download client implementation.

Covers the local NZB fetch + upload contract so SAB does not need to
reach indexer download URLs directly.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from pullbox.providers.download.sabnzbd import SABnzbdClient, SABnzbdError

_FAKE_NZB_CONTENT = b"<?xml version='1.0'?><nzb>fake</nzb>"


def _make_client(**kwargs: Any) -> SABnzbdClient:
    defaults: dict[str, Any] = {
        "url": "http://localhost:8080",
        "api_key": "secret",
    }
    defaults.update(kwargs)
    return SABnzbdClient(**defaults)


def _make_response(
    *,
    status_code: int = 200,
    content: bytes = _FAKE_NZB_CONTENT,
    content_type: str = "application/x-nzb",
    json_data: dict[str, Any] | None = None,
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.content = content
    response.headers = {"content-type": content_type}
    response.raise_for_status = MagicMock()
    response.json.return_value = json_data or {}
    return response


@pytest.mark.asyncio
class TestAddNzb:
    """Tests for SABnzbdClient.add_nzb()."""

    async def test_nzb_fetch_allows_proxy_delay_without_extending_sab_control_timeout(self) -> None:
        requests: list[httpx.Request] = []

        def transport(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.host == "indexer.test":
                # Model a proxy that needs more time than a local control API.
                if request.extensions["timeout"]["read"] < 30:
                    raise httpx.ReadTimeout("Slow proxy", request=request)
                return httpx.Response(200, content=_FAKE_NZB_CONTENT)
            return httpx.Response(200, json={"status": True, "nzo_ids": ["test-id"]})

        client = _make_client()
        await client._client.aclose()
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), timeout=10) as http:
            client._client = http
            assert (
                await client.add_nzb("https://indexer.test/release.nzb", "Test issue") == "test-id"
            )
        assert len(requests) == 2
        assert requests[0].extensions["timeout"]["read"] == 60
        assert requests[0].extensions["timeout"]["connect"] == 10
        assert requests[1].extensions["timeout"]["read"] == 10

    async def test_nzb_fetch_has_total_deadline_and_never_submits_after_timeout(
        self, monkeypatch
    ) -> None:
        from pullbox.providers.download import sabnzbd

        monkeypatch.setattr(sabnzbd, "_NZB_FETCH_DEADLINE", 0.01, raising=False)
        client = _make_client()

        async def never_finishes(*args: Any, **kwargs: Any) -> None:
            await asyncio.Event().wait()

        client._client.get = AsyncMock(side_effect=never_finishes)
        client._client.post = AsyncMock()
        try:
            async with asyncio.timeout(1):
                with pytest.raises(
                    SABnzbdError, match="Failed to download NZB from URL: Request timed out"
                ):
                    await client.add_nzb("https://indexer.test/slow.nzb", "Test issue")
            client._client.post.assert_not_awaited()
        finally:
            await client._client.aclose()

    async def test_add_nzb_downloads_locally_then_uploads_to_sab(self) -> None:
        client = _make_client(category="comics", priority="1", post_processing="3")
        fetch_response = _make_response()
        upload_response = _make_response(json_data={"status": True, "nzo_ids": ["nzo-123"]})
        client._client.get = AsyncMock(return_value=fetch_response)  # type: ignore[method-assign]
        client._client.post = AsyncMock(return_value=upload_response)  # type: ignore[method-assign]

        result = await client.add_nzb("http://example.com/test.nzb", "Batman 001")

        assert result == "nzo-123"
        client._client.get.assert_awaited_once_with(  # type: ignore[attr-defined]
            "http://example.com/test.nzb",
            follow_redirects=True,
            timeout=httpx.Timeout(10.0, read=60.0),
        )
        client._client.post.assert_awaited_once()  # type: ignore[attr-defined]

        _, kwargs = client._client.post.call_args  # type: ignore[attr-defined]
        assert kwargs["params"]["mode"] == "addfile"
        assert kwargs["params"]["cat"] == "comics"
        assert kwargs["params"]["priority"] == "1"
        assert kwargs["params"]["pp"] == "3"
        assert kwargs["params"]["nzbname"] == "Batman 001"
        file_name, file_bytes, content_type = kwargs["files"]["nzbfile"]
        assert file_name == "Batman 001.nzb"
        assert file_bytes == _FAKE_NZB_CONTENT
        assert content_type == "application/x-nzb"

    async def test_add_nzb_preserves_existing_extension(self) -> None:
        client = _make_client()
        client._client.get = AsyncMock(return_value=_make_response())  # type: ignore[method-assign]
        client._client.post = AsyncMock(  # type: ignore[method-assign]
            return_value=_make_response(json_data={"status": True, "nzo_ids": ["nzo-456"]})
        )

        await client.add_nzb("http://example.com/test.nzb", "Batman 001.nzb")

        _, kwargs = client._client.post.call_args  # type: ignore[attr-defined]
        file_name, *_ = kwargs["files"]["nzbfile"]
        assert file_name == "Batman 001.nzb"

    async def test_add_nzb_accepts_xml_body_with_generic_content_type(self) -> None:
        client = _make_client()
        client._client.get = AsyncMock(  # type: ignore[method-assign]
            return_value=_make_response(content_type="application/octet-stream")
        )
        client._client.post = AsyncMock(  # type: ignore[method-assign]
            return_value=_make_response(json_data={"status": True, "nzo_ids": ["nzo-789"]})
        )

        result = await client.add_nzb("http://example.com/test.nzb", "Batman 001")

        assert result == "nzo-789"

    async def test_add_nzb_rejects_non_nzb_response(self) -> None:
        client = _make_client()
        client._client.get = AsyncMock(  # type: ignore[method-assign]
            return_value=_make_response(
                content=b"<html><body>login page</body></html>",
                content_type="text/html",
            )
        )

        with pytest.raises(SABnzbdError, match="URL did not return NZB content"):
            await client.add_nzb("http://example.com/test.nzb", "Batman 001")

    async def test_add_nzb_download_timeout_raises_clear_error(self) -> None:
        client = _make_client()
        client._client.get = AsyncMock(side_effect=httpx.TimeoutException("boom"))  # type: ignore[method-assign]

        with pytest.raises(
            SABnzbdError,
            match="Failed to download NZB from URL: Request timed out",
        ):
            await client.add_nzb("http://example.com/test.nzb", "Batman 001")

    async def test_add_nzb_upload_http_error_raises(self) -> None:
        client = _make_client()
        fetch_response = _make_response()
        upload_response = _make_response(status_code=500)
        upload_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "server error",
            request=MagicMock(),
            response=MagicMock(status_code=500),
        )
        client._client.get = AsyncMock(return_value=fetch_response)  # type: ignore[method-assign]
        client._client.post = AsyncMock(return_value=upload_response)  # type: ignore[method-assign]

        with pytest.raises(SABnzbdError, match="HTTP 500"):
            await client.add_nzb("http://example.com/test.nzb", "Batman 001")

    async def test_add_nzb_upload_api_error_raises(self) -> None:
        client = _make_client()
        client._client.get = AsyncMock(return_value=_make_response())  # type: ignore[method-assign]
        client._client.post = AsyncMock(  # type: ignore[method-assign]
            return_value=_make_response(json_data={"status": False, "error": "Upload rejected"})
        )

        with pytest.raises(SABnzbdError, match="Upload rejected"):
            await client.add_nzb("http://example.com/test.nzb", "Batman 001")

    async def test_add_nzb_missing_nzo_id_raises(self) -> None:
        client = _make_client()
        client._client.get = AsyncMock(return_value=_make_response())  # type: ignore[method-assign]
        client._client.post = AsyncMock(  # type: ignore[method-assign]
            return_value=_make_response(json_data={"status": True, "nzo_ids": []})
        )

        with pytest.raises(SABnzbdError, match="No nzo_id returned"):
            await client.add_nzb("http://example.com/test.nzb", "Batman 001")


@pytest.mark.asyncio
class TestAddTorrent:
    """Verify add_torrent raises NotImplementedError."""

    async def test_add_torrent_raises(self) -> None:
        client = _make_client()

        with pytest.raises(NotImplementedError, match="does not support torrent"):
            await client.add_torrent("magnet:?xt=urn:btih:abc", "Test")


@pytest.mark.asyncio
class TestDownloadStatus:
    """Tests for SABnzbd status and queue mapping."""

    async def test_get_download_status_returns_active_queue_slot(self) -> None:
        client = _make_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "queue": {
                    "slots": [
                        {
                            "nzo_id": "nzo-1",
                            "filename": "Batman 001.nzb",
                            "status": "Downloading",
                            "percentage": "45.5",
                            "mb": "100",
                            "kbpersec": "512",
                            "timeleft": "01:02:03",
                        }
                    ]
                }
            }
        )

        status = await client.get_download_status("nzo-1")

        assert status.external_id == "nzo-1"
        assert status.title == "Batman 001.nzb"
        assert status.state == "downloading"
        assert status.progress == pytest.approx(0.455)
        assert status.size_bytes == 100 * 1024 * 1024
        assert status.speed_bytes == 512 * 1024
        assert status.eta_seconds == 3723
        assert status.client_state is None
        client._request.assert_awaited_once_with({"mode": "queue"})  # type: ignore[attr-defined]

    async def test_get_download_status_falls_back_to_history_finalizing(self) -> None:
        client = _make_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"queue": {"slots": []}},
                {
                    "history": {
                        "slots": [
                            {
                                "nzo_id": "nzo-2",
                                "name": "Absolute Superman 014",
                                "status": "Extracting",
                                "bytes": "2048",
                                "storage": "/downloads/Absolute Superman 014.cbz",
                            }
                        ]
                    }
                },
            ]
        )

        status = await client.get_download_status("nzo-2")

        assert status.external_id == "nzo-2"
        assert status.state == "finalizing"
        assert status.progress == 1.0
        assert status.size_bytes == 2048
        assert status.downloaded_path == "/downloads/Absolute Superman 014.cbz"
        assert status.client_state == "Extracting"
        assert client._request.await_args_list[0].args[0] == {"mode": "queue"}  # type: ignore[attr-defined]
        assert client._request.await_args_list[1].args[0] == {  # type: ignore[attr-defined]
            "mode": "history",
            "limit": 50,
        }

    async def test_get_download_status_raises_when_missing_from_queue_and_history(self) -> None:
        client = _make_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"queue": {"slots": []}},
                {"history": {"slots": []}},
            ]
        )

        with pytest.raises(SABnzbdError, match="Download not found: missing"):
            await client.get_download_status("missing")

    async def test_get_queue_maps_post_download_phase_to_finalizing_progress(self) -> None:
        client = _make_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            return_value={
                "queue": {
                    "slots": [
                        {
                            "nzo_id": "nzo-3",
                            "filename": "Wonder Woman 001",
                            "status": "Repairing",
                            "percentage": "12.3",
                            "mb": "200",
                            "kbpersec": "0",
                            "timeleft": "0:00:00",
                        }
                    ]
                }
            }
        )

        queue = await client.get_queue()

        assert len(queue) == 1
        assert queue[0].state == "finalizing"
        assert queue[0].progress == pytest.approx(0.123)
        assert queue[0].client_state == "Repairing"


@pytest.mark.asyncio
class TestRemoveDownload:
    """Tests for SABnzbd queue/history removal fallback behavior."""

    async def test_remove_download_from_queue_success(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={})  # type: ignore[method-assign]

        result = await client.remove_download("nzo-1")

        assert result is True
        client._request.assert_awaited_once_with(  # type: ignore[attr-defined]
            {
                "mode": "queue",
                "name": "delete",
                "value": "nzo-1",
                "del_files": "",
            }
        )

    async def test_remove_download_falls_back_to_history_with_delete_files(self) -> None:
        client = _make_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[SABnzbdError("not in queue"), {}]
        )

        result = await client.remove_download("nzo-2", delete_files=True)

        assert result is True
        assert client._request.await_args_list[0].args[0] == {  # type: ignore[attr-defined]
            "mode": "queue",
            "name": "delete",
            "value": "nzo-2",
            "del_files": "delete",
        }
        assert client._request.await_args_list[1].args[0] == {  # type: ignore[attr-defined]
            "mode": "history",
            "name": "delete",
            "value": "nzo-2",
            "del_files": "1",
        }

    async def test_remove_download_returns_false_when_queue_and_history_fail(self) -> None:
        client = _make_client()
        client._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[SABnzbdError("not in queue"), SABnzbdError("not in history")]
        )

        result = await client.remove_download("nzo-3", delete_files=True)

        assert result is False


@pytest.mark.asyncio
class TestHealthAndOptions:
    """Tests for SABnzbd health and option discovery."""

    async def test_test_connection_success_includes_version_details(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"version": "4.5.1"})  # type: ignore[method-assign]

        result = await client.test_connection()

        assert result.healthy is True
        assert result.message == "SABnzbd v4.5.1"
        assert result.details == {"version": "4.5.1"}
        assert result.response_time_ms >= 0

    async def test_test_connection_provider_error_is_unhealthy(self) -> None:
        client = _make_client()
        client._request = AsyncMock(side_effect=SABnzbdError("bad api key"))  # type: ignore[method-assign]

        result = await client.test_connection()

        assert result.healthy is False
        assert result.message == "SABnzbd error: bad api key"
        assert result.response_time_ms >= 0

    async def test_test_connection_unexpected_error_is_unhealthy(self) -> None:
        client = _make_client()
        client._request = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

        result = await client.test_connection()

        assert result.healthy is False
        assert result.message == "Connection failed: boom"
        assert result.response_time_ms >= 0

    async def test_get_options_returns_categories(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"categories": ["comics", "books"]})  # type: ignore[method-assign]

        options = await client.get_options()

        assert options.categories == ["comics", "books"]

    async def test_get_options_uses_default_when_categories_empty(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"categories": []})  # type: ignore[method-assign]

        options = await client.get_options()

        assert options.categories == ["Default"]
