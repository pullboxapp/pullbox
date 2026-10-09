"""One-time GCD sign-in uses real operator auth, policy saves and offline wire I/O."""

import asyncio
import json
import os
import sys

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from pullbox.config import get_settings
from pullbox.core.encryption import decrypt_secret
from pullbox.core.provider_cooldown import ProviderCooldown
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.providers.metadata.gcd_api_v2 import GcdApiV2Source
from tests.api.test_metadata_sources_api import csrf, policy
from tests.unit.test_gcd_api_v2 import series_row
from tests.unit.test_gcd_sign_in import PASSWORD, TOKEN, USERNAME

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest_plugins = ["conftest_security"]
URL = "/api/v1/metadata/sources/gcd_api_v2/sign-in"


def body(**updates):
    return {"revision": 0, "username": USERNAME, "password": PASSWORD, **updates}


@pytest.fixture
def wire(sec_app, monkeypatch):
    from pullbox.api.deps import get_settings_dep
    from pullbox.providers.metadata import gcd_api_v2, sources

    settings = get_settings().model_copy(update={"metadata_gcd_api_v2_enabled": True})
    sec_app.dependency_overrides[get_settings_dep] = lambda: settings
    calls, responses = [], {"exchange": 200, "check": 200}
    cooldown = ProviderCooldown()
    original = gcd_api_v2.exchange_gcd_token

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            assert request.url == "https://beta.comics.org/api/v2/auth/token/"
            assert json.loads(request.content) == {"username": USERNAME, "password": PASSWORD}
            return httpx.Response(
                responses["exchange"],
                json={
                    "token": TOKEN,
                }
                if responses["exchange"] == 200
                else {"detail": PASSWORD},
            )
        assert request.url.path == "/api/v2/series/"
        assert request.headers["authorization"] == f"Token {TOKEN}"
        assert dict(request.url.params) == {"page_size": "1", "page": "1"}
        return httpx.Response(
            responses["check"], json={"count": 1, "next": None, "results": [series_row()]}
        )

    async def exchange(username, password):
        return await original(
            username, password, transport=httpx.MockTransport(handler), cooldown=cooldown
        )

    monkeypatch.setattr(gcd_api_v2, "exchange_gcd_token", exchange)
    monkeypatch.setattr(
        sources,
        "GcdApiV2Source",
        lambda token: GcdApiV2Source(
            token, transport=httpx.MockTransport(handler), cooldown=ProviderCooldown()
        ),
    )
    return {"calls": calls, "responses": responses, "settings": settings}


async def test_sign_in_checks_then_saves_only_encrypted_token(
    authenticated_client, sec_db, wire, caplog
):
    result = await authenticated_client.post(URL, json=body(), headers=csrf(authenticated_client))
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved["enabled"] and saved["credential_configured"] and saved["revision"] == 1
    assert saved["last_status"] == "ok" and saved["last_success_at"]
    assert [request.method for request in wire["calls"]] == ["POST", "GET"]
    async with sec_db() as session:
        row = await session.scalar(
            select(MetadataSourceConfig).where(MetadataSourceConfig.source == "gcd_api_v2")
        )
        assert row.credential_secret.startswith("enc:")
        assert decrypt_secret(row.credential_secret) == TOKEN
        assert row.settings == {} and row.priority == 50
        assert USERNAME not in repr(row.__dict__) and PASSWORD not in repr(row.__dict__)
    assert all(value not in result.text + caplog.text for value in (USERNAME, PASSWORD, TOKEN))


@pytest.mark.parametrize("failure", ["exchange", "check"])
async def test_failed_sign_in_retains_existing_disabled_token_and_priority(
    authenticated_client, sec_db, wire, failure
):
    existing = await authenticated_client.put(
        "/api/v1/metadata/sources/gcd_api_v2",
        json=policy(enabled=False, priority=7, credential="synthetic-existing-token"),
        headers=csrf(authenticated_client),
    )
    assert existing.status_code == 200
    wire["responses"][failure] = 403
    result = await authenticated_client.post(
        URL, json=body(revision=1), headers=csrf(authenticated_client)
    )
    assert result.status_code == 400 and PASSWORD not in result.text
    async with sec_db() as session:
        row = await session.scalar(
            select(MetadataSourceConfig).where(MetadataSourceConfig.source == "gcd_api_v2")
        )
        assert decrypt_secret(row.credential_secret) == "synthetic-existing-token"
        assert not row.enabled and row.priority == 7 and row.revision == 1


async def test_stale_revision_refuses_sign_in_before_any_network_io(authenticated_client, wire):
    assert (
        await authenticated_client.put(
            "/api/v1/metadata/sources/gcd_api_v2",
            json=policy(enabled=False),
            headers=csrf(authenticated_client),
        )
    ).status_code == 200
    result = await authenticated_client.post(URL, json=body(), headers=csrf(authenticated_client))
    assert result.status_code == 409 and not wire["calls"]


async def test_flag_off_refuses_exchange_and_persistence(
    authenticated_client, sec_app, wire, sec_db
):
    from pullbox.api.deps import get_settings_dep

    sec_app.dependency_overrides[get_settings_dep] = lambda: wire["settings"].model_copy(
        update={"metadata_gcd_api_v2_enabled": False}
    )
    result = await authenticated_client.post(URL, json=body(), headers=csrf(authenticated_client))
    assert result.status_code == 400 and not wire["calls"]
    async with sec_db() as session:
        assert await session.scalar(select(MetadataSourceConfig)) is None


async def test_sign_in_requires_interactive_operator_and_csrf(
    authenticated_client, unauthenticated_client, sec_api_key, wire
):
    assert (await authenticated_client.post(URL, json=body())).status_code == 403
    assert (await unauthenticated_client.post(URL, json=body())).status_code in {401, 403}
    assert (
        await unauthenticated_client.post(URL, json=body(), headers={"X-API-Key": sec_api_key})
    ).status_code in {401, 403}
    assert not wire["calls"]


@pytest.mark.parametrize(
    "payload",
    [
        {"password": PASSWORD},
        body(password=123),
        body(username="x" * 1025),
        body(password="x" * 4097),
        body(revision=True),
        body(url=PASSWORD),
    ],
)
async def test_validation_never_echoes_submitted_credentials(
    authenticated_client, wire, payload, caplog
):
    result = await authenticated_client.post(URL, json=payload, headers=csrf(authenticated_client))
    assert result.status_code == 422
    assert all(value not in result.text + caplog.text for value in (USERNAME, PASSWORD, TOKEN))
    assert not wire["calls"]


async def test_malformed_and_oversized_input_never_echoes_body(authenticated_client, wire):
    for raw, status in [(f'{{"password":"{PASSWORD}",', 422), (PASSWORD * 4000, 413)]:
        result = await authenticated_client.post(
            URL,
            content=raw,
            headers={**csrf(authenticated_client), "Content-Type": "application/json"},
        )
        assert result.status_code == status and PASSWORD not in result.text
    assert not wire["calls"]


@pytest.mark.parametrize("failure", ["exchange", "check"])
@pytest.mark.parametrize("remote,status", [(429, 429), (503, 502)])
async def test_provider_holds_and_unavailability_are_safe_and_do_not_save(
    authenticated_client, sec_db, wire, failure, remote, status, caplog
):
    wire["responses"][failure] = remote
    result = await authenticated_client.post(URL, json=body(), headers=csrf(authenticated_client))
    assert result.status_code == status
    assert all(value not in result.text + caplog.text for value in (USERNAME, PASSWORD, TOKEN))
    async with sec_db() as session:
        assert await session.scalar(select(MetadataSourceConfig)) is None


async def test_sign_in_does_not_hold_a_transaction_during_exchange_or_connection_check(
    authenticated_client, wire, monkeypatch
):
    from pullbox.services import gcd_sign_in as service

    original_read = service.read_source_policies
    original_exchange = service.gcd_api_v2.exchange_gcd_token
    original_check = service.MetadataSourceRegistry.check
    held, checks = [], []

    async def read(session):
        held.append(session)
        return await original_read(session)

    async def exchange(*args):
        assert held and not held[0].in_transaction()
        checks.append("exchange")
        return await original_exchange(*args)

    async def check(self, *args, **kwargs):
        assert held and not held[0].in_transaction()
        checks.append("check")
        return await original_check(self, *args, **kwargs)

    monkeypatch.setattr(service, "read_source_policies", read)
    monkeypatch.setattr(service.gcd_api_v2, "exchange_gcd_token", exchange)
    monkeypatch.setattr(service.MetadataSourceRegistry, "check", check)
    result = await authenticated_client.post(URL, json=body(), headers=csrf(authenticated_client))
    assert result.status_code == 200 and checks == ["exchange", "check"]


async def test_edit_during_exchange_is_not_overwritten_by_the_returned_token(
    authenticated_client, sec_db, wire, monkeypatch
):
    from pullbox.services import gcd_sign_in as service

    original = service.gcd_api_v2.exchange_gcd_token

    async def exchange(*args):
        updated = await authenticated_client.put(
            "/api/v1/metadata/sources/gcd_api_v2",
            json=policy(enabled=False, priority=3, credential="synthetic-concurrent-token"),
            headers=csrf(authenticated_client),
        )
        assert updated.status_code == 200
        return await original(*args)

    monkeypatch.setattr(service.gcd_api_v2, "exchange_gcd_token", exchange)
    result = await authenticated_client.post(URL, json=body(), headers=csrf(authenticated_client))
    assert result.status_code == 409
    async with sec_db() as session:
        row = await session.scalar(
            select(MetadataSourceConfig).where(MetadataSourceConfig.source == "gcd_api_v2")
        )
        assert decrypt_secret(row.credential_secret) == "synthetic-concurrent-token"
        assert not row.enabled and row.priority == 3 and row.revision == 1


@pytest.mark.parametrize("phase", ["exchange", "check"])
async def test_service_cancellation_clears_credentials_and_never_saves(
    sec_db, wire, monkeypatch, phase
):
    from pullbox.schemas.metadata_sources import GcdSignInRequest
    from pullbox.services import gcd_sign_in as service

    submitted = GcdSignInRequest.model_validate(body())

    async def exchange(*args):
        if phase == "exchange":
            raise asyncio.CancelledError
        return SecretStr(TOKEN)

    async def check(*args, **kwargs):
        assert submitted.username.get_secret_value() == ""
        assert submitted.password.get_secret_value() == ""
        raise asyncio.CancelledError

    monkeypatch.setattr(service.gcd_api_v2, "exchange_gcd_token", exchange)
    monkeypatch.setattr(service.MetadataSourceRegistry, "check", check)
    async with sec_db() as session:
        with pytest.raises(asyncio.CancelledError):
            await service.sign_in_gcd(session, submitted, gcd_api_enabled=True)
        assert not session.in_transaction()
        assert submitted.username.get_secret_value() == ""
        assert submitted.password.get_secret_value() == ""
        assert await session.scalar(select(MetadataSourceConfig)) is None
