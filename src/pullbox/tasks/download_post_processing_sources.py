"""Source discovery and path mapping helpers for download post-processing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.download import DownloadHistory

logger = structlog.get_logger(__name__)

_POST_PROCESSING_SOURCE_RETRY_DELAYS = (0.0, 0.5, 1.0, 2.0, 4.0)


def _is_windows_origin_path(path: str) -> bool:
    """Identify configured paths using Windows drive or UNC syntax."""
    windows_path = PureWindowsPath(path)
    return bool(windows_path.drive)


def _map_configured_download_path(
    raw_path: str,
    *,
    remote_root: str,
    local_root: str,
) -> str | None:
    """Map a client path by path components without weakening POSIX semantics."""
    windows_origin = _is_windows_origin_path(remote_root)
    path_type = PureWindowsPath if windows_origin else PurePosixPath
    candidate = path_type(raw_path)
    configured_remote = path_type(remote_root)

    try:
        remainder = candidate.relative_to(configured_remote)
    except ValueError:
        return None

    if ".." in remainder.parts:
        raise FileNotFoundError(
            "The download client reported an unsafe parent traversal outside "
            "the configured Remote Path."
        )

    local_base = Path(local_root)
    mapped = local_base.joinpath(*remainder.parts)
    resolved_base = local_base.expanduser().resolve(strict=False)
    resolved_mapped = mapped.expanduser().resolve(strict=False)
    if resolved_mapped != resolved_base and not resolved_mapped.is_relative_to(resolved_base):
        raise FileNotFoundError(
            "The mapped download path resolves outside the configured Download Directory."
        )

    return str(mapped)


def _find_comic_file(
    download_path: Path,
    allowed_extensions: set[str] | None = None,
) -> Path | None:
    """Find the first comic book file in a download path."""
    from pullbox.core.file_safety import DEFAULT_ALLOWED_EXTENSIONS

    exts = allowed_extensions or DEFAULT_ALLOWED_EXTENSIONS

    if download_path.is_file():
        if download_path.suffix.lower() in exts:
            return download_path
        return None

    if download_path.is_dir():
        # Sort to get deterministic results; prefer files in the root.
        try:
            for path in sorted(download_path.rglob("*")):
                try:
                    if path.is_file() and path.suffix.lower() in exts:
                        return path
                except PermissionError:
                    # SMB/NFS cleanup tombstones such as ``.smbdelete*`` can
                    # briefly exist in the directory listing but reject stat().
                    continue
        except NotADirectoryError:
            # Network mounts (NFS/SMB) can raise ENOTDIR when rglob tries to
            # recurse into a file with stale directory metadata. Fall back to a
            # non-recursive listing which avoids the issue.
            logger.warning(
                "find_comic_file_rglob_enotdir",
                path=str(download_path),
            )
            try:
                for path in sorted(download_path.iterdir()):
                    try:
                        if path.is_file() and path.suffix.lower() in exts:
                            return path
                    except PermissionError:
                        continue
            except OSError:
                pass

    return None


def _reject_filesystem_root_probe(probe_root: Path) -> None:
    """Fail before recursive discovery can inspect the container filesystem root."""
    if probe_root.expanduser().resolve(strict=False) != Path("/"):
        return

    logger.error(
        "post_processing_root_probe_rejected",
        probe_root=str(probe_root),
        hint="Verify Remote Path and Download Directory before retrying post-processing.",
    )
    raise RuntimeError(
        "Refusing to inspect the container filesystem root during post-processing. "
        "Verify the download client's Remote Path and Download Directory."
    )


@dataclass(frozen=True)
class PostProcessingSourceProbe:
    """Resolved source visibility for post-processing on local/network storage."""

    comic_file: Path | None
    probe_root: Path
    source_seen: bool
    attempts: int


def _build_post_processing_integrity_exception(
    comic_file: Path,
    errors: list[str],
) -> Exception:
    """Classify integrity failures as corrupt release vs transient file access."""
    primary_error = errors[0] if errors else "Quick integrity check failed"
    lowered = primary_error.lower()

    if lowered.startswith("file not found:") or "[errno 2]" in lowered:
        return FileNotFoundError(
            f"Source file became unreadable during the quick integrity check: {comic_file}"
        )

    return RuntimeError(
        f"Release failed quick integrity check: {primary_error}. Try another release."
    )


async def _probe_post_processing_source(
    source_path: Path,
    allowed_extensions: set[str],
    *,
    find_comic_file: Callable[[Path, set[str]], Path | None] | None = None,
) -> PostProcessingSourceProbe:
    """Retry source discovery briefly to smooth over shared-storage visibility lag."""
    from asyncio import get_running_loop

    source_seen = False
    last_probe_root = source_path
    attempts = len(_POST_PROCESSING_SOURCE_RETRY_DELAYS)
    finder = find_comic_file or _find_comic_file

    for attempt, delay_seconds in enumerate(_POST_PROCESSING_SOURCE_RETRY_DELAYS, start=1):
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        source_exists = await get_running_loop().run_in_executor(None, source_path.exists)
        probe_root = source_path

        # Some clients report a specific file path before that file is fully
        # visible on a network mount. If the parent folder is present, scan the
        # folder instead of treating the file path as terminally missing.
        if not source_exists and source_path.suffix:
            parent = source_path.parent
            parent_exists = await get_running_loop().run_in_executor(None, parent.exists)
            if parent_exists:
                probe_root = parent
                source_exists = True

        source_seen = source_seen or source_exists
        last_probe_root = probe_root

        if source_exists:
            _reject_filesystem_root_probe(probe_root)
            comic_file = await get_running_loop().run_in_executor(
                None,
                finder,
                probe_root,
                allowed_extensions,
            )
            if comic_file is not None:
                return PostProcessingSourceProbe(
                    comic_file=comic_file,
                    probe_root=probe_root,
                    source_seen=True,
                    attempts=attempt,
                )

    return PostProcessingSourceProbe(
        comic_file=None,
        probe_root=last_probe_root,
        source_seen=source_seen,
        attempts=attempts,
    )


async def _resolve_local_path(
    session: AsyncSession,
    download: DownloadHistory,
) -> str | None:
    """Translate the download client's reported path to a local path."""
    raw_path = download.downloaded_path
    if not raw_path:
        return None

    from pullbox.models.client import DownloadClientConfig
    from pullbox.models.download import DownloadClientType

    if download.download_client is DownloadClientType.AIRDCPP:
        from pullbox.services.airdcpp_path_mapping import map_airdcpp_completed_path

        config_id = download.download_client_config_id
        if config_id is None:
            raise ValueError("AirDC++ completed download has no exact client identity")
        result = await session.execute(
            select(DownloadClientConfig).where(
                DownloadClientConfig.id == config_id,
                DownloadClientConfig.client_type == DownloadClientType.AIRDCPP,
            )
        )
        client_cfg = result.scalar_one_or_none()
        if client_cfg is None or not client_cfg.remote_path or not client_cfg.download_dir:
            raise ValueError("AirDC++ completed download path mapping is incomplete")
        mapped = await asyncio.to_thread(
            map_airdcpp_completed_path,
            remote_target=raw_path,
            remote_root=client_cfg.remote_path,
            local_root=client_cfg.download_dir,
            require_file=False,
        )
        logger.info(
            "airdcpp_path_mapping_applied",
            client_config_id=config_id,
        )
        return str(mapped)

    result = await session.execute(
        select(DownloadClientConfig).where(
            DownloadClientConfig.client_type == download.download_client,
            DownloadClientConfig.enabled.is_(True),
        )
    )
    client_cfg = result.scalars().first()

    if client_cfg and client_cfg.remote_path and client_cfg.download_dir:
        windows_origin = _is_windows_origin_path(client_cfg.remote_path)
        resolved = _map_configured_download_path(
            raw_path,
            remote_root=client_cfg.remote_path,
            local_root=client_cfg.download_dir,
        )
        if resolved is not None:
            logger.info(
                "path_mapping_applied",
                raw=raw_path,
                remote_prefix=client_cfg.remote_path,
                local_prefix=client_cfg.download_dir,
                resolved=resolved,
            )
            return resolved
        logger.warning(
            "path_mapping_prefix_mismatch",
            raw=raw_path,
            remote_prefix=client_cfg.remote_path,
            hint="The download client's path does not start with the "
            "configured Remote Path. Check that Remote Path matches "
            "the root of where the client stores completed downloads.",
        )
        if windows_origin:
            raise FileNotFoundError(
                "The download client's Windows path does not match the configured Remote Path. "
                "Verify both paths in Settings > Download Clients."
            )
    elif client_cfg and (client_cfg.remote_path or client_cfg.download_dir):
        logger.warning(
            "path_mapping_incomplete",
            has_remote_path=bool(client_cfg.remote_path),
            has_download_dir=bool(client_cfg.download_dir),
            hint="Both Remote Path and Download Directory must be set for path mapping to work.",
        )

    return raw_path


async def _resolve_local_download_root(
    session: AsyncSession,
    download: DownloadHistory,
) -> Path | None:
    """Return the configured local root that bounds source cleanup."""
    from pullbox.models.client import DownloadClientConfig

    result = await session.execute(
        select(DownloadClientConfig).where(
            DownloadClientConfig.client_type == download.download_client,
            DownloadClientConfig.enabled.is_(True),
        )
    )
    client_cfg = result.scalars().first()
    if client_cfg is None or not client_cfg.download_dir:
        return None
    download_dir = client_cfg.download_dir.strip()
    if not download_dir:
        return None

    return Path(download_dir).expanduser().resolve(strict=False)
