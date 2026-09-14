"""Bounded cleanup of catalog-owned artifacts, never of library data."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from pullbox.services.catalog.database import safe_path
from pullbox.services.catalog.storage import load_json

if TYPE_CHECKING:
    from pathlib import Path


def cleanup(root: Path, *, completed: bool = False) -> None:
    """Caller holds the update lock; keep active, previous and their weekly bases.

    Old generations get a two-day grace period so in-flight readers can finish.
    Only fixed-format owned files are removed; unknown files are left alone.
    """
    keep = set()
    for pointer in ("active.json", "previous.json"):
        ref = load_json(root / pointer)
        keep.add(str(ref.get("path", "")))
        keep.add(f"bases/{ref.get('base_version', '')}.db")
    for directory, pattern in (
        ("staging", r"catalog-[A-Za-z0-9_-]+(?:\.patch)?\.db(?:-journal)?"),
        ("downloads", r"[a-f0-9]{64}\.part"),
        ("bases", r"\d{8}T\d{6}Z\.db"),
        ("versions", r"\d{8}T\d{6}Z\.db"),
    ):
        folder = safe_path(root / directory)
        if not folder.exists():
            continue
        for path in folder.iterdir():
            safe_path(path)
            if not path.is_file() or not re.fullmatch(pattern, path.name):
                continue
            relative = str(path.relative_to(root))
            expired = path.stat().st_mtime < time.time() - 2 * 86400
            if (
                directory == "staging"
                or (directory == "downloads" and completed)
                or (expired and relative not in keep)
            ):
                path.unlink()
