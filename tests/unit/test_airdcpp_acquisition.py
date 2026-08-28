"""Restart-safe AirDC++ queue intent and idempotency contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models import Base
from pullbox.models.airdcpp import AirDcppAcquisition
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.providers.airdcpp.contracts import (
    AirDcppQueueBundleAddInfo,
    AirDcppQueueFile,
)
from pullbox.providers.airdcpp.errors import (
    AirDcppEntityNotFoundError,
    AirDcppUnavailableError,
)
from pullbox.providers.base import ReleaseResult
from pullbox.services.airdcpp_acquisition import AirDcppQueueAcquisitionService
from pullbox.services.airdcpp_search_types import DcMetrics, DcRoute, DcValidatedCandidate
from pullbox.services.release_validator import ReleaseValidator

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

_TTH = "CUO74LMZUQMQCBR5UKTIFJPO32LVUH5VZBOL54Y"


@pytest.fixture
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with factory() as session:
        client = DownloadClientConfig(
            name="Dedicated Air",
            client_type=DownloadClientType.AIRDCPP,
            url="http://air.example.test:5600",
            enabled=True,
            priority=20,
        )
        series = Series(
            comicvine_id=9200,
            title="Example Comic",
            sort_title="Example Comic",
            year_start=2026,
            status=SeriesStatus.CONTINUING,
            series_type=SeriesType.STANDARD,
            monitored=True,
            issue_count=1,
        )
        session.add_all([client, series])
        await session.flush()
        issue = Issue(
            series_id=series.id,
            comicvine_id=9201,
            issue_number=1,
            title="One",
            status=IssueStatus.WANTED,
        )
        session.add(issue)
        await session.commit()
        return client.id, issue.id


def _candidate(client_id: int) -> DcValidatedCandidate:
    release = ReleaseResult(
        title="Example Comic 001 (2026).cbz",
        indexer_name="Dedicated Air",
        download_url=f"airdcpp://client/{client_id}/tth/{_TTH}",
        size_bytes=100_000_000,
        age_days=None,
        seeders=None,
        leechers=None,
        grabs=None,
        is_torrent=False,
        category=None,
        published_at=None,
        protocol=AcquisitionProtocol.DC,
    )
    validation = ReleaseValidator().validate_all_results(
        [release],
        wanted_series="Example Comic",
        wanted_issue=1,
        wanted_year=2026,
        wanted_issue_type=IssueType.ISSUE,
    )[0][0]
    return DcValidatedCandidate(
        release=release,
        validation=validation,
        route=DcRoute(
            client_config_id=client_id,
            client_identity=f"airdcpp:{client_id}",
            search_instance_id=44,
            grouped_result_id="opaque-result",
            result_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            tth=_TTH,
            size_bytes=100_000_000,
        ),
        metrics=DcMetrics(2, 1, 2, 1_000_000),
    )


class _FakeApi:
    def __init__(
        self,
        session: AsyncSession,
        *,
        failure: Exception | None = None,
        file_bundle_failure: Exception | None = None,
        adopted: list[AirDcppQueueFile] | None = None,
    ) -> None:
        self.session = session
        self.failure = failure
        self.file_bundle_failure = file_bundle_failure
        self.adopted = adopted or []
        self.mutations = 0
        self.lookups = 0
        self.file_bundle_mutations = 0

    async def download_search_result(self, *_args: object, **_kwargs: object):
        assert self.session.in_transaction() is False
        self.mutations += 1
        if self.failure is not None:
            raise self.failure
        return AirDcppQueueBundleAddInfo(id=91, merged=False)

    async def get_queue_files_by_tth(self, _tth: str) -> list[AirDcppQueueFile]:
        assert self.session.in_transaction() is False
        self.lookups += 1
        return self.adopted

    async def create_file_bundle(self, **_kwargs: object) -> AirDcppQueueBundleAddInfo:
        assert self.session.in_transaction() is False
        self.file_bundle_mutations += 1
        if self.file_bundle_failure is not None:
            raise self.file_bundle_failure
        return AirDcppQueueBundleAddInfo(id=92, merged=False)


def _queue_file(*, size: int = 100_000_000, target: str | None = None) -> AirDcppQueueFile:
    return AirDcppQueueFile.model_validate(
        {
            "id": 51,
            "name": "Example Comic 001 (2026).cbz",
            "target": target or "/Downloads/Example Comic 001 (2026).cbz",
            "type": {"id": "file"},
            "bundle": 91,
            "size": size,
            "downloaded_bytes": 0,
            "priority": {"id": 3, "str": "Normal", "auto": False},
            "time_added": 1,
            "time_finished": 0,
            "speed": 0,
            "seconds_left": 0,
            "sources": {"online": 1, "total": 2, "str": "1/2"},
            "status": {
                "id": "queued",
                "failed": False,
                "downloaded": False,
                "completed": False,
                "str": "Queued",
            },
            "tth": _TTH,
        }
    )


@pytest.mark.asyncio
async def test_acquisition_persists_before_mutation_and_replays_idempotently(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, issue_id = await _seed(db_factory)
    async with db_factory() as session:
        api = _FakeApi(session)
        service = AirDcppQueueAcquisitionService()

        first = await service.acquire(
            session,
            candidate=_candidate(client_id),
            issue_id=issue_id,
            request_key="manual-intent-1",
            search_log_id=None,
            api_client=api,
            queue_priority=3,
            replace_existing_file=False,
        )
        second = await service.acquire(
            session,
            candidate=_candidate(client_id),
            issue_id=issue_id,
            request_key="manual-intent-1",
            search_log_id=None,
            api_client=api,
            queue_priority=3,
            replace_existing_file=False,
        )

        assert api.mutations == 1
        assert second.acquisition_id == first.acquisition_id
        acquisition = await session.get(AirDcppAcquisition, first.acquisition_id)
        history = await session.get(DownloadHistory, first.download_history_id)
        issue = await session.get(Issue, issue_id)
        assert acquisition is not None and history is not None and issue is not None
        assert acquisition.bundle_id == 91
        assert history.external_id == f"airdcpp:{client_id}:bundle:91"
        assert history.download_client_config_id == client_id
        assert history.protocol is AcquisitionProtocol.DC
        assert history.state is DownloadState.SENT
        assert issue.status is IssueStatus.DOWNLOADING


@pytest.mark.asyncio
async def test_ambiguous_mutation_adopts_only_exact_tth_size_and_target(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, issue_id = await _seed(db_factory)
    async with db_factory() as session:
        api = _FakeApi(
            session,
            failure=AirDcppUnavailableError(),
            adopted=[
                _queue_file(size=99_000_000),
                _queue_file(target="/Downloads/Example Comic 001 (2026).cbz"),
            ],
        )

        result = await AirDcppQueueAcquisitionService().acquire(
            session,
            candidate=_candidate(client_id),
            issue_id=issue_id,
            request_key="manual-intent-ambiguous",
            search_log_id=None,
            api_client=api,
            queue_priority=None,
            replace_existing_file=False,
        )

        assert api.mutations == 1
        assert api.lookups == 1
        assert result.bundle_id == 91
        acquisition = (
            await session.execute(
                select(AirDcppAcquisition).where(
                    AirDcppAcquisition.request_key == "manual-intent-ambiguous"
                )
            )
        ).scalar_one()
        assert acquisition.reconciliation_error is None
        assert acquisition.bundle_id == 91


@pytest.mark.asyncio
async def test_expired_live_route_creates_exact_tth_bundle_once_for_source_recovery(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, issue_id = await _seed(db_factory)
    async with db_factory() as session:
        api = _FakeApi(session, failure=AirDcppEntityNotFoundError())

        result = await AirDcppQueueAcquisitionService().acquire(
            session,
            candidate=_candidate(client_id),
            issue_id=issue_id,
            request_key="manual-intent-expired",
            search_log_id=None,
            api_client=api,
            queue_priority=3,
            replace_existing_file=False,
        )

        assert result.bundle_id == 92
        assert api.mutations == api.file_bundle_mutations == 1
        acquisition = await session.get(AirDcppAcquisition, result.acquisition_id)
        assert acquisition is not None
        assert acquisition.client_state == "source_search_pending"
        assert acquisition.next_retry_at is not None


@pytest.mark.asyncio
async def test_expired_route_fallback_failure_remains_restart_reconcilable(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, issue_id = await _seed(db_factory)
    async with db_factory() as session:
        api = _FakeApi(
            session,
            failure=AirDcppEntityNotFoundError(),
            file_bundle_failure=AirDcppUnavailableError(),
        )

        result = await AirDcppQueueAcquisitionService().acquire(
            session,
            candidate=_candidate(client_id),
            issue_id=issue_id,
            request_key="manual-intent-fallback-unavailable",
            search_log_id=None,
            api_client=api,
            queue_priority=3,
            replace_existing_file=False,
        )

        assert result.bundle_id is None
        assert result.state is DownloadState.RETRY_PENDING
        acquisition = await session.get(AirDcppAcquisition, result.acquisition_id)
        assert acquisition is not None
        assert acquisition.client_state == "reconcile_pending"
        assert acquisition.reconciliation_error == "unavailable"
