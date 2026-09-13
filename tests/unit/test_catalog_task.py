"""Catalog scheduling respects explicit first-download consent."""

from unittest.mock import AsyncMock

from pullbox.services.catalog import service
from pullbox.tasks.catalog_task import update_catalog


async def test_task_delegates_automatic_policy(monkeypatch):
    catalog = AsyncMock()
    monkeypatch.setattr(service, "get_catalog_service", lambda: catalog)
    await update_catalog()
    catalog.sync.assert_awaited_once_with(manual=False)
