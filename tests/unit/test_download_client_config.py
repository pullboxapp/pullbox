"""Unit tests for DownloadClientConfig model update (DCE-M.2).

Tests new client-specific columns for NZBGet, Transmission, and Deluge,
including persistence, NULL defaults, cross-client isolation, and edge cases.

Run:
    pytest tests/unit/test_download_client_config.py -v
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType
from tests.sqlite_schema import create_test_schema

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-dce-m2")


@pytest.fixture
async def db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Create an in-memory database with all tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await create_test_schema(conn)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(db: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession, None]:
    """Provide a single session for each test."""
    async with db() as sess:
        yield sess


def _make_config(
    client_type: DownloadClientType,
    name: str = "test-client",
    **kwargs: object,
) -> DownloadClientConfig:
    """Helper to create a DownloadClientConfig with minimal required fields."""
    return DownloadClientConfig(
        name=name,
        client_type=client_type,
        url="http://localhost:8080",
        **kwargs,
    )


# ── NZBGet columns ──────────────────────────────────────────────


@pytest.mark.asyncio
class TestNZBGetColumns:
    """Tests for NZBGet-specific columns on DownloadClientConfig."""

    async def test_nzbget_priority_persists(self, session: AsyncSession) -> None:
        config = _make_config(DownloadClientType.NZBGET, nzbget_priority="high")
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.nzbget_priority == "high"

    async def test_nzbget_post_processing_persists(self, session: AsyncSession) -> None:
        config = _make_config(DownloadClientType.NZBGET, nzbget_post_processing="pp3")
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.nzbget_post_processing == "pp3"

    async def test_nzbget_all_priority_values(self, session: AsyncSession) -> None:
        """All valid NZBGet priority values persist correctly."""
        priorities = ["force", "very-high", "high", "normal", "low", "very-low"]
        configs = []
        for i, prio in enumerate(priorities):
            config = _make_config(
                DownloadClientType.NZBGET,
                name=f"nzbget-prio-{i}",
                nzbget_priority=prio,
            )
            session.add(config)
            configs.append(config)
        await session.flush()

        for config, prio in zip(configs, priorities, strict=True):
            await session.refresh(config)
            assert config.nzbget_priority == prio

    async def test_nzbget_invalid_priority_stored_as_is(self, session: AsyncSession) -> None:
        """Model stores any string — validation is at the service layer."""
        config = _make_config(DownloadClientType.NZBGET, nzbget_priority="super-high")
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.nzbget_priority == "super-high"

    async def test_nzbget_columns_null_by_default(self, session: AsyncSession) -> None:
        config = _make_config(DownloadClientType.NZBGET)
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.nzbget_priority is None
        assert config.nzbget_post_processing is None


# ── Transmission columns ─────────────────────────────────────────


@pytest.mark.asyncio
class TestTransmissionColumns:
    """Tests for Transmission-specific columns on DownloadClientConfig."""

    async def test_transmission_download_dir_persists(self, session: AsyncSession) -> None:
        config = _make_config(
            DownloadClientType.TRANSMISSION,
            transmission_download_dir="/mnt/downloads/comics",
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.transmission_download_dir == "/mnt/downloads/comics"

    async def test_transmission_bandwidth_priority_persists(self, session: AsyncSession) -> None:
        config = _make_config(
            DownloadClientType.TRANSMISSION,
            transmission_bandwidth_priority=1,
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.transmission_bandwidth_priority == 1

    async def test_transmission_bandwidth_priority_negative(self, session: AsyncSession) -> None:
        """Negative bandwidth priority (-1 = low) persists correctly."""
        config = _make_config(
            DownloadClientType.TRANSMISSION,
            transmission_bandwidth_priority=-1,
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.transmission_bandwidth_priority == -1

    async def test_transmission_bandwidth_priority_out_of_range_stored(
        self, session: AsyncSession
    ) -> None:
        """Out-of-range values stored as-is — validated at API layer."""
        config = _make_config(
            DownloadClientType.TRANSMISSION,
            transmission_bandwidth_priority=-2,
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.transmission_bandwidth_priority == -2

    async def test_transmission_seed_ratio_limit_persists(self, session: AsyncSession) -> None:
        config = _make_config(
            DownloadClientType.TRANSMISSION,
            transmission_seed_ratio_limit=2.5,
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.transmission_seed_ratio_limit == pytest.approx(2.5)

    async def test_transmission_seed_idle_limit_persists(self, session: AsyncSession) -> None:
        config = _make_config(
            DownloadClientType.TRANSMISSION,
            transmission_seed_idle_limit=60,
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.transmission_seed_idle_limit == 60

    async def test_transmission_columns_null_by_default(self, session: AsyncSession) -> None:
        config = _make_config(DownloadClientType.TRANSMISSION)
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.transmission_download_dir is None
        assert config.transmission_bandwidth_priority is None
        assert config.transmission_seed_ratio_limit is None
        assert config.transmission_seed_idle_limit is None

    async def test_transmission_long_download_dir(self, session: AsyncSession) -> None:
        """Very long directory path (500+ chars) within String(1000) limit."""
        long_path = "/mnt/" + "a" * 600 + "/comics"
        config = _make_config(
            DownloadClientType.TRANSMISSION,
            transmission_download_dir=long_path,
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.transmission_download_dir == long_path
        assert len(config.transmission_download_dir) > 500


# ── Deluge columns ───────────────────────────────────────────────


@pytest.mark.asyncio
class TestDelugeColumns:
    """Tests for Deluge-specific columns on DownloadClientConfig."""

    async def test_deluge_label_persists(self, session: AsyncSession) -> None:
        config = _make_config(DownloadClientType.DELUGE, deluge_label="comics")
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.deluge_label == "comics"

    async def test_deluge_max_ratio_persists(self, session: AsyncSession) -> None:
        config = _make_config(DownloadClientType.DELUGE, deluge_max_ratio=1.5)
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.deluge_max_ratio == pytest.approx(1.5)

    async def test_deluge_move_completed_path_persists(self, session: AsyncSession) -> None:
        config = _make_config(
            DownloadClientType.DELUGE,
            deluge_move_completed_path="/mnt/complete/comics",
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.deluge_move_completed_path == "/mnt/complete/comics"

    async def test_deluge_label_with_spaces(self, session: AsyncSession) -> None:
        """Label with spaces stored correctly."""
        config = _make_config(DownloadClientType.DELUGE, deluge_label="comic books")
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.deluge_label == "comic books"

    async def test_deluge_label_with_unicode(self, session: AsyncSession) -> None:
        """Label with unicode characters stored correctly."""
        config = _make_config(DownloadClientType.DELUGE, deluge_label="漫画-comics")
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.deluge_label == "漫画-comics"

    async def test_deluge_columns_null_by_default(self, session: AsyncSession) -> None:
        config = _make_config(DownloadClientType.DELUGE)
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.deluge_label is None
        assert config.deluge_max_ratio is None
        assert config.deluge_move_completed_path is None

    async def test_deluge_long_move_completed_path(self, session: AsyncSession) -> None:
        """Very long path within String(1000) column limit."""
        long_path = "/mnt/" + "deep/nested/" * 80 + "comics"
        config = _make_config(
            DownloadClientType.DELUGE,
            deluge_move_completed_path=long_path,
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.deluge_move_completed_path == long_path


# ── Cross-client isolation ───────────────────────────────────────


@pytest.mark.asyncio
class TestCrossClientIsolation:
    """Verify client-specific columns are NULL for unrelated client types."""

    async def test_sabnzbd_config_has_null_nzbget_columns(self, session: AsyncSession) -> None:
        config = _make_config(
            DownloadClientType.SABNZBD,
            name="sab-1",
            api_key="test-key",
            sab_priority="high",
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.sab_priority == "high"
        assert config.nzbget_priority is None
        assert config.nzbget_post_processing is None
        assert config.transmission_download_dir is None
        assert config.deluge_label is None

    async def test_qbittorrent_config_has_null_new_columns(self, session: AsyncSession) -> None:
        config = _make_config(
            DownloadClientType.QBITTORRENT,
            name="qbt-1",
            username="admin",
            password="pass",
            qbt_ratio_limit=2.0,
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.qbt_ratio_limit == pytest.approx(2.0)
        assert config.nzbget_priority is None
        assert config.transmission_bandwidth_priority is None
        assert config.deluge_label is None

    async def test_nzbget_config_has_null_sab_and_torrent_columns(
        self, session: AsyncSession
    ) -> None:
        config = _make_config(
            DownloadClientType.NZBGET,
            name="nzbget-1",
            nzbget_priority="normal",
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.nzbget_priority == "normal"
        assert config.sab_priority is None
        assert config.qbt_content_layout is None
        assert config.transmission_download_dir is None
        assert config.deluge_label is None

    async def test_changing_client_type_retains_old_columns(self, session: AsyncSession) -> None:
        """Updating client_type from SABNZBD to NZBGET — old sab_* columns retain values."""
        config = _make_config(
            DownloadClientType.SABNZBD,
            name="switcheroo",
            sab_priority="force",
        )
        session.add(config)
        await session.flush()

        config.client_type = DownloadClientType.NZBGET
        config.nzbget_priority = "high"
        await session.flush()
        await session.refresh(config)

        assert config.client_type == DownloadClientType.NZBGET
        assert config.nzbget_priority == "high"
        # Old sab column retains value — ignored by NZBGet client at runtime
        assert config.sab_priority == "force"


# ── Existing config compatibility ────────────────────────────────


@pytest.mark.asyncio
class TestExistingConfigCompatibility:
    """Existing SABnzbd/qBittorrent configs are unaffected by new columns."""

    async def test_sabnzbd_config_unchanged(self, session: AsyncSession) -> None:
        config = _make_config(
            DownloadClientType.SABNZBD,
            name="sab-compat",
            api_key="my-api-key",
            category="comics",
            sab_priority="normal",
            sab_post_processing="3",
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.name == "sab-compat"
        assert config.client_type == DownloadClientType.SABNZBD
        assert config.api_key == "my-api-key"
        assert config.category == "comics"
        assert config.sab_priority == "normal"
        assert config.sab_post_processing == "3"

    async def test_qbittorrent_config_unchanged(self, session: AsyncSession) -> None:
        config = _make_config(
            DownloadClientType.QBITTORRENT,
            name="qbt-compat",
            username="admin",
            password="secret",
            category="comics",
            qbt_content_layout="Original",
            qbt_ratio_limit=1.5,
            qbt_seeding_time_limit=120,
        )
        session.add(config)
        await session.flush()
        await session.refresh(config)

        assert config.name == "qbt-compat"
        assert config.client_type == DownloadClientType.QBITTORRENT
        assert config.username == "admin"
        assert config.category == "comics"
        assert config.qbt_content_layout == "Original"
        assert config.qbt_ratio_limit == pytest.approx(1.5)
        assert config.qbt_seeding_time_limit == 120

    async def test_all_five_client_types_coexist(self, session: AsyncSession) -> None:
        """All 5 client types can exist in the same table simultaneously."""
        configs = [
            _make_config(DownloadClientType.SABNZBD, name="sab"),
            _make_config(DownloadClientType.NZBGET, name="nzbget"),
            _make_config(DownloadClientType.QBITTORRENT, name="qbt"),
            _make_config(DownloadClientType.TRANSMISSION, name="trans"),
            _make_config(DownloadClientType.DELUGE, name="deluge"),
        ]
        session.add_all(configs)
        await session.flush()

        for c in configs:
            await session.refresh(c)
            assert c.id is not None
            assert c.enabled is True

    async def test_all_new_columns_null_when_unset(self, session: AsyncSession) -> None:
        """A config with no client-specific columns set has all NULLs."""
        config = _make_config(DownloadClientType.NZBGET, name="bare-minimum")
        session.add(config)
        await session.flush()
        await session.refresh(config)

        # NZBGet
        assert config.nzbget_priority is None
        assert config.nzbget_post_processing is None
        # Transmission
        assert config.transmission_download_dir is None
        assert config.transmission_bandwidth_priority is None
        assert config.transmission_seed_ratio_limit is None
        assert config.transmission_seed_idle_limit is None
        # Deluge
        assert config.deluge_label is None
        assert config.deluge_max_ratio is None
        assert config.deluge_move_completed_path is None
