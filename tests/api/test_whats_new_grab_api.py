"""What's New selected acquisition delegates to the existing issue endpoints."""
# ruff: noqa: F811 - owning fixtures are intentionally imported.

from unittest.mock import AsyncMock

import pytest

from pullbox.api.v1 import issues as issue_api
from pullbox.schemas.search import GrabReleaseResponse
from tests.api.test_metadata_sources_api import csrf
from tests.integration.metadata_identity.test_whats_new_grab import prepare_selection
from tests.ui.test_whats_new_issue_state import actions_enabled  # noqa: F401

pytest_plugins = ["conftest_security"]


@pytest.fixture
async def selected_release(sec_db, tmp_path, actions_enabled):
    async with sec_db.begin() as session:
        *_, selected = await prepare_selection(session, tmp_path)
    return selected


async def test_selected_grab_reuses_existing_endpoint(
    authenticated_client, selected_release, monkeypatch
):
    called = AsyncMock(
        return_value=GrabReleaseResponse(
            issue_id=selected_release.issue_id, download_id=1, title="Selected", status="queued"
        )
    )
    monkeypatch.setattr(issue_api, "grab_release", called)
    response = await authenticated_client.post(
        "/api/v1/whats-new/grab",
        json={
            "selection": selected_release.model_dump(),
            "result": {
                "download_url": "https://example.test/selected",
                "title": "Selected",
                "indexer_name": "Test",
            },
        },
        headers=csrf(authenticated_client),
    )
    assert response.status_code == 201, response.text
    assert called.await_count == 1
    assert called.call_args.kwargs["issue_id"] == selected_release.issue_id


async def test_stale_grab_never_calls_existing_acquisition(
    authenticated_client, selected_release, monkeypatch
):
    called = AsyncMock()
    monkeypatch.setattr(issue_api, "grab_release", called)
    selected_release.fingerprint = "0" * 64
    response = await authenticated_client.post(
        "/api/v1/whats-new/grab",
        json={
            "selection": selected_release.model_dump(),
            "result": {
                "download_url": "https://example.test/selected",
                "title": "Selected",
                "indexer_name": "Test",
            },
        },
        headers=csrf(authenticated_client),
    )
    assert response.status_code == 409, response.text
    called.assert_not_awaited()


@pytest.mark.parametrize("kind", ["direct", "dc"])
async def test_direct_transports_reuse_existing_guards(
    authenticated_client, selected_release, monkeypatch, kind
):
    from pullbox.schemas.search import DcGrabResponse, DirectGrabResponse

    response_type = DirectGrabResponse if kind == "direct" else DcGrabResponse
    response_data = {
        "issue_id": selected_release.issue_id,
        "acquisition_id": 1,
        "title": "Selected",
        "status": "queued",
    }
    response_data.update(
        {"artifact_id": 2} if kind == "direct" else {"download_id": 2, "bundle_id": 3}
    )
    called = AsyncMock(return_value=response_type(**response_data))
    method = "grab_direct_release" if kind == "direct" else "grab_direct_connect_release"
    monkeypatch.setattr(issue_api, method, called)
    result = {"direct_attempt_id": 1} if kind == "direct" else {"dc_route_token": "t" * 40}
    response = await authenticated_client.post(
        "/api/v1/whats-new/grab",
        json={"selection": selected_release.model_dump(), "result": result},
        headers=csrf(authenticated_client),
    )
    assert response.status_code == 201, response.text
    assert called.await_count == 1
    assert called.call_args.kwargs["issue_id"] == selected_release.issue_id


async def test_concurrent_click_does_not_send_second_acquisition(
    authenticated_client, selected_release, monkeypatch
):
    import asyncio

    started, finish = asyncio.Event(), asyncio.Event()

    async def grab(**_kwargs):
        started.set()
        await finish.wait()
        return GrabReleaseResponse(
            issue_id=selected_release.issue_id, download_id=1, title="Selected", status="queued"
        )

    called = AsyncMock(side_effect=grab)
    monkeypatch.setattr(issue_api, "grab_release", called)
    body = {
        "selection": selected_release.model_dump(),
        "result": {
            "download_url": "https://example.test/selected",
            "title": "Selected",
            "indexer_name": "Test",
        },
    }
    pending = asyncio.create_task(
        authenticated_client.post(
            "/api/v1/whats-new/grab", json=body, headers=csrf(authenticated_client)
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        again = await authenticated_client.post(
            "/api/v1/whats-new/grab", json=body, headers=csrf(authenticated_client)
        )
        assert again.status_code == 409, again.text
    finally:
        finish.set()
        first = await pending
    assert first.status_code == 201
    called.assert_awaited_once()


async def test_grab_requires_auth_and_csrf(
    authenticated_client, unauthenticated_client, selected_release, monkeypatch
):
    called = AsyncMock()
    monkeypatch.setattr(issue_api, "grab_release", called)
    body = {
        "selection": selected_release.model_dump(),
        "result": {
            "download_url": "https://example.test/selected",
            "title": "Selected",
            "indexer_name": "Test",
        },
    }
    denied = await unauthenticated_client.post("/api/v1/whats-new/grab", json=body)
    assert denied.status_code in {401, 403}
    denied = await authenticated_client.post("/api/v1/whats-new/grab", json=body)
    assert denied.status_code == 403
    called.assert_not_awaited()
