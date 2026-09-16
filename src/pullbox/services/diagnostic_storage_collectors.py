"""Storage and permission collectors for diagnostic packages."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from pullbox.models.config import SystemConfig

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def collect_disk_and_permissions(session: AsyncSession) -> dict[str, object]:
    """Collect disk space and directory permission info for key directories."""
    from pullbox.config import get_settings

    settings = get_settings()

    dirs: dict[str, Path] = {
        "library_root": settings.library_root,
        "data_dir": settings.data_dir,
        "covers_dir": settings.covers_dir,
        "logs_dir": settings.logs_dir,
        "temp_dir": settings.temp_dir,
    }

    config_key_map = {
        "comics_dir": "library_root",
        "covers_dir": "covers_dir",
        "logs_dir": "logs_dir",
    }
    try:
        result = await session.execute(
            select(SystemConfig.key, SystemConfig.value).where(
                SystemConfig.key.in_(config_key_map.keys())
            )
        )
        for key, value in result.all():
            if value:
                dirs[config_key_map[key]] = Path(value)
    except Exception:
        pass

    try:
        db_url = settings.db_url
        if ":///" in db_url:
            db_path = Path(db_url.split(":///", 1)[1])
            dirs["database_dir"] = db_path.parent
    except Exception:
        pass

    return await asyncio.to_thread(_probe_directories, dirs)


def _probe_directories(dirs: dict[str, Path]) -> dict[str, object]:
    """Probe roots only; diagnostics must never enumerate the user's collection."""
    output: dict[str, object] = {}
    for name, path in dirs.items():
        info: dict[str, object] = {"path": str(path)}
        info["exists"] = path.exists()
        info["is_dir"] = path.is_dir() if path.exists() else False
        info["writable"] = os.access(path, os.W_OK) if path.exists() else False

        disk_target = path if path.exists() else path.parent
        try:
            while not disk_target.exists() and disk_target != disk_target.parent:
                disk_target = disk_target.parent
            if disk_target.exists():
                usage = shutil.disk_usage(disk_target)
                info["disk_total_bytes"] = usage.total
                info["disk_used_bytes"] = usage.used
                info["disk_free_bytes"] = usage.free
                pct = round(usage.free / usage.total * 100, 1) if usage.total else 0
                info["disk_free_pct"] = pct
        except OSError:
            pass

        info["dir_size_bytes"] = None
        info["dir_size_status"] = "not_collected"
        info["dir_size_reason"] = (
            "Recursive directory sizing is omitted to keep diagnostics independent of library size."
        )

        output[name] = info

    return output
