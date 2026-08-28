"""Download monitor write-phase characterization tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pullbox.models.download import DownloadHistory, DownloadState
from pullbox.models.issue import Issue, IssueStatus


class _FakeSession:
    def __init__(self) -> None:
        self.download = SimpleNamespace(
            id=1,
            issue_id=2,
            state=DownloadState.DOWNLOADING,
            error_message=None,
            downloaded_path="/downloads/book.cbz",
        )
        self.issue = SimpleNamespace(status=IssueStatus.DOWNLOADING)

    async def get(self, model, key):
        if model is DownloadHistory and key == 1:
            return self.download
        if model is Issue and key == 2:
            return self.issue
        return None


class _FakeLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.events.append((event, kwargs))


@pytest.mark.asyncio
async def test_apply_monitor_updates_handles_removed_externally() -> None:
    """Removed-external updates should fail the download and restore the issue."""
    from pullbox.tasks import download_monitor_apply

    session = _FakeSession()
    cleared: list[int] = []
    summaries: list[tuple[object, dict[str, object]]] = []
    projected: list[tuple[object, object | None]] = []

    async def publish_progress(_session, download, progress) -> None:  # type: ignore[no-untyped-def]
        projected.append((download, progress))

    result = await download_monitor_apply.apply_monitor_updates(
        session,
        [
            {
                "id": 1,
                "removed_externally": True,
                "error_message": "Download was removed from the client externally",
                "issue_id": 2,
            }
        ],
        first_active_observed_at={1: 123.0},
        clear_progress=cleared.append,
        handle_download_failure=None,
        emit_download_lifecycle_summary=lambda download, **payload: summaries.append(
            (download, payload)
        ),
        event_logger=_FakeLogger(),
        publish_progress=publish_progress,
    )

    assert result.completed == 0
    assert result.failed == 1
    assert session.download.state == DownloadState.FAILED
    assert session.download.error_message == "Download was removed from the client externally"
    assert session.issue.status == IssueStatus.WANTED
    assert cleared == [1]
    assert summaries[0][1]["outcome"] == "removed_externally"
    assert projected == [(session.download, None)]
