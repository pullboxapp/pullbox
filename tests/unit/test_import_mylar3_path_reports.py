"""Private pre-job reports remain bounded, readable, and safe to export."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from pullbox.schemas.import_mylar3_path_preflight import (
    MylarPathException,
    MylarPathPreviewResponse,
    MylarPathResolutionCounts,
)
from pullbox.services import import_mylar3_path_reports as reports

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def report_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reports, "_directory", lambda: tmp_path / "reports")


def preview() -> MylarPathPreviewResponse:
    return MylarPathPreviewResponse(
        resolution=MylarPathResolutionCounts(locations=2, identity_resolved=1, missing=1),
        requires_confirmation=False,
        can_confirm=False,
        exception_count=1,
        exceptions=[
            MylarPathException(
                series_id="1",
                series_name="Missing series",
                stored_path="/comics/Missing",
                attempted_path="/comics/Missing",
                outcome="missing",
                reason="Missing folder",
                suggested_action="Check the source path",
            )
        ],
    )


def test_reports_retain_only_five_private_files() -> None:
    for _ in range(7):
        reports.save_report(preview(), "/imports/mylar.db")
    paths = list(reports._directory().glob("*.json"))
    assert len(paths) == 5
    assert all(path.stat().st_mode & 0o077 == 0 for path in paths)
    assert reports.latest_report()["exceptions"][0]["stored_path"] == "/comics/Missing"
    assert not list(reports._directory().glob("*.tmp"))


def test_expired_and_symlink_reports_are_not_served(tmp_path: Path) -> None:
    report_id = reports.save_report(preview(), "/imports/mylar.db")
    path = reports._directory() / f"{report_id}.json"
    os.utime(path, (0, 0))
    with pytest.raises(FileNotFoundError):
        reports.load_report(report_id)
    assert reports.latest_report()["status"] == "not_available"
    path.unlink()
    target = tmp_path / "private.json"
    target.write_text("{}")
    path.symlink_to(target)
    with pytest.raises(FileNotFoundError):
        reports.load_report(report_id)


@pytest.mark.parametrize("payload", [[], {}, {"exceptions": None}, {"exceptions": ["bad"]}])
def test_corrupt_reports_fail_closed(payload: object) -> None:
    report_id = reports.save_report(preview(), "/imports/mylar.db")
    if isinstance(payload, dict):
        payload["report_id"] = report_id
    (reports._directory() / f"{report_id}.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        reports.load_report(report_id)
    assert reports.latest_report()["status"] == "not_available"


def test_size_limit_leaves_no_partial_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reports, "_MAX_REPORT_BYTES", 1)
    with pytest.raises(ValueError, match="size limit"):
        reports.save_report(preview(), "/imports/mylar.db")
    assert not reports._directory().exists()


def test_page_clamps_and_filters_without_rescanning() -> None:
    report_id = reports.save_report(preview(), "/imports/mylar.db")
    page = reports.report_page(report_id, 500, "MISSING SERIES")
    assert page["page"] == 1
    assert page["total"] == 1
    assert page["items"][0]["series_id"] == "1"
    assert reports.report_page(report_id, 1, "No match")["items"] == []
