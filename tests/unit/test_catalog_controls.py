"""Catalog controls use the existing operator queue and expose safe status."""

from unittest.mock import Mock

from pullbox.api.v1.catalog import catalog_status, sync_catalog
from pullbox.services.catalog.service import CatalogStatus


async def test_status_reads_local_service_only(monkeypatch):
    service = Mock()
    service.status.return_value = CatalogStatus(
        phase="current", installed_version="20260913T050000Z"
    )
    monkeypatch.setattr("pullbox.services.catalog.service.get_catalog_service", lambda: service)
    assert (await catalog_status(Mock())).installed_version == "20260913T050000Z"


async def test_download_is_queued_not_awaited_in_request(monkeypatch):
    scheduler = Mock()
    scheduler.run_task_now.return_value = "queued"
    monkeypatch.setattr("pullbox.core.scheduler.get_scheduler", lambda: scheduler)
    assert await sync_catalog(Mock()) == {"status": "queued"}
    scheduler.run_task_now.assert_called_once_with("catalog_update")
