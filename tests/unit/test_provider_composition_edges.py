"""Provider composition coverage for every supported download-client lane."""

from __future__ import annotations

import pytest

from pullbox.composition import providers
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType
from pullbox.providers.base import ProviderRegistry


def _client(name: str, client_type: DownloadClientType, port: int) -> DownloadClientConfig:
    return DownloadClientConfig(
        name=name,
        client_type=client_type,
        url=f"http://localhost:{port}",
        enabled=True,
        priority=port,
        api_key="encrypted-api-key",
        username="user",
        password="encrypted-password",
        category="comics",
        sab_priority="normal",
        sab_post_processing="2",
        nzbget_priority="high",
        nzbget_post_processing="pp2",
        qbt_content_layout="Original",
        qbt_ratio_limit=1.5,
        qbt_seeding_time_limit=60,
        transmission_download_dir="/downloads",
        transmission_bandwidth_priority=1,
        transmission_seed_ratio_limit=1.5,
        transmission_seed_idle_limit=30,
        deluge_label="comics",
        deluge_max_ratio=1.5,
        deluge_move_completed_path="/downloads/completed",
    )


@pytest.mark.asyncio
async def test_download_client_composition_builds_and_reuses_every_supported_client(
    db_session, monkeypatch
) -> None:
    providers._download_client_cache.clear()
    monkeypatch.setattr(providers, "decrypt_secret", lambda value: f"plain:{value}")
    db_session.add_all(
        [
            _client("SABnzbd", DownloadClientType.SABNZBD, 8080),
            _client("NZBGet", DownloadClientType.NZBGET, 6789),
            _client("qBittorrent", DownloadClientType.QBITTORRENT, 8081),
            _client("Transmission", DownloadClientType.TRANSMISSION, 9091),
            _client("Deluge", DownloadClientType.DELUGE, 8112),
        ]
    )
    await db_session.flush()

    first_registry = ProviderRegistry()
    assert await providers.register_download_clients(db_session, first_registry) == []
    first_items = first_registry.get_download_client_items()
    assert len(first_items) == 5

    second_registry = ProviderRegistry()
    assert await providers.register_download_clients(db_session, second_registry) == []
    second_items = second_registry.get_download_client_items()
    assert [item[1] for item in second_items] == [item[1] for item in first_items]


@pytest.mark.asyncio
async def test_download_client_composition_reports_secret_failures(db_session, monkeypatch) -> None:
    providers._download_client_cache.clear()
    client = _client("Broken SABnzbd", DownloadClientType.SABNZBD, 8080)
    db_session.add(client)
    await db_session.flush()
    monkeypatch.setattr(
        providers,
        "decrypt_secret",
        lambda _value: (_ for _ in ()).throw(ValueError("invalid ciphertext")),
    )

    failures = await providers.register_download_clients(db_session, ProviderRegistry())

    assert failures == [
        {
            "config_id": str(client.id),
            "name": "Broken SABnzbd",
            "client_type": "sabnzbd",
            "url": "http://localhost:8080",
            "status": "unhealthy",
            "message": (
                "Configuration error: saved credentials could not be loaded. "
                "Re-save this client in Settings > Download Clients."
            ),
        }
    ]


@pytest.mark.asyncio
async def test_empty_indexer_composition_returns_no_registry(db_session) -> None:
    assert await providers.build_registry(db_session) is None
