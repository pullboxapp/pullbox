"""Filesystem-specific health check implementation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pullbox.models.health import HealthStatus
from pullbox.models.library import LibraryRoot
from pullbox.services.health_helpers import (
    _STATUS_PRECEDENCE,
    _coerce_pathlike,
    _serialize_sub_check,
)
from pullbox.services.health_types import CheckOutcome, SubCheckOutcome

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SettingsLike = object
PerfCounter = Callable[[], float]
Scandir = Callable[[Path], AbstractContextManager[Iterable[object]]]
Mkstemp = Callable[..., tuple[int, str]]
Close = Callable[[int], None]
Unlink = Callable[[str], None]
FilesystemTargetCheck = Callable[[Path, str, bool], tuple[SubCheckOutcome, str]]


async def check_filesystem(
    session: AsyncSession,
    *,
    settings: SettingsLike | None,
    check_target: FilesystemTargetCheck,
) -> CheckOutcome:
    """Check operational filesystem targets and persist path-level sub-checks."""
    paths: list[tuple[str, Path, bool]] = []

    result = await session.execute(select(LibraryRoot).where(LibraryRoot.enabled.is_(True)))
    roots = list(result.scalars().all())
    for root in roots:
        paths.append(
            (
                f"Library Root: {root.name}",
                Path(root.path),
                bool(root.allow_managed_writes),
            )
        )

    if settings:
        configured_targets = (
            ("Data Directory", getattr(settings, "data_dir", None), Path("/data")),
            ("Logs Directory", getattr(settings, "logs_dir", None), Path("/data/logs")),
            (
                "Backup Directory",
                getattr(settings, "backup_dir", None),
                Path("/data/backups"),
            ),
        )
        for label, raw_path, default_path in configured_targets:
            configured = _coerce_pathlike(raw_path)
            if configured is None:
                continue
            if configured != default_path or configured.exists():
                paths.append((label, configured, True))

    if not paths:
        return CheckOutcome(
            component="filesystem",
            check_name="accessibility",
            status=HealthStatus.UNKNOWN,
            message="Not configured",
            actionable_guidance=("Configure at least one library root in Settings > Library."),
        )

    sub_checks: list[SubCheckOutcome] = []
    guidance_parts: list[str] = []
    worst = HealthStatus.HEALTHY
    inaccessible_or_unwritable = False

    for name, path, require_write in paths:
        sub_check, guidance = await asyncio.to_thread(
            check_target,
            path,
            name,
            require_write,
        )
        sub_checks.append(sub_check)
        if guidance:
            guidance_parts.append(guidance)
        issue = str(sub_check.details.get("issue") or "")
        if issue in {"missing", "unreadable", "unwritable"}:
            inaccessible_or_unwritable = True
        worst = max(worst, sub_check.status, key=lambda s: _STATUS_PRECEDENCE.get(s, 0))

    if inaccessible_or_unwritable:
        msg = "One or more paths are inaccessible or not writable"
    elif any(not require_write for _name, _path, require_write in paths):
        msg = "All paths meet configured access requirements"
    else:
        msg = "All paths accessible and writable"

    return CheckOutcome(
        component="filesystem",
        check_name="accessibility",
        status=worst,
        message=msg,
        details={"checks": [_serialize_sub_check(check) for check in sub_checks]},
        actionable_guidance=" ".join(guidance_parts),
        sub_checks=tuple(sub_checks),
    )


def check_filesystem_target(
    path: Path,
    name: str,
    require_write: bool,
    *,
    perf_counter: PerfCounter,
    scandir: Scandir,
    mkstemp: Mkstemp,
    close: Close,
    unlink: Unlink,
) -> tuple[SubCheckOutcome, str]:
    """Check one operational filesystem target and return a persistable sub-check."""
    started = perf_counter()
    check_name = name
    details: dict[str, Any] = {
        "path": str(path),
        "required_access": "read_write" if require_write else "read",
    }

    if not path.is_dir():
        return (
            SubCheckOutcome(
                check_name=check_name,
                name=name,
                status=HealthStatus.UNHEALTHY,
                message="Missing or not a directory",
                details={**details, "issue": "missing"},
            ),
            f"'{path}' is missing or not a directory. Check mount points and settings.",
        )

    try:
        with scandir(path) as entries:
            next(iter(entries), None)
    except OSError as exc:
        return (
            SubCheckOutcome(
                check_name=check_name,
                name=name,
                status=HealthStatus.UNHEALTHY,
                message=f"Not readable ({exc})",
                details={**details, "issue": "unreadable"},
            ),
            f"'{path}' is not readable. Check filesystem permissions and mount status.",
        )

    if require_write:
        try:
            fd, probe = mkstemp(prefix=".pullbox-health-", dir=path)
            close(fd)
            unlink(probe)
        except OSError as exc:
            return (
                SubCheckOutcome(
                    check_name=check_name,
                    name=name,
                    status=HealthStatus.UNHEALTHY,
                    message=f"Not writable ({exc})",
                    details={**details, "issue": "unwritable"},
                ),
                f"'{path}' is not writable. Check directory ownership and write permissions.",
            )

    elapsed_ms = (perf_counter() - started) * 1000

    return (
        SubCheckOutcome(
            check_name=check_name,
            name=name,
            status=HealthStatus.HEALTHY,
            message="Readable and writable" if require_write else "Readable (reference-only)",
            details={**details, "issue": "ok"},
            response_time_ms=elapsed_ms,
        ),
        "",
    )
