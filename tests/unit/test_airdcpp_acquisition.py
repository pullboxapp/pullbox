"""Restart-safe AirDC++ queue intent and idempotency contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models import Base
from pullbox.models.airdcpp import AirDcppAcquisition, AirDcppClientSettings
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus, IssueType
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.models.search_log import SearchLog, SearchType
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.providers.airdcpp.contracts import (
    AirDcppQueueBundleAddInfo,
    AirDcppQueueFile,
)
from pullbox.providers.airdcpp.errors import (
    AirDcppEntityNotFoundError,
    AirDcppUnavailableError,
)
from pullbox.providers.airdcpp.supervisor import AirDcppSupervisorState
from pullbox.providers.base import ReleaseResult
from pullbox.services.airdcpp_acquisition import AirDcppQueueAcquisitionService
from pullbox.services.airdcpp_search_types import (
    DcMetrics,
    DcRoute,
    DcSearchOutcome,
    DcValidatedCandidate,
)
from pullbox.services.intervention_service import InterventionService
from pullbox.services.release_validator import ReleaseValidator
from pullbox.services.search_acquisition_router import route_search_acquisition
from pullbox.services.search_targets import IssueSearchOutcome, IssueSearchTarget

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


@pytest.mark.parametrize("confidence", [MatchConfidence.HIGH, MatchConfidence.MEDIUM])
@pytest.mark.parametrize("queue_timeout", [False, True])
async def test_automatic_dc_routes_to_queue_or_restart_safe_intervention(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    confidence: MatchConfidence,
    queue_timeout: bool,
) -> None:
    client_id, issue_id = await _seed(db_factory)
    async with db_factory() as session:
        session.add(
            AirDcppClientSettings(
                client_config_id=client_id,
                search_enabled=True,
                automatic_search_enabled=True,
                queue_priority=3,
            )
        )
        log = SearchLog(
            issue_id=issue_id,
            series_title="Example Comic",
            issue_number=1,
            search_type=SearchType.AUTOMATED,
        )
        session.add(log)
        await session.commit()
        log_id = log.id
        candidate = _candidate(client_id)
        candidate = replace(
            candidate, validation=replace(candidate.validation, confidence=confidence)
        )
        outcome = IssueSearchOutcome(
            IssueSearchTarget(issue_id, 1, "Example Comic", 1, IssueType.ISSUE),
            "fast",
            0,
            [],
            [],
            [],
            [],
            None,
            None,
            {},
            1,
            dc_outcome=DcSearchOutcome((candidate,), (), (), 1, 1, 0, 1, False),
        )
        api = _FakeApi(session, failure=AirDcppUnavailableError() if queue_timeout else None)
        supervisor = SimpleNamespace(state=AirDcppSupervisorState.READY, api_client=api)
        monkeypatch.setattr(
            "pullbox.composition.airdcpp.get_airdcpp_supervisor_registry",
            lambda: SimpleNamespace(
                get=lambda identity: supervisor if identity == client_id else None
            ),
        )
        routed = await route_search_acquisition(
            session,
            outcome=outcome,
            search_log_id=log_id,
            eval_kwargs={},
            type_thresholds={"issue": "high"},
            download_service=AsyncMock(),
            intervention_service=InterventionService(),
            runner=None,
        )
        await session.commit()
        if confidence is MatchConfidence.HIGH:
            assert routed.grabbed == 1
            assert routed.action_status == ("retry_pending" if queue_timeout else "downloading")
            assert routed.download_id is not None
            assert api.mutations == 1
            replay = await route_search_acquisition(
                session,
                outcome=outcome,
                search_log_id=log_id,
                eval_kwargs={},
                type_thresholds={"issue": "high"},
                download_service=AsyncMock(),
                intervention_service=InterventionService(),
                runner=None,
            )
            assert replay.action_status == "already_downloading"
            assert replay.grabbed == 0
            assert api.mutations == 1
        else:
            assert routed.queued == 1
            assert api.mutations == 0
            pending = (await session.scalars(select(PendingMatch))).one()
            assert pending.match_details["source_kind"] == "dc"
            assert candidate.route.tth not in str(pending.match_details)
            assert await session.scalar(select(DownloadHistory.id)) is None
            pending_id = pending.id
            await session.commit()
            # A fresh session/service must not depend on an in-memory route grant.
            async with db_factory() as restarted:
                api.session = restarted
                downloaded = await InterventionService().approve_match(restarted, pending_id)
                assert downloaded.download_client is DownloadClientType.AIRDCPP
                assert api.mutations == 1
                restored_pending = await restarted.get(PendingMatch, pending_id)
                assert restored_pending.status == PendingMatchStatus.APPROVED
                await restarted.commit()
        assert len((await session.scalars(select(DownloadHistory))).all()) == 1


async def test_automatic_dc_rechecks_client_opt_in_before_mutation(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_id, issue_id = await _seed(db_factory)
    async with db_factory() as session:
        session.add(
            AirDcppClientSettings(client_config_id=client_id, automatic_search_enabled=False)
        )
        await session.commit()
        candidate = _candidate(client_id)
        outcome = IssueSearchOutcome(
            IssueSearchTarget(issue_id, 1, "Example Comic", 1, IssueType.ISSUE),
            "fast",
            0,
            [],
            [],
            [],
            [],
            None,
            None,
            {},
            1,
            dc_outcome=DcSearchOutcome((candidate,), (), (), 1, 1, 0, 1, False),
        )
        routed = await route_search_acquisition(
            session,
            outcome=outcome,
            search_log_id=1,
            eval_kwargs={},
            type_thresholds={"issue": "high"},
            download_service=AsyncMock(),
            intervention_service=InterventionService(),
            runner=None,
        )
        assert routed.action_status == "source_unavailable"
        assert routed.notices
        assert await session.scalar(select(DownloadHistory.id)) is None


@pytest.mark.parametrize("owned", [False, True])
async def test_automatic_dc_never_queues_a_blocklisted_or_owned_candidate(
    db_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    owned: bool,
) -> None:
    from pullbox.models.blocklist import BlocklistReason
    from pullbox.services.blocklist_service import BlocklistService

    client_id, issue_id = await _seed(db_factory)
    candidate = _candidate(client_id)
    async with db_factory() as session:
        session.add(
            AirDcppClientSettings(client_config_id=client_id, automatic_search_enabled=True)
        )
        if owned:
            root = LibraryRoot(name="Test library", path="/test-library")
            session.add(root)
            await session.flush()
            session.add(
                LibraryFile(
                    issue_id=issue_id,
                    library_root_id=root.id,
                    file_path="/test-library/owned.cbz",
                    file_name="owned.cbz",
                    file_size=10,
                    file_format=FileFormat.CBZ,
                    file_modified_at=datetime.now(UTC),
                )
            )
        else:
            await BlocklistService.add_entry(
                session, candidate.release.title, BlocklistReason.REJECTED
            )
        await session.commit()
        api = _FakeApi(session)
        supervisor = SimpleNamespace(state=AirDcppSupervisorState.READY, api_client=api)
        monkeypatch.setattr(
            "pullbox.composition.airdcpp.get_airdcpp_supervisor_registry",
            lambda: SimpleNamespace(get=lambda _: supervisor),
        )
        outcome = IssueSearchOutcome(
            IssueSearchTarget(issue_id, 1, "Example Comic", 1, IssueType.ISSUE),
            "fast",
            0,
            [],
            [],
            [],
            [],
            None,
            None,
            {},
            1,
            dc_outcome=DcSearchOutcome((candidate,), (), (), 1, 1, 0, 1, False),
        )
        routed = await route_search_acquisition(
            session,
            outcome=outcome,
            search_log_id=1,
            eval_kwargs={},
            type_thresholds={"issue": "high"},
            download_service=AsyncMock(),
            intervention_service=InterventionService(),
            runner=None,
        )
        assert routed.grabbed == 0
        if owned:
            assert routed.action_status == "already_owned"
        assert api.mutations == 0
        assert await session.scalar(select(DownloadHistory.id)) is None


@pytest.mark.parametrize("mutation", ["plaintext", "another_issue", "size"])
def test_dc_review_route_rejects_modified_ownership(mutation: str) -> None:
    from pullbox.services.airdcpp_search_acquisition import dc_review_candidate, dc_review_snapshot

    candidate = _candidate(1)
    snapshot = dc_review_snapshot(candidate, issue_id=1, search_log_id=2)
    pending = PendingMatch(
        issue_id=2 if mutation == "another_issue" else 1,
        release_title=candidate.release.title,
        file_size=1 if mutation == "size" else candidate.route.size_bytes,
        match_details={"dc_route_snapshot": "{}" if mutation == "plaintext" else snapshot},
    )
    with pytest.raises(ValueError):
        dc_review_candidate(pending)


class _FakeApi:
    def __init__(
        self,
        session: AsyncSession,
        *,
        failure: Exception | None = None,
        file_bundle_failure: Exception | None = None,
        adopted: list[AirDcppQueueFile] | None = None,
        merged: bool = False,
    ) -> None:
        self.session = session
        self.failure = failure
        self.file_bundle_failure = file_bundle_failure
        self.adopted = adopted or []
        self.merged = merged
        self.mutations = 0
        self.lookups = 0
        self.file_bundle_mutations = 0
        self.removed_bundles: list[int] = []
        self.observed_mutation_state: str | None = None
        self.observed_next_retry_at: datetime | None = None

    async def download_search_result(self, *_args: object, **_kwargs: object):
        assert self.session.in_transaction() is False
        pending = (
            await self.session.execute(
                select(AirDcppAcquisition).where(AirDcppAcquisition.bundle_id.is_(None))
            )
        ).scalar_one()
        self.observed_mutation_state = pending.client_state
        self.observed_next_retry_at = pending.next_retry_at
        await self.session.commit()
        self.mutations += 1
        if self.failure is not None:
            raise self.failure
        return AirDcppQueueBundleAddInfo(id=91, merged=self.merged)

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

    async def remove_queue_bundle(self, bundle_id: int) -> None:
        assert self.session.in_transaction() is False
        self.removed_bundles.append(bundle_id)


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
        assert api.observed_mutation_state == "mutation_pending"
        assert api.observed_next_retry_at is not None
        assert api.observed_next_retry_at > datetime.now(UTC)
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


@pytest.mark.parametrize(
    ("merged", "removed_bundles"),
    [(False, [91]), (True, [])],
)
@pytest.mark.asyncio
async def test_initial_mutation_does_not_resurrect_a_cancelled_download(
    db_factory: async_sessionmaker[AsyncSession],
    merged: bool,
    removed_bundles: list[int],
) -> None:
    client_id, issue_id = await _seed(db_factory)

    class _CancelDuringMutationApi(_FakeApi):
        async def download_search_result(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> AirDcppQueueBundleAddInfo:
            added = await super().download_search_result(*_args, **_kwargs)
            async with db_factory() as cancel_session:
                acquisition = (
                    await cancel_session.execute(select(AirDcppAcquisition))
                ).scalar_one()
                history = await cancel_session.get(
                    DownloadHistory,
                    acquisition.download_history_id,
                )
                assert history is not None
                acquisition.client_state = "cancelled"
                acquisition.next_retry_at = None
                history.state = DownloadState.FAILED
                history.error_message = "Cancelled by user"
                await cancel_session.commit()
            return added

    async with db_factory() as session:
        api = _CancelDuringMutationApi(session, merged=merged)

        result = await AirDcppQueueAcquisitionService().acquire(
            session,
            candidate=_candidate(client_id),
            issue_id=issue_id,
            request_key="manual-intent-cancelled",
            search_log_id=None,
            api_client=api,
            queue_priority=3,
            replace_existing_file=False,
        )

    assert result.bundle_id is None
    assert result.state is DownloadState.FAILED
    assert api.removed_bundles == removed_bundles
    async with db_factory() as session:
        acquisition = (await session.execute(select(AirDcppAcquisition))).scalar_one()
        history = await session.get(DownloadHistory, acquisition.download_history_id)
        assert history is not None
        assert acquisition.bundle_id is None
        assert acquisition.client_state == "cancelled"
        assert history.state is DownloadState.FAILED
        assert history.error_message == "Cancelled by user"


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
