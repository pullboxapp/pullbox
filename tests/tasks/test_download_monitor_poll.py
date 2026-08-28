"""Download monitor polling-phase characterization tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class _FakeClient:
    def __init__(self) -> None:
        self.status_calls: list[str] = []

    async def find_torrent_by_title(self, title: str) -> str:
        assert title == "Batman 001.cbz"
        return "matched-hash"

    async def get_download_status(self, external_id: str):
        self.status_calls.append(external_id)
        return SimpleNamespace(
            state="downloading",
            progress=0.25,
            speed_bytes=1024,
            eta_seconds=30,
            size_bytes=4096,
            downloaded_path=None,
            error_message=None,
            client_state="Downloading",
        )


class _FakeService:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client

    def get_client_for_type(self, client_type: object) -> _FakeClient:
        assert client_type == "qbittorrent"
        return self.client


class _FakeLogger:
    def debug(self, event: str, **kwargs: object) -> None:
        del event
        del kwargs


@pytest.mark.asyncio
async def test_poll_download_clients_matches_missing_external_id_by_title() -> None:
    """The polling phase should preserve title matching before status checks."""
    from pullbox.tasks import download_monitor_poll
    from pullbox.tasks.download_monitor_updates import build_status_update

    client = _FakeClient()

    updates = await download_monitor_poll.poll_download_clients(
        [
            {
                "id": 7,
                "external_id": None,
                "title": "Batman 001.cbz",
                "download_client": "qbittorrent",
                "downloaded_path": None,
                "issue_id": 99,
            }
        ],
        _FakeService(client),
        record_download_progress=lambda download_id, status, event_logger: False,
        build_status_update=build_status_update,
        build_status_check_error_update=lambda **kwargs: None,
        event_logger=_FakeLogger(),
    )

    assert updates == [
        {"id": 7, "external_id": "matched-hash"},
        {
            "id": 7,
            "client_state": "Downloading",
            "state": "downloading",
            "progress_snapshot": {
                "progress": 0.25,
                "speed_bytes": 1024,
                "eta_seconds": 30,
                "size_bytes": 4096,
                "bytes_transferred": 1024,
                "client_state": "Downloading",
                "is_indeterminate": False,
            },
        },
    ]
    assert client.status_calls == ["matched-hash"]
