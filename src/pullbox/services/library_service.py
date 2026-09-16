"""Library service — file statistics and comics directory management."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select

from pullbox.models.config import SystemConfig
from pullbox.models.library import FileFormat, LibraryFile, LibraryRoot, MatchConfidence

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


def _normalize_library_path(path: str | Path) -> str:
    """Return a normalized absolute path string for library path comparisons."""
    return str(Path(path).expanduser().resolve(strict=False))


class LibraryService:
    """Read-only library statistics and unmatched file queries."""

    @staticmethod
    async def get_unmatched(
        session: AsyncSession,
        limit: int = 50,
        offset: int = 0,
    ) -> list[LibraryFile]:
        """Get files in the matching queue (unmatched)."""
        result = await session.execute(
            select(LibraryFile)
            .where(LibraryFile.match_confidence == MatchConfidence.UNMATCHED)
            .order_by(LibraryFile.file_name)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_stats(session: AsyncSession) -> dict[str, int]:
        """Get library statistics."""
        total = (await session.execute(select(func.count(LibraryFile.id)))).scalar_one()

        matched = (
            await session.execute(
                select(func.count(LibraryFile.id)).where(
                    LibraryFile.match_confidence != MatchConfidence.UNMATCHED
                )
            )
        ).scalar_one()

        unmatched = total - matched

        # By format
        format_counts = {}
        for fmt in FileFormat:
            count = (
                await session.execute(
                    select(func.count(LibraryFile.id)).where(LibraryFile.file_format == fmt)
                )
            ).scalar_one()
            format_counts[fmt.value] = count

        total_size = (
            await session.execute(select(func.sum(LibraryFile.file_size)))
        ).scalar_one() or 0

        return {
            "total_files": total,
            "matched": matched,
            "unmatched": unmatched,
            "total_size_bytes": total_size,
            **format_counts,
        }


# ── Comics directory helpers ───────────────────────────────────────


async def get_comics_directory(session: AsyncSession) -> Path | None:
    """Get the configured primary comics directory, or None if not set."""
    row = await session.get(SystemConfig, "comics_directory")
    if row and row.value:
        return Path(row.value)
    return None


async def set_comics_directory(session: AsyncSession, path: Path) -> LibraryRoot:
    """Set the legacy primary comics directory through explicit root management.

    This compatibility entry point preserves path identity: an existing root is
    promoted, while a new path creates a separate validated root. It never
    rewrites paths owned by an established root.

    Raises:
        ValueError: If the path does not exist or is not a directory.
    """
    # The comics directory is an operator-managed library root; existence and
    # directory checks below happen before the value is persisted or reused.
    # codeql[py/path-injection]
    path = path.expanduser()
    if not path.exists():
        raise ValueError(f"Path '{path}' does not exist")
    if not path.is_dir():
        raise ValueError(f"Path '{path}' is not a directory")
    path = path.resolve(strict=True)

    from pullbox.core.exceptions import ValidationError
    from pullbox.services.library_root_management import (
        create_library_root,
        update_library_root,
    )

    result = await session.execute(select(LibraryRoot).where(LibraryRoot.path == str(path)))
    root = result.scalar_one_or_none()
    try:
        if root is not None:
            configured_names = {
                name.casefold()
                for name in (
                    await session.scalars(select(LibraryRoot.name).where(LibraryRoot.id != root.id))
                ).all()
            }
            promoted_name = (
                root.name if "comics directory" in configured_names else "Comics Directory"
            )
            await update_library_root(
                session,
                root.id,
                {
                    "name": promoted_name,
                    "enabled": True,
                    "allow_referenced_registrations": True,
                    "allow_managed_writes": True,
                    "is_default_managed_destination": True,
                },
            )
            return root

        configured_names = {
            name.casefold() for name in (await session.scalars(select(LibraryRoot.name))).all()
        }
        name = "Comics Directory"
        suffix = 2
        while name.casefold() in configured_names:
            name = f"Comics Directory {suffix}"
            suffix += 1
        created = await create_library_root(
            session,
            name=name,
            path=str(path),
            allow_referenced_registrations=True,
            allow_managed_writes=True,
            is_default_managed_destination=True,
        )
    except ValidationError as exc:
        raise ValueError(exc.message) from exc

    created_root = await session.get(LibraryRoot, int(created["id"]))
    if created_root is None:  # pragma: no cover - guarded by the flush above
        raise RuntimeError("Created library root could not be reloaded.")
    return created_root


async def reconcile_runtime_library_paths(
    session: AsyncSession,
    runtime_root: Path,
) -> dict[str, bool | int | str] | None:
    """Bootstrap a fresh runtime root without rebinding established paths.

    A changed container path is not proof that it represents the same physical
    library. Established roots and tracked paths therefore remain untouched
    until an operator completes an explicit rebind workflow.
    """
    runtime_root_str = _normalize_library_path(runtime_root)
    config_row = await session.get(SystemConfig, "comics_directory")
    roots = list((await session.execute(select(LibraryRoot).order_by(LibraryRoot.id))).scalars())
    stored_root_str = (
        _normalize_library_path(config_row.value)
        if config_row is not None and config_row.value.strip()
        else ""
    )

    if not stored_root_str and not roots:
        if config_row is None:
            config_row = SystemConfig(
                key="comics_directory",
                value=runtime_root_str,
                value_type="string",
            )
            session.add(config_row)
        else:
            config_row.value = runtime_root_str

        root = LibraryRoot(
            name="Comics Directory",
            path=runtime_root_str,
            enabled=True,
            allow_referenced_registrations=True,
            allow_managed_writes=True,
            is_default_managed_destination=True,
        )
        session.add(root)

        await session.flush()
        return {
            "status": "bootstrapped",
            "old_root": "",
            "new_root": runtime_root_str,
            "series_updated": 0,
            "library_files_updated": 0,
            "rebind_required": False,
        }

    runtime_record = next(
        (root for root in roots if _normalize_library_path(root.path) == runtime_root_str),
        None,
    )

    if stored_root_str == runtime_root_str:
        if runtime_record is None:
            runtime_record = LibraryRoot(
                name="Comics Directory",
                path=runtime_root_str,
                enabled=True,
                allow_referenced_registrations=True,
                allow_managed_writes=True,
                is_default_managed_destination=not any(
                    root.is_default_managed_destination for root in roots
                ),
            )
            session.add(runtime_record)
            await session.flush()
            return {
                "status": "bootstrapped",
                "old_root": stored_root_str,
                "new_root": runtime_root_str,
                "series_updated": 0,
                "library_files_updated": 0,
                "rebind_required": False,
            }
        if not runtime_record.enabled:
            return {
                "status": "root_unavailable",
                "old_root": stored_root_str,
                "new_root": runtime_root_str,
                "series_updated": 0,
                "library_files_updated": 0,
                "rebind_required": False,
            }
        return None

    return {
        "status": "rebind_required",
        "old_root": stored_root_str,
        "new_root": runtime_root_str,
        "series_updated": 0,
        "library_files_updated": 0,
        "rebind_required": True,
    }
