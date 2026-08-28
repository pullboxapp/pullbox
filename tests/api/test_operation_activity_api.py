"""API coverage for the shared global activity surface."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pullbox.models.operation_progress import (
    OperationProgressState,
    OperationProgressType,
)
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService
from pullbox.services.operation_progress import (
    OperationProgressUpdate,
    publish_operation_progress,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _csrf_header_for(authenticated_client) -> dict[str, str]:  # type: ignore[no-untyped-def]
    session_token = authenticated_client.cookies.get(SESSION_COOKIE_NAME)
    assert session_token is not None
    csrf_token = AuthService.get_csrf_token_from_session(session_token)
    assert csrf_token is not None
    return {"X-CSRF-Token": csrf_token}


async def _seed_operation(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    async with factory() as session:
        result = await publish_operation_progress(
            session,
            OperationProgressUpdate(
                operation_type=OperationProgressType.DOWNLOAD,
                operation_key="17",
                revision=4,
                state=OperationProgressState.RUNNING,
                phase="downloading",
                title="Batman 001",
                message="Downloading from NZBGet",
                detail_url="/downloads",
                event_at=datetime(2026, 8, 27, 12, 0, tzinfo=UTC),
            ),
        )
        await session.commit()
        return result.operation.id


async def test_activity_api_requires_authentication(unauthenticated_client) -> None:  # type: ignore[no-untyped-def]
    response = await unauthenticated_client.get("/api/v1/activity")

    assert response.status_code == 401


async def test_activity_api_returns_shared_progress_contract(
    authenticated_client,  # type: ignore[no-untyped-def]
    sec_db: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_operation(sec_db)

    response = await authenticated_client.get("/api/v1/activity")

    assert response.status_code == 200
    data = response.json()
    assert data["active_count"] == 1
    assert data["spinner_count"] == 1
    assert data["attention_count"] == 0
    assert data["operations"][0]["operation_type"] == "download"
    assert data["operations"][0]["operation_key"] == "17"
    assert data["operations"][0]["overall"]["indeterminate"] is True
    assert data["operations"][0]["item"] is None


async def test_activity_api_acknowledges_attention_item(
    authenticated_client,  # type: ignore[no-untyped-def]
    sec_db: async_sessionmaker[AsyncSession],
) -> None:
    operation_id = await _seed_operation(sec_db)

    response = await authenticated_client.post(
        f"/api/v1/activity/{operation_id}/acknowledge",
        headers=_csrf_header_for(authenticated_client),
    )

    assert response.status_code == 200
    assert response.json()["acknowledged"] is True
