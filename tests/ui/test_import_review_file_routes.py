"""Review file previews are read-only, scoped, and CSRF protected."""

import re
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pullbox.api.middleware import SESSION_COOKIE_NAME
from pullbox.models.import_job import ImportedFile, ImportedFileStatus
from pullbox.providers.base import IssueSummary
from pullbox.services.auth_service import AuthService
from tests.ui.test_import_collection_shell_ui_routes import _seed_import_review_job

pytest_plugins = ["conftest_security"]


def csrf(client):
    return {
        "x-csrf-token": AuthService.get_csrf_token_from_session(
            client.cookies.get(SESSION_COOKIE_NAME)
        )
        or ""
    }


def token(html):
    return unescape(re.search(r'name="token" value="([^"]+)"', html).group(1))


async def test_assignment_preview_and_apply_are_file_scoped(
    authenticated_client, sec_db, monkeypatch
):
    from pullbox.ui import import_review_file_routes as routes

    metadata = AsyncMock()
    metadata.get_series_metadata.return_value = SimpleNamespace(
        provider_id="20",
        title="Second",
        year_start=2020,
        publisher="Test",
        issue_count=1,
        comicvine_url=None,
    )
    metadata.get_issue_summaries_for_series.return_value = [
        IssueSummary("201", 1, "Second", "2020-01-01", None, "issue")
    ]
    monkeypatch.setattr(routes, "build_metadata_service", AsyncMock(return_value=metadata))
    job_id = await _seed_import_review_job(sec_db)
    response = await authenticated_client.get(f"/import/{job_id}/files/1/assign?cv_id=20")
    assert response.status_code == 200
    assert "dropdown-select-contract" in response.text
    assert "Only this file" in response.text
    async with sec_db() as session:
        assert (await session.get(ImportedFile, 1)).import_series_id == 1
    data = {"token": token(response.text), "cv_id": 20, "issue_cv_id": 201}
    rejected = await authenticated_client.post(
        f"/import/{job_id}/files/2/assign", data=data, headers=csrf(authenticated_client)
    )
    assert rejected.status_code == 422
    response = await authenticated_client.post(
        f"/import/{job_id}/files/1/assign", data=data, headers=csrf(authenticated_client)
    )
    assert response.status_code == 200, response.text
    async with sec_db() as session:
        assert (await session.get(ImportedFile, 1)).import_series_id != 1
        assert (await session.get(ImportedFile, 2)).import_series_id == 1


async def test_source_preview_does_not_scan_and_post_only_queues(
    authenticated_client, sec_db, monkeypatch
):
    from pullbox.services import import_review_source_actions as actions
    from pullbox.tasks import import_task

    inspect = Mock(side_effect=AssertionError("Preview and enqueue must not scan"))
    monkeypatch.setattr(actions, "inspect_review_source", inspect)
    trigger = Mock()
    monkeypatch.setattr(import_task, "trigger_import_review_source_action", trigger)
    job_id = await _seed_import_review_job(sec_db)
    async with sec_db() as session:
        file = await session.get(ImportedFile, 1)
        file.status = ImportedFileStatus.SAFETY_BLOCKED
        file.diagnostics = {
            "safety_block": {"category": "archive_inspection_failed", "overrideable": False}
        }
        await session.commit()
    preview = await authenticated_client.get(f"/import/{job_id}/files/1/source")
    assert preview.status_code == 200
    assert "This does not grant a safety exception" in preview.text
    data = {"token": token(preview.text), "source_action": "recheck"}
    rejected = await authenticated_client.post(f"/import/{job_id}/files/1/source", data=data)
    assert rejected.status_code == 403
    result = await authenticated_client.post(
        f"/import/{job_id}/files/1/source", data=data, headers=csrf(authenticated_client)
    )
    assert result.status_code == 200, result.text
    trigger.assert_called_once_with(job_id, 1)
    inspect.assert_not_called()
    async with sec_db() as session:
        file = await session.get(ImportedFile, 1)
        assert file.status is ImportedFileStatus.SAFETY_BLOCKED
        assert file.diagnostics["review_source_action"]["state"] == "pending"


async def test_inline_issue_choices_are_bounded_and_use_the_same_scoped_assignment(
    authenticated_client, sec_db, monkeypatch
):
    from pullbox.ui import import_review_file_routes as routes

    metadata = AsyncMock()
    metadata.get_series_metadata.return_value = SimpleNamespace(
        provider_id="20", title="Second", year_start=2020
    )
    metadata.get_issue_summaries_for_series.return_value = [
        IssueSummary(str(200 + i), i, f"Issue {i}", "2020-01-01", None, "issue")
        for i in range(1, 32)
    ]
    monkeypatch.setattr(routes, "build_metadata_service", AsyncMock(return_value=metadata))
    job_id = await _seed_import_review_job(sec_db)
    url = f"/import/{job_id}/files/1/assign?cv_id=20&inline=true"
    response = await authenticated_client.get(url)
    assert response.status_code == 200
    assert 'data-testid="import-review-issue-choices"' in response.text
    assert response.text.count('data-testid="import-review-use-issue"') == 25
    assert 'role="dialog"' not in response.text
    assert "issue_page=2" in response.text
    second = await authenticated_client.get(url + "&issue_page=2")
    assert second.text.count('data-testid="import-review-use-issue"') == 6
    filtered = await authenticated_client.get(url + "&q=Issue%2031")
    assert filtered.text.count('data-testid="import-review-use-issue"') == 1
    assert 'value="231"' in filtered.text
    rejected = await authenticated_client.post(
        f"/import/{job_id}/files/2/assign",
        data={"token": token(filtered.text), "cv_id": 20, "issue_cv_id": 231},
        headers=csrf(authenticated_client),
    )
    assert rejected.status_code == 422
