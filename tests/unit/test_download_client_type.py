"""Unit tests for DownloadClientType enum expansion (DCE-M.1).

Tests all 7 enum values, protocol classification helper properties,
StrEnum string comparison behavior, and SQLAlchemy round-trip persistence.

Run:
    pytest tests/unit/test_download_client_type.py -v
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models import Base
from pullbox.models.download import DownloadClientType

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-dce")


def test_existing_client_types_expose_acquisition_protocol() -> None:
    assert DownloadClientType.SABNZBD.acquisition_protocol is AcquisitionProtocol.USENET
    assert DownloadClientType.NZBGET.acquisition_protocol is AcquisitionProtocol.USENET
    assert DownloadClientType.QBITTORRENT.acquisition_protocol is AcquisitionProtocol.TORRENT
    assert DownloadClientType.TRANSMISSION.acquisition_protocol is AcquisitionProtocol.TORRENT
    assert DownloadClientType.DELUGE.acquisition_protocol is AcquisitionProtocol.TORRENT
    assert DownloadClientType.DIRECT.acquisition_protocol is AcquisitionProtocol.DIRECT


def test_airdcpp_client_type_maps_to_dc_protocol() -> None:
    assert DownloadClientType.AIRDCPP.value == "airdcpp"
    assert DownloadClientType.AIRDCPP.acquisition_protocol is AcquisitionProtocol.DC
    assert DownloadClientType.AIRDCPP.is_usenet is False
    assert DownloadClientType.AIRDCPP.is_torrent is False
    assert DownloadClientType.AIRDCPP.is_direct is False


# Minimal model for round-trip testing without pulling in full DownloadHistory
class _ClientTypeRecord(Base):
    __tablename__ = "test_client_type_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_type: Mapped[str] = mapped_column(String(50))


@pytest.fixture
async def db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Create an in-memory database with the test table."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(db: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession, None]:
    """Provide a single session for each test."""
    async with db() as sess:
        yield sess


class TestDownloadClientTypeValues:
    """Tests for DownloadClientType enum values."""

    def test_sabnzbd_value(self) -> None:
        assert DownloadClientType.SABNZBD == "sabnzbd"
        assert DownloadClientType.SABNZBD.value == "sabnzbd"

    def test_nzbget_value(self) -> None:
        assert DownloadClientType.NZBGET == "nzbget"
        assert DownloadClientType.NZBGET.value == "nzbget"

    def test_qbittorrent_value(self) -> None:
        assert DownloadClientType.QBITTORRENT == "qbittorrent"
        assert DownloadClientType.QBITTORRENT.value == "qbittorrent"

    def test_transmission_value(self) -> None:
        assert DownloadClientType.TRANSMISSION == "transmission"
        assert DownloadClientType.TRANSMISSION.value == "transmission"

    def test_deluge_value(self) -> None:
        assert DownloadClientType.DELUGE == "deluge"
        assert DownloadClientType.DELUGE.value == "deluge"

    def test_direct_value(self) -> None:
        assert DownloadClientType.DIRECT == "direct"
        assert DownloadClientType.DIRECT.value == "direct"

    def test_airdcpp_value(self) -> None:
        assert DownloadClientType.AIRDCPP == "airdcpp"
        assert DownloadClientType.AIRDCPP.value == "airdcpp"

    def test_all_seven_members_exist(self) -> None:
        members = list(DownloadClientType)
        assert len(members) == 7

    def test_construction_from_string(self) -> None:
        assert DownloadClientType("nzbget") is DownloadClientType.NZBGET
        assert DownloadClientType("transmission") is DownloadClientType.TRANSMISSION
        assert DownloadClientType("deluge") is DownloadClientType.DELUGE
        assert DownloadClientType("direct") is DownloadClientType.DIRECT
        assert DownloadClientType("airdcpp") is DownloadClientType.AIRDCPP

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError, match="rtorrent"):
            DownloadClientType("rtorrent")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError):
            DownloadClientType("")

    def test_case_sensitive_uppercase_raises(self) -> None:
        """StrEnum values are lowercase — uppercase must not match."""
        with pytest.raises(ValueError):
            DownloadClientType("NZBGET")

    def test_case_sensitive_mixed_case_raises(self) -> None:
        with pytest.raises(ValueError):
            DownloadClientType("NzbGet")

    def test_similar_but_wrong_value_raises(self) -> None:
        """Close misspellings must not silently match."""
        with pytest.raises(ValueError):
            DownloadClientType("nzb_get")

    def test_strenum_is_instance_of_str(self) -> None:
        """StrEnum members should be valid str instances."""
        for member in DownloadClientType:
            assert isinstance(member, str)

    def test_strenum_identity_via_equality(self) -> None:
        """String equality works both directions for StrEnum."""
        assert "deluge" == DownloadClientType.DELUGE  # noqa: SIM300
        assert "transmission" == DownloadClientType.TRANSMISSION  # noqa: SIM300

    def test_enum_in_string_formatting(self) -> None:
        """StrEnum values render cleanly in f-strings."""
        ct = DownloadClientType.NZBGET
        assert f"client={ct}" == "client=nzbget"

    def test_enum_member_ordering_matches_definition(self) -> None:
        """Members iterate in definition order (Usenet first, then torrent)."""
        members = list(DownloadClientType)
        assert members[0] is DownloadClientType.SABNZBD
        assert members[1] is DownloadClientType.NZBGET
        assert members[2] is DownloadClientType.QBITTORRENT
        assert members[3] is DownloadClientType.TRANSMISSION
        assert members[4] is DownloadClientType.DELUGE
        assert members[5] is DownloadClientType.DIRECT
        assert members[6] is DownloadClientType.AIRDCPP

    def test_membership_check_with_in(self) -> None:
        """The 'in' operator works with string values against the enum."""
        assert "nzbget" in [ct.value for ct in DownloadClientType]
        assert "transmission" in [ct.value for ct in DownloadClientType]
        assert "direct" in [ct.value for ct in DownloadClientType]
        assert "airdcpp" in [ct.value for ct in DownloadClientType]
        assert "rtorrent" not in [ct.value for ct in DownloadClientType]

    def test_hashable_for_set_and_dict_use(self) -> None:
        """Enum members are hashable and can be used as dict keys / in sets."""
        s = {DownloadClientType.NZBGET, DownloadClientType.DELUGE}
        assert len(s) == 2
        assert DownloadClientType.NZBGET in s
        d = {DownloadClientType.TRANSMISSION: "rpc"}
        assert d[DownloadClientType.TRANSMISSION] == "rpc"


class TestIsUsenet:
    """Tests for the is_usenet helper property."""

    def test_sabnzbd_is_usenet(self) -> None:
        assert DownloadClientType.SABNZBD.is_usenet is True

    def test_nzbget_is_usenet(self) -> None:
        assert DownloadClientType.NZBGET.is_usenet is True

    def test_qbittorrent_not_usenet(self) -> None:
        assert DownloadClientType.QBITTORRENT.is_usenet is False

    def test_transmission_not_usenet(self) -> None:
        assert DownloadClientType.TRANSMISSION.is_usenet is False

    def test_deluge_not_usenet(self) -> None:
        assert DownloadClientType.DELUGE.is_usenet is False

    def test_direct_not_usenet(self) -> None:
        assert DownloadClientType.DIRECT.is_usenet is False

    def test_airdcpp_not_usenet(self) -> None:
        assert DownloadClientType.AIRDCPP.is_usenet is False


class TestIsTorrent:
    """Tests for the is_torrent helper property."""

    def test_sabnzbd_not_torrent(self) -> None:
        assert DownloadClientType.SABNZBD.is_torrent is False

    def test_nzbget_not_torrent(self) -> None:
        assert DownloadClientType.NZBGET.is_torrent is False

    def test_qbittorrent_is_torrent(self) -> None:
        assert DownloadClientType.QBITTORRENT.is_torrent is True

    def test_transmission_is_torrent(self) -> None:
        assert DownloadClientType.TRANSMISSION.is_torrent is True

    def test_deluge_is_torrent(self) -> None:
        assert DownloadClientType.DELUGE.is_torrent is True

    def test_direct_not_torrent(self) -> None:
        assert DownloadClientType.DIRECT.is_torrent is False

    def test_airdcpp_not_torrent(self) -> None:
        assert DownloadClientType.AIRDCPP.is_torrent is False


class TestIsDirect:
    """Tests for the is_direct helper property."""

    def test_direct_is_direct(self) -> None:
        assert DownloadClientType.DIRECT.is_direct is True

    @pytest.mark.parametrize(
        "member",
        [member for member in DownloadClientType if member is not DownloadClientType.DIRECT],
    )
    def test_client_backed_types_are_not_direct(self, member: DownloadClientType) -> None:
        assert member.is_direct is False


class TestProtocolClassification:
    """Verify every client-history type has exactly one protocol classification."""

    def test_legacy_boolean_flags_match_protocol(self) -> None:
        for member in DownloadClientType:
            expected_count = 0 if member.acquisition_protocol is AcquisitionProtocol.DC else 1
            assert sum((member.is_usenet, member.is_torrent, member.is_direct)) == expected_count, (
                f"{member.name} compatibility flags must agree with its protocol"
            )

    def test_no_member_is_both(self) -> None:
        """No member should have both is_usenet and is_torrent True."""
        for member in DownloadClientType:
            assert not (member.is_usenet and member.is_torrent), (
                f"{member.name} cannot be both usenet and torrent"
            )

    def test_usenet_count(self) -> None:
        usenet = [m for m in DownloadClientType if m.is_usenet]
        assert len(usenet) == 2

    def test_torrent_count(self) -> None:
        torrent = [m for m in DownloadClientType if m.is_torrent]
        assert len(torrent) == 3

    def test_direct_count(self) -> None:
        direct = [m for m in DownloadClientType if m.is_direct]
        assert direct == [DownloadClientType.DIRECT]

    def test_dc_count(self) -> None:
        dc = [
            member
            for member in DownloadClientType
            if member.acquisition_protocol is AcquisitionProtocol.DC
        ]
        assert dc == [DownloadClientType.AIRDCPP]

    def test_property_returns_bool_not_truthy(self) -> None:
        """Properties must return actual bool, not just truthy/falsy values."""
        for member in DownloadClientType:
            assert type(member.is_usenet) is bool
            assert type(member.is_torrent) is bool
            assert type(member.is_direct) is bool


@pytest.mark.asyncio
class TestSQLAlchemyRoundTrip:
    """Tests that enum values survive a database insert + select cycle."""

    async def test_nzbget_round_trip(self, session: AsyncSession) -> None:
        record = _ClientTypeRecord(id=1, client_type=DownloadClientType.NZBGET.value)
        session.add(record)
        await session.flush()

        result = await session.execute(select(_ClientTypeRecord).where(_ClientTypeRecord.id == 1))
        loaded = result.scalar_one()
        assert loaded.client_type == "nzbget"
        assert DownloadClientType(loaded.client_type) is DownloadClientType.NZBGET

    async def test_transmission_round_trip(self, session: AsyncSession) -> None:
        record = _ClientTypeRecord(id=2, client_type=DownloadClientType.TRANSMISSION.value)
        session.add(record)
        await session.flush()

        result = await session.execute(select(_ClientTypeRecord).where(_ClientTypeRecord.id == 2))
        loaded = result.scalar_one()
        assert loaded.client_type == "transmission"
        assert DownloadClientType(loaded.client_type) is DownloadClientType.TRANSMISSION

    async def test_deluge_round_trip(self, session: AsyncSession) -> None:
        record = _ClientTypeRecord(id=3, client_type=DownloadClientType.DELUGE.value)
        session.add(record)
        await session.flush()

        result = await session.execute(select(_ClientTypeRecord).where(_ClientTypeRecord.id == 3))
        loaded = result.scalar_one()
        assert loaded.client_type == "deluge"
        assert DownloadClientType(loaded.client_type) is DownloadClientType.DELUGE

    async def test_existing_sabnzbd_still_valid(self, session: AsyncSession) -> None:
        """Existing SABNZBD records remain valid after enum expansion."""
        record = _ClientTypeRecord(id=4, client_type=DownloadClientType.SABNZBD.value)
        session.add(record)
        await session.flush()

        result = await session.execute(select(_ClientTypeRecord).where(_ClientTypeRecord.id == 4))
        loaded = result.scalar_one()
        assert DownloadClientType(loaded.client_type) is DownloadClientType.SABNZBD

    async def test_existing_qbittorrent_still_valid(self, session: AsyncSession) -> None:
        """Existing QBITTORRENT records remain valid after enum expansion."""
        record = _ClientTypeRecord(id=5, client_type=DownloadClientType.QBITTORRENT.value)
        session.add(record)
        await session.flush()

        result = await session.execute(select(_ClientTypeRecord).where(_ClientTypeRecord.id == 5))
        loaded = result.scalar_one()
        assert DownloadClientType(loaded.client_type) is DownloadClientType.QBITTORRENT

    async def test_airdcpp_round_trip(self, session: AsyncSession) -> None:
        record = _ClientTypeRecord(id=6, client_type=DownloadClientType.AIRDCPP.value)
        session.add(record)
        await session.flush()

        result = await session.execute(select(_ClientTypeRecord).where(_ClientTypeRecord.id == 6))
        loaded = result.scalar_one()
        assert DownloadClientType(loaded.client_type) is DownloadClientType.AIRDCPP

    async def test_all_seven_types_coexist_in_one_table(self, session: AsyncSession) -> None:
        """All 7 client types can be stored and queried in the same table."""
        for i, ct in enumerate(DownloadClientType, start=10):
            session.add(_ClientTypeRecord(id=i, client_type=ct.value))
        await session.flush()

        result = await session.execute(select(_ClientTypeRecord).where(_ClientTypeRecord.id >= 10))
        rows = list(result.scalars().all())
        stored_types = {r.client_type for r in rows}
        assert stored_types == {
            "sabnzbd",
            "nzbget",
            "qbittorrent",
            "transmission",
            "deluge",
            "direct",
            "airdcpp",
        }

    async def test_round_trip_preserves_helper_properties(self, session: AsyncSession) -> None:
        """After round-tripping through DB, reconstructed enum has correct properties."""
        record = _ClientTypeRecord(id=20, client_type=DownloadClientType.TRANSMISSION.value)
        session.add(record)
        await session.flush()

        result = await session.execute(select(_ClientTypeRecord).where(_ClientTypeRecord.id == 20))
        loaded = result.scalar_one()
        reconstructed = DownloadClientType(loaded.client_type)
        assert reconstructed.is_torrent is True
        assert reconstructed.is_usenet is False
