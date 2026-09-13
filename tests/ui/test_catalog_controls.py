"""Catalog settings and controls exercise the real authentication middleware."""

import os
import sys
from unittest.mock import Mock

import pytest

from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService
from tests.unit.test_catalog_reader import installed_reader
from tests.unit.test_catalog_service import Server

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest_plugins = ["conftest_security"]


@pytest.fixture
def local_catalog(tmp_path, monkeypatch):
    service = Server(tmp_path).service(tmp_path / "local")
    monkeypatch.setattr("pullbox.services.catalog.service.get_catalog_service", lambda: service)
    return service


async def test_catalog_status_is_authenticated(unauthenticated_client, sec_user, local_catalog):
    response = await unauthenticated_client.get("/api/v1/catalog")
    assert response.status_code == 401


async def test_catalog_writes_require_csrf(authenticated_client, local_catalog):
    response = await authenticated_client.post("/api/v1/catalog/sync")
    assert response.status_code == 403
    response = await authenticated_client.patch(
        "/api/v1/catalog/preferences", json={"automatic_updates": False}
    )
    assert response.status_code == 403
    assert local_catalog.status().requested is False


async def test_operator_can_queue_and_save_preference(
    authenticated_client, local_catalog, monkeypatch
):
    scheduler = Mock()
    scheduler.run_task_now.return_value = "queued"
    monkeypatch.setattr("pullbox.core.scheduler.get_scheduler", lambda: scheduler)
    token = authenticated_client.cookies.get(SESSION_COOKIE_NAME)
    headers = {"X-CSRF-Token": AuthService.get_csrf_token_from_session(token)}
    response = await authenticated_client.post("/api/v1/catalog/sync", headers=headers)
    assert response.status_code == 202
    scheduler.run_task_now.assert_called_once_with("catalog_update")
    response = await authenticated_client.patch(
        "/api/v1/catalog/preferences", headers=headers, json={"automatic_updates": False}
    )
    assert response.status_code == 200
    assert local_catalog.status().automatic_updates is False
    assert local_catalog.status().requested is False


async def test_metadata_settings_explains_local_storage_and_progress(authenticated_client):
    response = await authenticated_client.get("/settings?tab=metadata")
    assert response.status_code == 200
    assert 'id="catalog-download-progress"' in response.text
    assert 'role="status" aria-live="polite"' in response.text
    assert "window.pullboxLiveUpdatesEnabled()" in response.text
    assert "Download catalog" in response.text
    assert "A download never moves or imports comics." in response.text


async def test_add_series_labels_local_search_without_api_key(
    authenticated_client, tmp_path, monkeypatch
):
    reader = installed_reader(tmp_path)
    monkeypatch.setattr("pullbox.services.catalog.reader.get_catalog_reader", lambda: reader)
    response = await authenticated_client.get("/series/add?q=Batman")
    assert response.status_code == 200
    assert "Search local catalog" in response.text
    assert "Batman" in response.text
    assert "Check your API key" not in response.text


async def test_api_keys_cannot_start_catalog_download(
    unauthenticated_client, sec_api_key, local_catalog
):
    readable = await unauthenticated_client.get(
        "/api/v1/catalog", headers={"X-Api-Key": sec_api_key}
    )
    assert readable.status_code == 200
    response = await unauthenticated_client.post(
        "/api/v1/catalog/sync", headers={"X-Api-Key": sec_api_key}
    )
    assert response.status_code == 401
