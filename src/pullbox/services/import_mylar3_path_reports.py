"""Bounded, private preflight reports, independent of import-job creation."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pullbox.schemas.import_mylar3_path_preflight import MylarPathPreviewResponse

PAGE_SIZE = 25
_MAX_REPORT_BYTES = 32 * 1024 * 1024
_MAX_REPORTS = 5
_MAX_AGE_SECONDS = 24 * 60 * 60
_REPORT_ID = re.compile(r"[a-f0-9]{32}")


def _directory() -> Path:
    from pullbox.config import get_settings

    return get_settings().data_dir / "diagnostics" / "mylar-preflight"


def save_report(preview: MylarPathPreviewResponse, source_path: str) -> str:
    """Atomically retain recent reports, never opening the source database for writing."""
    report_id = uuid4().hex
    report = preview.model_dump(mode="json")
    report.update(
        report_id=report_id, source_path=source_path, captured_at=datetime.now(UTC).isoformat()
    )
    content = json.dumps(report, ensure_ascii=True).encode("utf-8")
    if len(content) > _MAX_REPORT_BYTES:
        raise ValueError("Mylar path report exceeds the diagnostic size limit")
    directory = _directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
        os.replace(temporary, directory / f"{report_id}.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    reports = _report_paths()
    for old in reports[_MAX_REPORTS:]:
        old.unlink(missing_ok=True)
    return report_id


def _report_paths() -> list[Path]:
    return sorted(
        (
            path
            for path in _directory().glob("*.json")
            if _REPORT_ID.fullmatch(path.stem) and not path.is_symlink()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def load_report(report_id: str) -> dict[str, Any]:
    """Load only an application-generated report, never a caller-supplied path."""
    if not _REPORT_ID.fullmatch(report_id):
        raise FileNotFoundError("Invalid preflight report")
    path = _directory() / f"{report_id}.json"
    if path.is_symlink():
        raise FileNotFoundError("Invalid preflight report")
    stat = path.stat()
    if stat.st_size > _MAX_REPORT_BYTES or time.time() - stat.st_mtime > _MAX_AGE_SECONDS:
        raise FileNotFoundError("Preflight report expired")
    with path.open("rb") as source:
        content = source.read(_MAX_REPORT_BYTES + 1)
    if len(content) > _MAX_REPORT_BYTES:
        raise ValueError("Preflight report is too large")
    report: dict[str, Any] = json.loads(content)
    if not isinstance(report, dict) or report.get("report_id") != report_id:
        raise ValueError("Preflight report identity changed")
    if not isinstance(report.get("exceptions"), list):
        raise ValueError("Preflight report exceptions are invalid")
    MylarPathPreviewResponse.model_validate(report)
    return report


def report_page(report_id: str, page: int, search: str) -> dict[str, Any]:
    report = load_report(report_id)
    query = search.strip().casefold()
    items = report["exceptions"]
    if query:
        items = [
            item for item in items if any(query in str(value).casefold() for value in item.values())
        ]
    total = len(items)
    page = min(page, max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))
    return {
        "items": items[(page - 1) * PAGE_SIZE : page * PAGE_SIZE],
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
    }


def latest_report() -> dict[str, Any]:
    """Include the most recent pre-job evidence in an operator-requested diagnostic ZIP."""
    try:
        for path in _report_paths():
            try:
                return load_report(path.stem)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return {
        "status": "not_available",
        "message": "No recent Mylar path preflight report is available.",
    }
