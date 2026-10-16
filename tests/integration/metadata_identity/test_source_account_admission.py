"""Provider account failures are durable and shared, without hiding healthy sources."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from pullbox.core.encryption import encrypt_secret
from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.models.metadata_source_account import MetadataSourceAccount as Account
from pullbox.schemas.metadata_sources import SeriesDiscoveryQuery, SourceCapability, SourceStatus
from pullbox.services.metadata_discovery import MetadataSourceError, MetadataSourceRegistry
from pullbox.services.metadata_sources import load_source_runtime
from tests.integration.metadata_identity.test_series_adoption import (
    configured_sources,  # noqa: F401
)
from tests.unit.test_metadata_discovery import Adapter, registration
from tests.unit.test_metadata_source_reads import ReadAdapter


@pytest.fixture
async def accounts(identity_probe_db):
    engine, factory, _ = identity_probe_db
    async with factory.begin() as session:
        await session.execute(
            update(MetadataSourceConfig)
            .where(MetadataSourceConfig.source == Source.METRON_API.value)
            .values(credential_secret=encrypt_secret("account-test-token"))
        )
    return engine, factory


async def reader(factory, *adapters):
    async with factory() as session:
        runtime = await load_source_runtime(session, gcd_api_enabled=False)
    return MetadataSourceRegistry(
        runtime,
        factories={
            item.source: registration(item, capabilities=list(SourceCapability))
            for item in adapters
        },
    )


@pytest.mark.parametrize(
    "failure",
    [
        SourceStatus.RATE_LIMITED,
        SourceStatus.TIMEOUT,
        SourceStatus.UNAVAILABLE,
        SourceStatus.AUTHENTICATION_FAILED,
    ],
)
async def test_failure_holds_other_series_after_registry_and_engine_restart(accounts, failure):
    engine, factory = accounts
    adapter = ReadAdapter(error=MetadataSourceError(failure, retry_after_seconds=720))
    assert (await (await reader(factory, adapter)).series(adapter.source, "42")).status is failure
    await engine.dispose()
    result = await (await reader(factory, adapter)).series(adapter.source, "43")
    assert result.status is failure
    assert len(adapter.calls) == 1, "Known account failures must not be retried for every series"
    if failure is not SourceStatus.AUTHENTICATION_FAILED:
        assert 0 < result.retry_after_seconds <= 720


async def test_search_failure_is_shared_with_exact_reads_but_local_source_still_works(accounts):
    _, factory = accounts
    remote = ReadAdapter(error=MetadataSourceError(SourceStatus.RATE_LIMITED, 720))
    local = Adapter(Source.COMICVINE_LOCAL)
    first = await (await reader(factory, remote, local)).discover(
        SeriesDiscoveryQuery(query="Example", sources=[remote.source, local.source])
    )
    assert len(first.results) == 1 and first.results[0].source is local.source
    result = await (await reader(factory, remote)).series(remote.source, "42")
    assert result.status is SourceStatus.RATE_LIMITED
    assert remote.calls == [0], "Search and catalog reads must share the same account guard"


async def test_priority_edit_does_not_bypass_authentication_hold_but_new_token_does(accounts):
    _, factory = accounts
    adapter = ReadAdapter(error=MetadataSourceError(SourceStatus.AUTHENTICATION_FAILED))
    await (await reader(factory, adapter)).series(adapter.source, "42")
    async with factory.begin() as session:
        await session.execute(
            update(MetadataSourceConfig)
            .where(MetadataSourceConfig.source == adapter.source.value)
            .values(priority=12, revision=2)
        )
    adapter.error = None
    assert (await (await reader(factory, adapter)).series(adapter.source, "42")).status is (
        SourceStatus.AUTHENTICATION_FAILED
    ), "Changing priority must not repeatedly submit the same rejected token"
    assert len(adapter.calls) == 1
    async with factory.begin() as session:
        await session.execute(
            update(MetadataSourceConfig)
            .where(MetadataSourceConfig.source == adapter.source.value)
            .values(credential_secret=encrypt_secret("replacement-account-token"))
        )
    assert (await (await reader(factory, adapter)).series(adapter.source, "42")).status is (
        SourceStatus.OK
    )
    assert len(adapter.calls) == 2


async def expire_account(factory):
    async with factory.begin() as session:
        await session.execute(
            update(Account).values(retry_at=datetime.now(UTC) - timedelta(seconds=1))
        )


async def throttled_account(accounts):
    _, factory = accounts
    adapter = ReadAdapter(error=MetadataSourceError(SourceStatus.RATE_LIMITED, 720))
    await (await reader(factory, adapter)).series(adapter.source, "42")
    await expire_account(factory)
    adapter.error = None
    adapter.started.clear()
    return factory, adapter


async def test_expired_cooldown_allows_one_probe_and_success_reopens_account(accounts):
    factory, adapter = await throttled_account(accounts)
    adapter.wait = asyncio.Event()
    registry = await reader(factory, adapter)
    task = asyncio.create_task(registry.series(adapter.source, "42"))
    try:
        await asyncio.wait_for(adapter.started.wait(), 3)
        result = await (await reader(factory, adapter)).series(adapter.source, "43")
        assert result.status is SourceStatus.RATE_LIMITED
        assert 0 < result.retry_after_seconds <= 45
        assert len(adapter.calls) == 2, "Only one expired-account probe may reach the provider"
        adapter.wait.set()
        assert (await task).status is SourceStatus.OK
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert (
        await (await reader(factory, adapter)).series(adapter.source, "42")
    ).status is SourceStatus.OK
    async with factory() as session:
        row = await session.scalar(select(Account))
        assert row.status is None and row.retry_at is None and row.lease_until is None


async def test_cancelled_probe_releases_lease_without_discarding_prior_failure(accounts):
    factory, adapter = await throttled_account(accounts)
    adapter.wait = asyncio.Event()
    task = asyncio.create_task((await reader(factory, adapter)).series(adapter.source, "42"))
    try:
        await asyncio.wait_for(adapter.started.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with factory() as session:
        row = await session.scalar(select(Account))
        assert row.status == "rate_limited" and row.lease_until is None
    adapter.wait = None
    assert (
        await (await reader(factory, adapter)).series(adapter.source, "42")
    ).status is SourceStatus.OK


async def test_abandoned_probe_recovers_after_lease_expiry(accounts):
    factory, adapter = await throttled_account(accounts)
    instance = await reader(factory, adapter)
    runtime = instance.runtime[adapter.source]
    permit = await runtime.account_admission.admit(runtime)
    assert permit.probe
    assert (await instance.series(adapter.source, "42")).status is SourceStatus.RATE_LIMITED
    async with factory.begin() as session:
        await session.execute(
            update(Account).values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert (await instance.series(adapter.source, "42")).status is SourceStatus.OK


async def test_probe_is_not_claimed_while_waiting_for_local_request_capacity(accounts, monkeypatch):
    factory, adapter = await throttled_account(accounts)
    instance = await reader(factory, adapter)
    instance.read_slots = asyncio.Semaphore(0)
    gate = instance.runtime[adapter.source].account_admission
    original = gate.admit
    entered = asyncio.Event()

    async def admission(runtime, **kwargs):
        entered.set()
        return await original(runtime, **kwargs)

    monkeypatch.setattr(gate, "admit", admission)
    task = asyncio.create_task(instance.series(adapter.source, "42"))
    try:
        await asyncio.sleep(0)
        assert not entered.is_set(), "A queued read must not spend or outlive its probe lease"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stale_probe_outcome_cannot_clear_or_restore_newer_state(accounts):
    from pullbox.schemas.metadata_sources import SourceOutcome

    factory, adapter = await throttled_account(accounts)
    runtime = (await reader(factory, adapter)).runtime[adapter.source]
    obsolete = await runtime.account_admission.admit(runtime)
    obsolete.started = True
    async with factory.begin() as session:
        await session.execute(
            update(Account).values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    newer = await runtime.account_admission.admit(runtime)
    newer.started = True
    newer.outcome = SourceOutcome(source=adapter.source, status=SourceStatus.AUTHENTICATION_FAILED)
    await runtime.account_admission.finish(runtime, newer)
    obsolete.outcome = SourceOutcome(source=adapter.source, status=SourceStatus.OK)
    await runtime.account_admission.finish(runtime, obsolete)
    async with factory() as session:
        assert (await session.scalar(select(Account))).status == "authentication_failed"


async def test_late_success_cannot_clear_a_newer_account_failure(accounts):
    _, factory = accounts
    healthy = ReadAdapter(wait=asyncio.Event())
    task = asyncio.create_task((await reader(factory, healthy)).series(healthy.source, "42"))
    try:
        await asyncio.wait_for(healthy.started.wait(), 3)
        failing = ReadAdapter(error=MetadataSourceError(SourceStatus.AUTHENTICATION_FAILED))
        await (await reader(factory, failing)).series(failing.source, "43")
        healthy.wait.set()
        assert (await task).status is SourceStatus.OK
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert (
        await (await reader(factory, healthy)).series(healthy.source, "42")
    ).status is SourceStatus.AUTHENTICATION_FAILED
    assert len(healthy.calls) == 1


async def test_obsolete_runtime_cannot_restore_a_replaced_credential(accounts):
    _, factory = accounts
    adapter = ReadAdapter()
    obsolete = await reader(factory, adapter)
    async with factory.begin() as session:
        await session.execute(
            update(MetadataSourceConfig)
            .where(
                MetadataSourceConfig.source == adapter.source.value,
            )
            .values(credential_secret=encrypt_secret("newer-account-token"))
        )
    current = await reader(factory, adapter)
    assert (await current.series(adapter.source, "42")).status is SourceStatus.OK
    assert (await obsolete.series(adapter.source, "42")).status is SourceStatus.UNAVAILABLE
    assert len(adapter.calls) == 1
    assert (await current.series(adapter.source, "42")).status is SourceStatus.OK


async def test_blocked_checks_do_not_extend_the_cooldown(accounts):
    _, factory = accounts
    adapter = ReadAdapter(error=MetadataSourceError(SourceStatus.UNAVAILABLE, 720))
    await (await reader(factory, adapter)).series(adapter.source, "42")
    async with factory() as session:
        row = await session.scalar(select(Account))
        previous = row.retry_at, row.revision
        assert "account-test-token" not in row.account_key
    for _ in range(3):
        await (await reader(factory, adapter)).series(adapter.source, "43")
    async with factory() as session:
        row = await session.scalar(select(Account))
        assert (row.retry_at, row.revision) == previous
    assert len(adapter.calls) == 1


@pytest.mark.parametrize("status", [SourceStatus.NOT_FOUND, SourceStatus.INCOMPATIBLE_RESPONSE])
async def test_entity_errors_do_not_block_other_requests(accounts, status):
    _, factory = accounts
    adapter = ReadAdapter(error=MetadataSourceError(status))
    await (await reader(factory, adapter)).series(adapter.source, "42")
    adapter.error = None
    assert (
        await (await reader(factory, adapter)).series(adapter.source, "42")
    ).status is SourceStatus.OK
    assert len(adapter.calls) == 2


async def test_missing_admission_database_fails_closed_without_provider_io(accounts, monkeypatch):
    _, factory = accounts
    adapter = ReadAdapter()
    instance = await reader(factory, adapter)
    gate = instance.runtime[adapter.source].account_admission
    logged = Mock()
    monkeypatch.setattr("pullbox.services.metadata_account_admission.logger", logged)
    attempts = 0

    def unavailable():
        nonlocal attempts
        attempts += 1
        raise SQLAlchemyError("private-database-detail")

    monkeypatch.setattr(gate.factory, "begin", unavailable)
    result = await instance.series(adapter.source, "42")
    assert result.status is SourceStatus.UNAVAILABLE and result.retry_after_seconds == 5
    assert attempts == 1 and adapter.calls == []
    assert logged.warning.call_args.kwargs["failure_kind"] == "database"
    assert logged.warning.call_args.kwargs["error_type"] == "SQLAlchemyError"
    assert "private-database-detail" not in str(logged.mock_calls)


async def test_transient_admission_timeout_rechecks_before_provider_io(accounts, monkeypatch):
    _, factory = accounts
    adapter = ReadAdapter()
    instance = await reader(factory, adapter)
    gate = instance.runtime[adapter.source].account_admission
    begin = gate.factory.begin
    attempts = 0

    @asynccontextmanager
    async def stalled_once():
        nonlocal attempts
        attempts += 1
        async with begin() as session:
            await session.scalar(select(Account.id))
            if attempts == 1:
                await asyncio.sleep(3)
            yield session

    monkeypatch.setattr(gate.factory, "begin", stalled_once)
    result = await instance.series(adapter.source, "42")
    assert result.status is SourceStatus.OK, "A transient local stall is not a provider outage"
    assert attempts == 2 and len(adapter.calls) == 1


async def test_admission_retry_remains_inside_the_request_deadline(accounts, monkeypatch):
    _, factory = accounts
    adapter = ReadAdapter()
    instance = await reader(factory, adapter)
    instance.total_timeout = 1
    gate = instance.runtime[adapter.source].account_admission
    begin = gate.factory.begin
    entered = asyncio.Event()
    attempts = 0

    @asynccontextmanager
    async def stalled():
        nonlocal attempts
        attempts += 1
        async with begin() as session:
            entered.set()
            await asyncio.Event().wait()
            yield session

    monkeypatch.setattr(gate.factory, "begin", stalled)
    result = await asyncio.wait_for(instance.series(adapter.source, "42"), 5)
    assert entered.is_set() and result.status is SourceStatus.TIMEOUT
    assert attempts == 1 and adapter.calls == []


async def test_admission_retry_revalidates_a_changed_credential(accounts, monkeypatch):
    _, factory = accounts
    adapter = ReadAdapter()
    instance = await reader(factory, adapter)
    gate = instance.runtime[adapter.source].account_admission
    begin = gate.factory.begin
    attempts = 0

    @asynccontextmanager
    async def changed_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            async with factory.begin() as session:
                await session.execute(
                    update(MetadataSourceConfig)
                    .where(MetadataSourceConfig.source == adapter.source.value)
                    .values(credential_secret=encrypt_secret("changed-during-local-stall"))
                )
        async with begin() as session:
            if attempts == 1:
                await asyncio.sleep(3)
            yield session

    monkeypatch.setattr(gate.factory, "begin", changed_once)
    result = await instance.series(adapter.source, "42")
    assert attempts == 2, "A local timeout should allow exactly one revalidated attempt"
    assert result.status is SourceStatus.UNAVAILABLE and adapter.calls == []


async def test_persistent_admission_timeout_stops_after_one_retry(accounts, monkeypatch):
    engine, factory = accounts
    adapter = ReadAdapter()
    instance = await reader(factory, adapter)
    gate = instance.runtime[adapter.source].account_admission
    begin = gate.factory.begin
    logged = Mock()
    monkeypatch.setattr("pullbox.services.metadata_account_admission.logger", logged)
    attempts = 0

    @asynccontextmanager
    async def stalled():
        nonlocal attempts
        attempts += 1
        async with begin() as session:
            await session.scalar(select(Account.id))
            await asyncio.Event().wait()
            yield session

    monkeypatch.setattr(gate.factory, "begin", stalled)
    result = await instance.series(adapter.source, "42")
    assert result.status is SourceStatus.UNAVAILABLE and result.retry_after_seconds == 5
    assert attempts == 2 and adapter.calls == []
    assert engine.pool.checkedout() == 0
    assert logged.debug.call_count == 1 and logged.warning.call_count == 1
    assert logged.warning.call_args.kwargs["failure_kind"] == "timeout"


async def test_cancelled_admission_does_not_retry_or_call_provider(accounts, monkeypatch):
    _, factory = accounts
    adapter = ReadAdapter()
    instance = await reader(factory, adapter)
    gate = instance.runtime[adapter.source].account_admission
    begin = gate.factory.begin
    entered = asyncio.Event()
    attempts = 0

    @asynccontextmanager
    async def stalled():
        nonlocal attempts
        attempts += 1
        async with begin() as session:
            entered.set()
            await asyncio.Event().wait()
            yield session

    monkeypatch.setattr(gate.factory, "begin", stalled)
    task = asyncio.create_task(instance.series(adapter.source, "42"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert attempts == 1 and adapter.calls == []


async def test_fresh_cached_metadata_remains_readable_without_bypassing_http_hold(accounts):
    from pullbox.services.metadata_read_cache import MetadataReadCache

    _, factory = accounts
    adapter = ReadAdapter()
    instance = await reader(factory, adapter)
    instance.read_cache = MetadataReadCache(factory)
    assert (await instance.series(adapter.source, "42")).status is SourceStatus.OK
    adapter.error = MetadataSourceError(SourceStatus.AUTHENTICATION_FAILED)
    await (await reader(factory, adapter)).series(adapter.source, "43")
    cached = await reader(factory, adapter)
    cached.read_cache = MetadataReadCache(factory)
    assert (await cached.series(adapter.source, "42")).status is SourceStatus.OK
    cached.revalidate_reads = True
    assert (await cached.series(adapter.source, "42")).status is SourceStatus.AUTHENTICATION_FAILED
    assert len(adapter.calls) == 2


async def test_admission_releases_database_before_io_and_has_bounded_query_cost(accounts):
    engine, factory = accounts
    adapter = ReadAdapter()
    instance = await reader(factory, adapter)
    original = adapter.series
    statements = []

    async def fetch(*args, **kwargs):
        assert engine.pool.checkedout() == 0, (
            "Account admission cannot hold a connection during HTTP"
        )
        return await original(*args, **kwargs)

    def record(*args):
        statements.append(args[2])

    adapter.series = fetch
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        assert (await instance.series(adapter.source, "42")).status is SourceStatus.OK
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert len(statements) <= 10


async def test_local_sources_do_not_create_or_depend_on_account_state(accounts):
    _, factory = accounts
    local = Adapter(Source.COMICVINE_LOCAL)
    result = await (await reader(factory, local)).discover(
        SeriesDiscoveryQuery(query="Example", sources=[local.source])
    )
    assert len(result.results) == 1
    async with factory() as session:
        assert list(await session.scalars(select(Account))) == []


async def test_account_recheck_does_not_decrypt_a_feature_disabled_source(accounts, monkeypatch):
    from pullbox.services import metadata_sources

    _, factory = accounts
    hidden = encrypt_secret("feature-disabled-gcd-token")
    async with factory.begin() as session:
        await session.execute(
            update(MetadataSourceConfig)
            .where(
                MetadataSourceConfig.source == Source.GCD_API_V2.value,
            )
            .values(credential_secret=hidden)
        )
    adapter = ReadAdapter()
    instance = await reader(factory, adapter)
    decrypted = []
    original = metadata_sources.decrypt_secret

    def decrypt(value):
        decrypted.append(value)
        return original(value)

    monkeypatch.setattr(metadata_sources, "decrypt_secret", decrypt)
    assert (await instance.series(adapter.source, "42")).status is SourceStatus.OK
    assert hidden not in decrypted, "Account admission must preserve the GCD release flag"


async def test_account_schema_rejects_invalid_healthy_deadline(accounts):
    _, factory = accounts
    async with factory() as session:
        session.add(
            Account(
                source=Source.METRON_API.value,
                account_key="a" * 64,
                status=None,
                retry_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


async def test_account_migration_matches_model_and_preserves_library(identity_probe_db):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import MetaData

    from pullbox.models import Base, Series
    from tests.integration.metadata_identity.test_production_migration import _revision

    engine, factory, _ = identity_probe_db
    async with factory.begin() as session:
        series = Series(title="Preserved account migration", sort_title="Preserved")
        session.add(series)
        await session.flush()
        identifier = series.id
    async with engine.begin() as connection:

        def migrate(sync):
            revision = _revision("w4q5r6s7t890_add_metadata_source_accounts", sync)
            revision.downgrade()
            revision.upgrade()
            _revision("x5r6s7t8u901_allow_metadata_authentication_probes", sync).upgrade()
            expected = MetaData()
            name = "metadata_source_accounts"
            Base.metadata.tables[name].to_metadata(expected)
            context = MigrationContext.configure(
                sync,
                opts={
                    "include_object": lambda obj, item, type_, reflected, compare_to: (
                        item == name if type_ == "table" else True
                    ),
                    "compare_server_default": True,
                },
            )
            assert compare_metadata(context, expected) == []

        await connection.run_sync(migrate)
    async with factory() as session:
        assert (await session.get(Series, identifier)).title == "Preserved account migration"
