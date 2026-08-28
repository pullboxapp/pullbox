"""AirDC++ runtime composition and feature-flag isolation contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pullbox.composition.airdcpp import (
    build_airdcpp_supervisor_configs,
    get_airdcpp_reconciliation_task_count,
    get_airdcpp_search_coordinator,
    load_airdcpp_search_clients,
    start_airdcpp_supervisor_registry,
    stop_airdcpp_supervisor_registry,
)
from pullbox.providers.airdcpp.supervisor import AirDcppSupervisorState


class _Scalars:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[object]:
        return self._values


class _Result:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def scalars(self) -> _Scalars:
        return _Scalars(self._values)


class _Session:
    def __init__(self, clients: list[object]) -> None:
        self.clients = clients
        self.execute_calls = 0

    async def execute(self, _statement: object) -> _Result:
        self.execute_calls += 1
        return _Result(self.clients)

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Registry:
    def __init__(self, **_kwargs: object) -> None:
        self.applied: tuple[object, ...] = ()
        self.stopped = False

    async def apply(self, configs: tuple[object, ...]) -> None:
        self.applied = configs

    async def stop(self) -> None:
        self.stopped = True


def _client() -> SimpleNamespace:
    return SimpleNamespace(
        id=12,
        name="Dedicated Air",
        url="http://airdcpp-vpn:5600",
        username="pullbox",
        password="encrypted-password",
        enabled=True,
        airdcpp_settings=SimpleNamespace(request_timeout_seconds=20),
    )


@pytest.mark.asyncio
async def test_composition_loads_only_bounded_exact_client_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([_client()])
    monkeypatch.setattr(
        "pullbox.composition.airdcpp.decrypt_secret",
        lambda value: "decrypted" if value == "encrypted-password" else "wrong",
    )

    configs = await build_airdcpp_supervisor_configs(session)  # type: ignore[arg-type]

    assert len(configs) == 1
    config = configs[0]
    assert config.config_id == 12
    assert config.client_identity == "airdcpp:12"
    assert config.base_url == "http://airdcpp-vpn:5600"
    assert config.password.get_secret_value() == "decrypted"
    assert config.request_timeout_seconds == 20


@pytest.mark.asyncio
async def test_feature_off_starts_no_session_query_or_supervisors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def session_factory() -> Any:
        calls.append("session")
        return _Session([_client()])

    monkeypatch.setattr("pullbox.composition.airdcpp.AirDcppSupervisorRegistry", _Registry)

    registry = await start_airdcpp_supervisor_registry(
        session_factory,
        enabled=False,
    )

    assert calls == []
    assert registry is None
    assert get_airdcpp_reconciliation_task_count() == 0


@pytest.mark.asyncio
async def test_feature_on_loads_configs_and_schedules_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session([_client()])
    monkeypatch.setattr("pullbox.composition.airdcpp.decrypt_secret", lambda _value: "decrypted")
    monkeypatch.setattr("pullbox.composition.airdcpp.AirDcppSupervisorRegistry", _Registry)

    registry = await start_airdcpp_supervisor_registry(lambda: session, enabled=True)

    assert isinstance(registry, _Registry)
    assert len(registry.applied) == 1
    assert session.execute_calls == 1
    assert get_airdcpp_search_coordinator() is not None
    assert get_airdcpp_reconciliation_task_count() == 1
    await stop_airdcpp_supervisor_registry()
    assert get_airdcpp_reconciliation_task_count() == 0


@pytest.mark.asyncio
async def test_search_composition_uses_only_ready_search_enabled_exact_clients() -> None:
    ready_api = object()
    ready_socket = object()
    ready = SimpleNamespace(
        state=AirDcppSupervisorState.READY,
        api_client=ready_api,
        socket_client=ready_socket,
    )
    unavailable = SimpleNamespace(
        state=AirDcppSupervisorState.UNAVAILABLE,
        api_client=object(),
        socket_client=object(),
    )
    clients = []
    for config_id, search_enabled in ((1, True), (2, True), (3, False)):
        client = _client()
        client.id = config_id
        client.priority = 10 + config_id
        client.airdcpp_settings.search_enabled = search_enabled
        client.airdcpp_settings.manual_collection_seconds = 8
        client.airdcpp_settings.automatic_collection_seconds = 15
        client.airdcpp_settings.max_results = 200
        client.airdcpp_settings.max_retained_routes = 400
        client.airdcpp_settings.max_concurrent_searches = 1
        client.airdcpp_settings.search_dispatch_deadline_seconds = 45
        client.airdcpp_settings.hub_allowlist = []
        clients.append(client)
    registry = SimpleNamespace(get=lambda config_id: {1: ready, 2: unavailable}.get(config_id))

    operations = await load_airdcpp_search_clients(  # type: ignore[arg-type]
        _Session(clients),
        registry,
    )

    assert len(operations) == 1
    assert operations[0].config_id == 1
    assert operations[0].api_client is ready_api
    assert operations[0].socket_client is ready_socket
    assert operations[0].max_concurrent_searches == 1


@pytest.mark.asyncio
async def test_automatic_search_composition_requires_per_client_opt_in() -> None:
    clients = []
    supervisors: dict[int, object] = {}
    for config_id, automatic in ((1, False), (2, True)):
        client = _client()
        client.id = config_id
        client.priority = 20
        client.airdcpp_settings.search_enabled = True
        client.airdcpp_settings.automatic_search_enabled = automatic
        client.airdcpp_settings.manual_collection_seconds = 8
        client.airdcpp_settings.automatic_collection_seconds = 15
        client.airdcpp_settings.max_results = 200
        client.airdcpp_settings.max_retained_routes = 400
        client.airdcpp_settings.max_concurrent_searches = 1
        client.airdcpp_settings.search_dispatch_deadline_seconds = 45
        client.airdcpp_settings.hub_allowlist = []
        clients.append(client)
        supervisors[config_id] = SimpleNamespace(
            state=AirDcppSupervisorState.READY,
            api_client=object(),
            socket_client=object(),
        )
    registry = SimpleNamespace(get=supervisors.get)

    operations = await load_airdcpp_search_clients(  # type: ignore[arg-type]
        _Session(clients),
        registry,
        automatic=True,
    )

    assert [operation.config_id for operation in operations] == [2]


@pytest.mark.asyncio
async def test_completed_reconciliation_triggers_immediate_post_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pullbox.composition.airdcpp as composition

    calls: list[object] = []

    class _Reconciler:
        async def reconcile_client(self, config_id: int, _api: object) -> object:
            calls.append(config_id)
            return SimpleNamespace(completed=1)

    scheduler = SimpleNamespace(run_task_now=lambda task_id: calls.append(task_id))
    supervisor = SimpleNamespace(
        state=AirDcppSupervisorState.READY,
        api_client=object(),
    )
    monkeypatch.setattr(composition, "_reconciler", _Reconciler())
    monkeypatch.setattr(composition, "_registry", SimpleNamespace(get=lambda _id: supervisor))
    monkeypatch.setattr("pullbox.core.scheduler.get_scheduler", lambda: scheduler)

    await composition._reconcile_ready_client(12)

    assert calls == [12, "process_completed"]
