"""Batched, restart-safe AirDC++ queue reconciliation contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import joinedload

from pullbox.core.acquisition import AcquisitionProtocol
from pullbox.models import Base
from pullbox.models.airdcpp import AirDcppAcquisition
from pullbox.models.client import DownloadClientConfig
from pullbox.models.download import DownloadClientType, DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus
from pullbox.models.operation_progress import OperationProgress, OperationProgressType
from pullbox.models.series import Series, SeriesStatus, SeriesType
from pullbox.providers.airdcpp.contracts import (
    AirDcppQueueBundle,
    AirDcppQueueBundleAddInfo,
    AirDcppQueueFile,
)
from pullbox.providers.airdcpp.errors import AirDcppUnavailableError
from pullbox.services.airdcpp_reconciliation import AirDcppReconciler, apply_airdcpp_bundle
from pullbox.services.airdcpp_search_cooldown import AirDcppCooldownReservation

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


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    state: DownloadState = DownloadState.SENT,
    imported: bool = False,
    bundle_id: int | None = 91,
) -> tuple[int, int, int]:
    async with factory() as session:
        client = DownloadClientConfig(
            name="Dedicated Air",
            client_type=DownloadClientType.AIRDCPP,
            url="http://air.example.test:5600",
            enabled=True,
            priority=20,
        )
        series = Series(
            comicvine_id=9300,
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
            comicvine_id=9301,
            issue_number=1,
            title="One",
            status=IssueStatus.DOWNLOADING,
        )
        session.add(issue)
        await session.flush()
        now = datetime.now(UTC)
        history = DownloadHistory(
            issue_id=issue.id,
            download_client_config_id=client.id,
            title="Example Comic 001 (2026).cbz",
            download_url="airdcpp://intent/reconcile",
            download_client=DownloadClientType.AIRDCPP,
            protocol=AcquisitionProtocol.DC,
            external_id=(f"airdcpp:{client.id}:bundle:{bundle_id}" if bundle_id else None),
            state=state,
            file_size=100_000_000,
            sent_at=now,
            imported_at=now if imported else None,
        )
        session.add(history)
        await session.flush()
        acquisition = AirDcppAcquisition(
            download_history_id=history.id,
            request_key=f"reconcile-{history.id}",
            client_config_id=client.id,
            client_identity=f"airdcpp:{client.id}",
            tth=_TTH,
            size_bytes=100_000_000,
            original_name=history.title,
            bundle_id=bundle_id,
            client_state="queued" if bundle_id else "reconcile_pending",
        )
        session.add(acquisition)
        await session.commit()
        return client.id, history.id, acquisition.id


def _bundle(
    *,
    bundle_id: int = 91,
    status_id: str = "queued",
    downloaded_bytes: int = 0,
    downloaded: bool = False,
    completed: bool = False,
    failed: bool = False,
) -> AirDcppQueueBundle:
    return AirDcppQueueBundle.model_validate(
        {
            "id": bundle_id,
            "name": "Example Comic 001 (2026).cbz",
            "target": "/Downloads/Example Comic 001 (2026).cbz",
            "type": {"id": "file"},
            "size": 100_000_000,
            "downloaded_bytes": downloaded_bytes,
            "priority": {"id": 3, "str": "Normal", "auto": False},
            "time_added": 1,
            "time_finished": 2 if downloaded else 0,
            "speed": 1_000_000,
            "seconds_left": 75,
            "sources": {"online": 1, "total": 2, "str": "1/2"},
            "status": {
                "id": status_id,
                "failed": failed,
                "downloaded": downloaded,
                "completed": completed,
                "str": "Localized diagnostic text is ignored",
            },
        }
    )


class _FakeApi:
    def __init__(
        self,
        pages: list[list[AirDcppQueueBundle]],
        *,
        files: list[AirDcppQueueFile] | None = None,
        lookup_failure: Exception | None = None,
        retry_result: AirDcppQueueBundleAddInfo | None = None,
        retry_failure: Exception | None = None,
        create_result: AirDcppQueueBundleAddInfo | None = None,
    ) -> None:
        self.pages = pages
        self.files = files or []
        self.lookup_failure = lookup_failure
        self.retry_result = retry_result or AirDcppQueueBundleAddInfo(id=92, merged=False)
        self.retry_failure = retry_failure
        self.create_result = create_result or AirDcppQueueBundleAddInfo(id=93, merged=False)
        self.calls: list[tuple[int, int]] = []
        self.tth_calls: list[str] = []
        self.alternate_calls: list[int] = []
        self.retry_calls: list[tuple[int, str, str, int | None]] = []
        self.create_calls: list[tuple[str, int, str, int | None]] = []
        self.removed_bundles: list[int] = []

    async def get_queue_bundles(self, *, start: int, count: int):
        self.calls.append((start, count))
        index = start // 100
        return self.pages[index] if index < len(self.pages) else []

    async def get_queue_files_by_tth(self, tth: str):
        self.tth_calls.append(tth)
        if self.lookup_failure is not None:
            raise self.lookup_failure
        return self.files

    async def search_queue_bundle(self, bundle_id: int) -> None:
        self.alternate_calls.append(bundle_id)

    async def download_search_result(
        self,
        instance_id: int,
        result_id: str,
        *,
        target_name: str,
        priority: int | None,
    ) -> AirDcppQueueBundleAddInfo:
        self.retry_calls.append((instance_id, result_id, target_name, priority))
        if self.retry_failure is not None:
            raise self.retry_failure
        return self.retry_result

    async def create_file_bundle(
        self,
        *,
        tth: str,
        size: int,
        target_name: str,
        priority: int | None,
    ) -> AirDcppQueueBundleAddInfo:
        self.create_calls.append((tth, size, target_name, priority))
        return self.create_result

    async def remove_queue_bundle(self, bundle_id: int) -> None:
        self.removed_bundles.append(bundle_id)


class _FakeCooldown:
    def __init__(self, *, granted: bool) -> None:
        self.granted = granted
        self.calls = 0

    async def reserve(self, config_id: int) -> AirDcppCooldownReservation:
        self.calls += 1
        now = datetime.now(UTC)
        return AirDcppCooldownReservation(
            config_id=config_id,
            granted=self.granted,
            not_before=now,
            next_allowed_at=now,
            wait_seconds=0 if self.granted else 45,
        )


@pytest.mark.parametrize(
    ("bundle", "expected"),
    [
        (_bundle(status_id="queued"), DownloadState.SENT),
        (_bundle(downloaded_bytes=25_000_000), DownloadState.DOWNLOADING),
        (
            _bundle(status_id="downloaded", downloaded_bytes=100_000_000, downloaded=True),
            DownloadState.FINALIZING,
        ),
        (
            _bundle(
                status_id="completion_validation_running",
                downloaded_bytes=100_000_000,
                downloaded=True,
            ),
            DownloadState.FINALIZING,
        ),
        (
            _bundle(
                status_id="completed",
                downloaded_bytes=100_000_000,
                downloaded=True,
                completed=True,
            ),
            DownloadState.COMPLETED,
        ),
        (_bundle(status_id="download_error", failed=True), DownloadState.FAILED),
    ],
)
@pytest.mark.asyncio
async def test_reconciliation_maps_stable_status_fields_without_localized_text(
    db_factory: async_sessionmaker[AsyncSession],
    bundle: AirDcppQueueBundle,
    expected: DownloadState,
) -> None:
    client_id, history_id, acquisition_id = await _seed(db_factory)

    result = await AirDcppReconciler(db_factory).reconcile_client(
        client_id,
        _FakeApi([[bundle]]),
    )

    async with db_factory() as session:
        history = await session.get(DownloadHistory, history_id)
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        operation = (
            await session.execute(
                select(OperationProgress).where(
                    OperationProgress.operation_type == OperationProgressType.DOWNLOAD,
                    OperationProgress.operation_key == str(history_id),
                )
            )
        ).scalar_one()
        assert history is not None and acquisition is not None
        assert history.state is expected
        assert history.downloaded_path == (
            "/Downloads/Example Comic 001 (2026).cbz"
            if expected is DownloadState.COMPLETED
            else None
        )
        assert acquisition.client_state == bundle.status.id
        assert acquisition.remote_target == "/Downloads/Example Comic 001 (2026).cbz"
        assert acquisition.last_reconciled_at is not None
        assert operation.source_label == "AirDC++"
        assert operation.overall_total == 100_000_000
        assert operation.overall_current == bundle.downloaded_bytes
        assert result.processed == 1
        assert result.completed == int(expected is DownloadState.COMPLETED)


@pytest.mark.asyncio
async def test_reconciliation_is_bounded_and_terminal_import_never_regresses(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, history_id, _ = await _seed(
        db_factory,
        state=DownloadState.IMPORTED,
        imported=True,
    )
    api = _FakeApi([[_bundle()]])
    reconciler = AirDcppReconciler(db_factory)

    first = await reconciler.reconcile_client(client_id, api)
    second = await reconciler.reconcile_client(client_id, api)

    async with db_factory() as session:
        history = await session.get(DownloadHistory, history_id)
        assert history is not None
        assert history.state is DownloadState.IMPORTED
        assert history.imported_at is not None
    assert first.processed == second.processed == 0
    assert api.calls == []


@pytest.mark.asyncio
async def test_completed_unimported_download_is_not_repolled_or_regressed(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, history_id, _ = await _seed(
        db_factory,
        state=DownloadState.COMPLETED,
    )
    api = _FakeApi([[_bundle(status_id="queued")]])

    result = await AirDcppReconciler(db_factory).reconcile_client(client_id, api)

    async with db_factory() as session:
        history = await session.get(DownloadHistory, history_id)
        assert history is not None
        assert history.state is DownloadState.COMPLETED
        assert history.imported_at is None
    assert result.processed == 0
    assert api.calls == []


@pytest.mark.asyncio
async def test_reconciliation_rotates_past_one_hundred_active_rows_in_bounded_cycles(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, _history_id, _ = await _seed(db_factory)
    newest_history_id = 0
    async with db_factory() as session:
        first = (
            await session.execute(
                select(DownloadHistory).where(
                    DownloadHistory.download_client_config_id == client_id
                )
            )
        ).scalar_one()
        for offset in range(1, 101):
            bundle_id = 91 + offset
            history = DownloadHistory(
                issue_id=first.issue_id,
                download_client_config_id=client_id,
                title=f"Example Comic {offset + 1:03d} (2026).cbz",
                download_url=f"airdcpp://intent/bounded-{offset}",
                download_client=DownloadClientType.AIRDCPP,
                protocol=AcquisitionProtocol.DC,
                external_id=f"airdcpp:{client_id}:bundle:{bundle_id}",
                state=DownloadState.SENT,
                file_size=100_000_000,
                sent_at=datetime.now(UTC),
            )
            session.add(history)
            await session.flush()
            if offset == 100:
                newest_history_id = history.id
            session.add(
                AirDcppAcquisition(
                    download_history_id=history.id,
                    request_key=f"bounded-{offset}",
                    client_config_id=client_id,
                    client_identity=f"airdcpp:{client_id}",
                    tth=_TTH,
                    size_bytes=100_000_000,
                    original_name=history.title,
                    bundle_id=bundle_id,
                    client_state="queued",
                )
            )
        await session.commit()
    api = _FakeApi(
        [
            [_bundle(bundle_id=bundle_id) for bundle_id in range(91, 191)],
            [_bundle(bundle_id=191, status_id="completed", downloaded=True, completed=True)],
        ]
    )
    reconciler = AirDcppReconciler(db_factory)

    first = await reconciler.reconcile_client(client_id, api)
    async with db_factory() as session:
        newest = await session.get(DownloadHistory, newest_history_id)
        assert newest is not None
        assert newest.state is DownloadState.SENT

    second = await reconciler.reconcile_client(client_id, api)
    async with db_factory() as session:
        newest = await session.get(DownloadHistory, newest_history_id)
        assert newest is not None
        assert newest.state is DownloadState.COMPLETED

    assert first.processed == second.processed == 100
    assert first.pages == second.pages == 2
    assert first.partial is second.partial is False
    assert api.calls == [(0, 100), (100, 100), (0, 100), (100, 100)]


@pytest.mark.asyncio
async def test_unchanged_progress_persistence_is_throttled_to_five_seconds(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    _client_id, _history_id, acquisition_id = await _seed(db_factory)
    started_at = datetime.now(UTC)
    async with db_factory() as session:
        acquisition = (
            await session.execute(
                select(AirDcppAcquisition)
                .options(joinedload(AirDcppAcquisition.download_history))
                .where(AirDcppAcquisition.id == acquisition_id)
            )
        ).scalar_one()
        first = _bundle(downloaded_bytes=10_000_000)
        assert apply_airdcpp_bundle(acquisition, first, at=started_at) is True
        assert acquisition.route_snapshot["queue"]["downloaded_bytes"] == 10_000_000

        too_soon = _bundle(downloaded_bytes=20_000_000)
        assert (
            apply_airdcpp_bundle(
                acquisition,
                too_soon,
                at=started_at + timedelta(seconds=4),
            )
            is False
        )
        assert acquisition.route_snapshot["queue"]["downloaded_bytes"] == 10_000_000
        assert acquisition.last_reconciled_at == started_at

        due = _bundle(downloaded_bytes=30_000_000)
        assert (
            apply_airdcpp_bundle(
                acquisition,
                due,
                at=started_at + timedelta(seconds=5),
            )
            is True
        )
        assert acquisition.route_snapshot["queue"]["downloaded_bytes"] == 30_000_000
        assert acquisition.last_reconciled_at == started_at + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_source_less_fallback_search_uses_same_durable_client_cooldown(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, _history_id, acquisition_id = await _seed(db_factory)
    async with db_factory() as session:
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert acquisition is not None
        acquisition.client_state = "source_search_pending"
        acquisition.next_retry_at = datetime.now(UTC)
        await session.commit()
    cooldown = _FakeCooldown(granted=True)
    api = _FakeApi([[_bundle()]])

    await AirDcppReconciler(db_factory, cooldown=cooldown).reconcile_client(
        client_id,
        api,
    )

    assert cooldown.calls == 1
    assert api.alternate_calls == [91]
    async with db_factory() as session:
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert acquisition is not None
        assert acquisition.next_retry_at is None
        assert acquisition.last_event_at is not None


@pytest.mark.asyncio
async def test_restart_reconciliation_adopts_one_exact_pre_id_queue_mutation(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, history_id, acquisition_id = await _seed(db_factory, bundle_id=None)
    queue_file = AirDcppQueueFile.model_validate(
        {
            "id": 401,
            "name": "Example Comic 001 (2026).cbz",
            "target": "/Downloads/Example Comic 001 (2026).cbz",
            "type": {"id": "file"},
            "bundle": 92,
            "size": 100_000_000,
            "downloaded_bytes": 0,
            "priority": {"id": 3, "str": "Normal", "auto": False},
            "time_added": 1,
            "time_finished": 0,
            "speed": 0,
            "seconds_left": 0,
            "sources": {"online": 1, "total": 1, "str": "1/1"},
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
    api = _FakeApi([[]], files=[queue_file])

    result = await AirDcppReconciler(db_factory).reconcile_client(client_id, api)

    assert api.tth_calls == [_TTH]
    assert result.changed == 1
    async with db_factory() as session:
        history = await session.get(DownloadHistory, history_id)
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert history is not None and acquisition is not None
        assert acquisition.bundle_id == 92
        assert acquisition.remote_target == "/Downloads/Example Comic 001 (2026).cbz"
        assert acquisition.client_state == "queued"
        assert history.external_id == f"airdcpp:{client_id}:bundle:92"
        assert history.state is DownloadState.SENT


@pytest.mark.asyncio
async def test_pre_id_reconciliation_retries_a_due_live_route_mutation(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, history_id, acquisition_id = await _seed(
        db_factory,
        state=DownloadState.RETRY_PENDING,
        bundle_id=None,
    )
    async with db_factory() as session:
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert acquisition is not None
        acquisition.search_instance_id = 44
        acquisition.grouped_result_id = "opaque-result"
        acquisition.result_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        acquisition.retry_count = 1
        acquisition.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    api = _FakeApi([[]], retry_result=AirDcppQueueBundleAddInfo(id=92, merged=False))

    result = await AirDcppReconciler(db_factory).reconcile_client(client_id, api)

    assert api.retry_calls == [(44, "opaque-result", "Example Comic 001 (2026).cbz", None)]
    assert api.create_calls == []
    assert result.changed == 1
    async with db_factory() as session:
        history = await session.get(DownloadHistory, history_id)
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert history is not None and acquisition is not None
        assert acquisition.bundle_id == 92
        assert acquisition.client_state == "queued"
        assert acquisition.reconciliation_error is None
        assert history.external_id == f"airdcpp:{client_id}:bundle:92"
        assert history.state is DownloadState.SENT


@pytest.mark.asyncio
async def test_pre_id_reconciliation_does_not_race_an_initial_mutation(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, history_id, acquisition_id = await _seed(
        db_factory,
        state=DownloadState.QUEUED,
        bundle_id=None,
    )
    async with db_factory() as session:
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert acquisition is not None
        acquisition.client_state = "mutation_pending"
        acquisition.search_instance_id = 44
        acquisition.grouped_result_id = "opaque-result"
        acquisition.result_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        acquisition.next_retry_at = None
        await session.commit()
    api = _FakeApi([[]])

    result = await AirDcppReconciler(db_factory).reconcile_client(client_id, api)

    assert api.tth_calls == [_TTH]
    assert api.retry_calls == []
    assert api.create_calls == []
    assert result.changed == 0
    async with db_factory() as session:
        history = await session.get(DownloadHistory, history_id)
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert history is not None and acquisition is not None
        assert acquisition.bundle_id is None
        assert acquisition.client_state == "mutation_pending"
        assert acquisition.retry_count == 0
        assert history.state is DownloadState.QUEUED


@pytest.mark.asyncio
async def test_manual_retry_claim_is_not_missing_before_its_deadline(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, history_id, acquisition_id = await _seed(
        db_factory,
        state=DownloadState.RETRY_PENDING,
    )
    async with db_factory() as session:
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert acquisition is not None
        acquisition.client_state = "retry_mutation_pending"
        acquisition.next_retry_at = datetime.now(UTC) + timedelta(minutes=5)
        await session.commit()
    api = _FakeApi([[]])

    result = await AirDcppReconciler(db_factory).reconcile_client(client_id, api)

    assert result.changed == 0
    assert result.missing == 0
    async with db_factory() as session:
        history = await session.get(DownloadHistory, history_id)
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert history is not None and acquisition is not None
        assert history.state is DownloadState.RETRY_PENDING
        assert acquisition.bundle_id == 91
        assert acquisition.client_state == "retry_mutation_pending"


@pytest.mark.asyncio
async def test_expired_manual_retry_claim_does_not_hide_a_committed_replacement(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, history_id, acquisition_id = await _seed(
        db_factory,
        state=DownloadState.RETRY_PENDING,
    )
    async with db_factory() as session:
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert acquisition is not None
        acquisition.client_state = "retry_mutation_pending"
        acquisition.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    class _CommitReplacementDuringSnapshotApi(_FakeApi):
        async def get_queue_bundles(
            self,
            *,
            start: int,
            count: int,
        ) -> list[AirDcppQueueBundle]:
            async with db_factory() as session:
                history = await session.get(DownloadHistory, history_id)
                acquisition = await session.get(AirDcppAcquisition, acquisition_id)
                assert history is not None and acquisition is not None
                acquisition.bundle_id = 95
                acquisition.client_state = "source_search_pending"
                acquisition.next_retry_at = datetime.now(UTC)
                history.external_id = f"airdcpp:{client_id}:bundle:95"
                await session.commit()
            return await super().get_queue_bundles(start=start, count=count)

    api = _CommitReplacementDuringSnapshotApi([[]])

    result = await AirDcppReconciler(db_factory).reconcile_client(client_id, api)

    assert result.changed == 0
    assert result.missing == 0
    async with db_factory() as session:
        history = await session.get(DownloadHistory, history_id)
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert history is not None and acquisition is not None
        assert history.state is DownloadState.RETRY_PENDING
        assert history.external_id == f"airdcpp:{client_id}:bundle:95"
        assert acquisition.bundle_id == 95
        assert acquisition.client_state == "source_search_pending"


@pytest.mark.asyncio
async def test_pre_id_reconciliation_recreates_an_expired_route_by_exact_tth(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, history_id, acquisition_id = await _seed(
        db_factory,
        state=DownloadState.RETRY_PENDING,
        bundle_id=None,
    )
    async with db_factory() as session:
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert acquisition is not None
        acquisition.search_instance_id = 44
        acquisition.grouped_result_id = "expired-result"
        acquisition.result_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        acquisition.retry_count = 1
        acquisition.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        acquisition.route_snapshot = {"queue_priority": 4}
        await session.commit()
    api = _FakeApi([[]], create_result=AirDcppQueueBundleAddInfo(id=93, merged=False))

    result = await AirDcppReconciler(db_factory).reconcile_client(client_id, api)

    assert api.retry_calls == []
    assert api.create_calls == [(_TTH, 100_000_000, "Example Comic 001 (2026).cbz", 4)]
    assert result.changed == 1
    async with db_factory() as session:
        history = await session.get(DownloadHistory, history_id)
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert history is not None and acquisition is not None
        assert acquisition.bundle_id == 93
        assert acquisition.client_state == "source_search_pending"
        assert acquisition.next_retry_at is not None
        assert history.external_id == f"airdcpp:{client_id}:bundle:93"
        assert history.state is DownloadState.SENT


@pytest.mark.asyncio
async def test_pre_id_reconciliation_exhaustion_becomes_manually_retryable_failure(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, history_id, acquisition_id = await _seed(
        db_factory,
        state=DownloadState.RETRY_PENDING,
        bundle_id=None,
    )
    async with db_factory() as session:
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert acquisition is not None
        acquisition.search_instance_id = 44
        acquisition.grouped_result_id = "opaque-result"
        acquisition.result_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        acquisition.retry_count = 2
        acquisition.max_retries = 3
        acquisition.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    api = _FakeApi([[]], retry_failure=AirDcppUnavailableError())

    result = await AirDcppReconciler(db_factory).reconcile_client(client_id, api)

    assert len(api.retry_calls) == 1
    assert result.changed == 1
    async with db_factory() as session:
        history = await session.get(DownloadHistory, history_id)
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert history is not None and acquisition is not None
        assert acquisition.bundle_id is None
        assert acquisition.client_state == "missing"
        assert acquisition.retry_count == acquisition.max_retries == 3
        assert acquisition.reconciliation_error == "queue_mutation_failed"
        assert history.state is DownloadState.FAILED
        assert history.next_retry_at is None


@pytest.mark.asyncio
async def test_pre_id_lookup_failure_does_not_consume_mutation_retry_budget(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, history_id, acquisition_id = await _seed(
        db_factory,
        state=DownloadState.RETRY_PENDING,
        bundle_id=None,
    )
    async with db_factory() as session:
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert acquisition is not None
        acquisition.search_instance_id = 44
        acquisition.grouped_result_id = "opaque-result"
        acquisition.result_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        acquisition.retry_count = 2
        acquisition.max_retries = 3
        acquisition.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    api = _FakeApi([[]], lookup_failure=AirDcppUnavailableError())

    result = await AirDcppReconciler(db_factory).reconcile_client(client_id, api)

    assert api.tth_calls == [_TTH]
    assert api.retry_calls == []
    assert api.create_calls == []
    assert result.changed == 0
    async with db_factory() as session:
        history = await session.get(DownloadHistory, history_id)
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert history is not None and acquisition is not None
        assert acquisition.bundle_id is None
        assert acquisition.client_state == "reconcile_pending"
        assert acquisition.retry_count == 2
        assert acquisition.reconciliation_error is None
        assert history.state is DownloadState.RETRY_PENDING


@pytest.mark.parametrize(
    ("merged", "expected_removed_bundles"),
    [(False, [92]), (True, [])],
)
@pytest.mark.asyncio
async def test_pre_id_recovery_does_not_resurrect_a_cancelled_download(
    db_factory: async_sessionmaker[AsyncSession],
    merged: bool,
    expected_removed_bundles: list[int],
) -> None:
    client_id, history_id, acquisition_id = await _seed(
        db_factory,
        state=DownloadState.RETRY_PENDING,
        bundle_id=None,
    )
    async with db_factory() as session:
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert acquisition is not None
        acquisition.search_instance_id = 44
        acquisition.grouped_result_id = "opaque-result"
        acquisition.result_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        acquisition.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    class _CancelDuringRetryApi(_FakeApi):
        async def download_search_result(
            self,
            instance_id: int,
            result_id: str,
            *,
            target_name: str,
            priority: int | None,
        ) -> AirDcppQueueBundleAddInfo:
            added = await super().download_search_result(
                instance_id,
                result_id,
                target_name=target_name,
                priority=priority,
            )
            async with db_factory() as session:
                history = await session.get(DownloadHistory, history_id)
                acquisition = await session.get(AirDcppAcquisition, acquisition_id)
                assert history is not None and acquisition is not None
                history.state = DownloadState.FAILED
                history.error_message = "Cancelled by user"
                acquisition.client_state = "cancelled"
                acquisition.next_retry_at = None
                await session.commit()
            return added

    api = _CancelDuringRetryApi(
        [[]],
        retry_result=AirDcppQueueBundleAddInfo(id=92, merged=merged),
    )

    result = await AirDcppReconciler(db_factory).reconcile_client(client_id, api)

    assert api.retry_calls == [(44, "opaque-result", "Example Comic 001 (2026).cbz", None)]
    assert api.removed_bundles == expected_removed_bundles
    assert result.changed == 0
    async with db_factory() as session:
        history = await session.get(DownloadHistory, history_id)
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert history is not None and acquisition is not None
        assert history.state is DownloadState.FAILED
        assert history.error_message == "Cancelled by user"
        assert acquisition.bundle_id is None
        assert acquisition.client_state == "cancelled"


@pytest.mark.asyncio
async def test_pre_id_recovery_failure_does_not_resurrect_a_cancelled_download(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    client_id, history_id, acquisition_id = await _seed(
        db_factory,
        state=DownloadState.RETRY_PENDING,
        bundle_id=None,
    )
    async with db_factory() as session:
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert acquisition is not None
        acquisition.search_instance_id = 44
        acquisition.grouped_result_id = "opaque-result"
        acquisition.result_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        acquisition.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    class _CancelDuringFailedRetryApi(_FakeApi):
        async def download_search_result(
            self,
            instance_id: int,
            result_id: str,
            *,
            target_name: str,
            priority: int | None,
        ) -> AirDcppQueueBundleAddInfo:
            self.retry_calls.append((instance_id, result_id, target_name, priority))
            async with db_factory() as session:
                history = await session.get(DownloadHistory, history_id)
                acquisition = await session.get(AirDcppAcquisition, acquisition_id)
                assert history is not None and acquisition is not None
                history.state = DownloadState.FAILED
                history.error_message = "Cancelled by user"
                acquisition.client_state = "cancelled"
                acquisition.next_retry_at = None
                await session.commit()
            raise AirDcppUnavailableError()

    api = _CancelDuringFailedRetryApi([[]])

    result = await AirDcppReconciler(db_factory).reconcile_client(client_id, api)

    assert api.retry_calls == [(44, "opaque-result", "Example Comic 001 (2026).cbz", None)]
    assert result.changed == 0
    async with db_factory() as session:
        history = await session.get(DownloadHistory, history_id)
        acquisition = await session.get(AirDcppAcquisition, acquisition_id)
        assert history is not None and acquisition is not None
        assert history.state is DownloadState.FAILED
        assert history.error_message == "Cancelled by user"
        assert acquisition.client_state == "cancelled"
        assert acquisition.retry_count == 0
