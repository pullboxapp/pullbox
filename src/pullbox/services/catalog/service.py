"""One resumable, verified catalog update at a time, independent of library writes."""

from __future__ import annotations

import asyncio
import fcntl
import os
import shutil
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from pullbox.config import get_settings
from pullbox.services.catalog.contract import (
    MAX_MANIFEST_BYTES,
    TRUSTED_KEYS,
    Artifact,
    CatalogError,
    Publication,
    valid_version,
    verify_manifest,
)
from pullbox.services.catalog.database import (
    apply_catalog_patch,
    file_sha256,
    safe_path,
    validate_snapshot,
)
from pullbox.services.catalog.retention import cleanup
from pullbox.services.catalog.storage import (
    activate_file,
    atomic_json,
    decompress,
    disk_work,
    load_json,
    stage_path,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = structlog.get_logger(__name__)
BUSY_PHASES = {"checking", "downloading", "verifying", "decompressing", "installing"}


class CatalogStatus(BaseModel):
    phase: str = "not_downloaded"
    requested: bool = False
    automatic_updates: bool = True
    installed_version: str | None = None
    source_cutoff_at: str | None = None
    target_version: str | None = None
    bytes_downloaded: int = 0
    bytes_total: int = 0
    catalog_size_bytes: int = 0
    last_checked_at: datetime | None = None
    last_updated_at: datetime | None = None
    attempt_started_at: datetime | None = None
    error: str | None = None


class CatalogService:
    """Use only signed API coordinates and activate after all validation succeeds."""

    def __init__(
        self,
        root: Path,
        base_url: str,
        *,
        keys: Mapping[str, Ed25519PublicKey] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.root = root
        url = httpx.URL(base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
        ):
            raise CatalogError("The Pullbox API address is invalid.")
        self.base_url = str(url).rstrip("/")
        self.keys = TRUSTED_KEYS if keys is None else keys
        self.transport = transport
        self._lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        try:
            self._state = CatalogStatus.model_validate(load_json(root / "state.json"))
            if self._state.phase in BUSY_PHASES:
                self._state.phase = "interrupted"
                self._state.error = "The previous download was interrupted. It can be resumed."
        except (CatalogError, ValidationError):
            self._state = CatalogStatus(
                phase="failed", error="Catalog state could not be read. Check the data volume."
            )
        # The active generation survives a missing or damaged optional status file.
        try:
            active = load_json(root / "active.json")
            if active:
                version, cutoff = str(active["version"]), str(active["source_cutoff_at"])
                self._state.installed_version = version
                self._state.source_cutoff_at = cutoff
                self._state.requested = True
                if self._state.phase == "not_downloaded":
                    self._state.phase = "current"
        except (CatalogError, KeyError):
            self._state.phase = "failed"
            self._state.error = "Catalog state could not be read. Check the data volume."

    def status(self) -> CatalogStatus:
        return self._state.model_copy(deep=True)

    async def set_automatic_updates(self, enabled: bool) -> None:
        self._state.automatic_updates = enabled
        await self._save()

    async def _save(self) -> None:
        async with self._state_lock:
            await disk_work(
                atomic_json, self.root / "state.json", self._state.model_dump(mode="json")
            )

    async def sync(self, *, manual: bool = False) -> bool:
        if self._lock.locked():
            return False
        if not manual:
            if not self._state.requested or not self._state.automatic_updates:
                return False
            checked = self._state.last_checked_at
            # Allow the daily scheduler's jitter to move earlier than yesterday.
            if checked and checked > datetime.now(UTC) - timedelta(hours=23):
                return False
        async with self._lock:
            safe_path(self.root).mkdir(parents=True, exist_ok=True)
            lock_path = safe_path(self.root / "update.lock")
            with lock_path.open("a+b") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False
                try:
                    self._state.attempt_started_at = datetime.now(UTC)
                    await disk_work(cleanup, self.root)
                    self._state.requested = True
                    self._state.phase, self._state.error = "checking", None
                    await self._save()
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(60, connect=15),
                        follow_redirects=False,
                        transport=self.transport,
                    ) as client:
                        changed = await self._update(client)
                    await disk_work(lambda: cleanup(self.root, completed=True))
                    self._state.phase = "current"
                    self._state.last_checked_at = datetime.now(UTC)
                    await self._save()
                    logger.info(
                        "catalog_update_complete",
                        version=self._state.installed_version,
                        changed=changed,
                    )
                    return changed
                except asyncio.CancelledError:
                    self._state.phase = "interrupted"
                    await self._save()
                    raise
                except (CatalogError, httpx.HTTPError, OSError) as exc:
                    message = (
                        str(exc)
                        if isinstance(exc, CatalogError)
                        else "Catalog download failed. Check the API connection and "
                        "available disk space, then retry."
                    )
                    self._state.phase, self._state.error = "failed", message
                    await self._save()
                    logger.warning("catalog_update_failed", reason=message)
                    raise CatalogError(message) from exc
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    async def _manifest(self, client: httpx.AsyncClient) -> Publication:
        cache = await disk_work(load_json, self.root / "manifest.json")
        headers = {"Accept-Encoding": "identity"}
        cached_raw = cache.get("raw")
        if isinstance(cached_raw, str) and isinstance(cache.get("etag"), str):
            verify_manifest(cached_raw.encode(), self.keys)
            headers["If-None-Match"] = cache["etag"]
        async with client.stream(
            "GET", self.base_url + "/api/v2/catalog/latest", headers=headers
        ) as response:
            if response.status_code == 304 and isinstance(cached_raw, str):
                return verify_manifest(cached_raw.encode(), self.keys)
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_MANIFEST_BYTES:
                    raise CatalogError("Catalog manifest is too large.")
            publication = verify_manifest(bytes(raw), self.keys)
            await disk_work(
                atomic_json,
                self.root / "manifest.json",
                {"raw": raw.decode(), "etag": response.headers.get("etag")},
            )
            return publication

    async def _download(self, client: httpx.AsyncClient, artifact: Artifact) -> Path:
        directory = safe_path(self.root / "downloads")
        directory.mkdir(parents=True, exist_ok=True)
        path = safe_path(directory / f"{artifact.sha256}.part")
        offset = path.stat().st_size if path.exists() else 0
        if offset > artifact.size_bytes:
            path.unlink()
            offset = 0
        if shutil.disk_usage(directory).free < artifact.size_bytes - offset + 64 * 1024 * 1024:
            raise CatalogError("Not enough disk space to download the catalog.")
        self._state.phase = "downloading"
        self._state.bytes_downloaded, self._state.bytes_total = offset, artifact.size_bytes
        await self._save()
        if offset < artifact.size_bytes:
            headers = {"Accept-Encoding": "identity"}
            if offset:
                headers.update({"Range": f"bytes={offset}-", "If-Range": f'"{artifact.sha256}"'})
            async with client.stream(
                "GET", self.base_url + artifact.download_path, headers=headers
            ) as response:
                response.raise_for_status()
                if response.status_code == 206:
                    expected = f"bytes {offset}-{artifact.size_bytes - 1}/{artifact.size_bytes}"
                    if response.headers.get("content-range") != expected:
                        raise CatalogError(
                            "Catalog resume response is invalid. Retry the download."
                        )
                elif response.status_code == 200:
                    offset = 0
                else:
                    raise CatalogError("Catalog download response is invalid.")
                with path.open("ab" if offset else "wb") as stream:
                    async for chunk in response.aiter_bytes(256 * 1024):
                        offset += len(chunk)
                        if offset > artifact.size_bytes:
                            raise CatalogError("Catalog download exceeded its signed size.")
                        await disk_work(stream.write, chunk)
                        self._state.bytes_downloaded = offset
                    await disk_work(stream.flush)
                    await disk_work(os.fsync, stream.fileno())
        self._state.phase = "verifying"
        if (
            path.stat().st_size != artifact.size_bytes
            or await disk_work(file_sha256, path) != artifact.sha256
        ):
            path.unlink(missing_ok=True)
            raise CatalogError("Catalog download checksum failed. Retry the download.")
        return path

    async def _snapshot(self, client: httpx.AsyncClient, artifact: Artifact) -> Path:
        target = safe_path(self.root / "bases" / f"{artifact.version}.db")
        if target.exists():
            try:
                await disk_work(validate_snapshot, target, artifact.version)
                return target
            except CatalogError:
                logger.warning("catalog_weekly_base_invalid", version=artifact.version)
        archive = await self._download(client, artifact)
        stage = stage_path(self.root, ".db")
        try:
            self._state.phase = "decompressing"
            await disk_work(decompress, archive, stage)
            self._state.phase = "verifying"
            await disk_work(validate_snapshot, stage, artifact.version)
            await disk_work(activate_file, stage, target)
        finally:
            stage.unlink(missing_ok=True)
        return target

    async def _update(self, client: httpx.AsyncClient) -> bool:
        publication = await self._manifest(client)
        active = await disk_work(load_json, self.root / "active.json")
        self._state.target_version = publication.latest_version
        if active and str(active.get("version", "")) >= publication.latest_version:
            version = valid_version(active.get("version"))
            relative = active.get("path")
            if relative not in {f"bases/{version}.db", f"versions/{version}.db"}:
                raise CatalogError("Catalog active file is invalid. Check the data volume.")
            try:
                await disk_work(validate_snapshot, self.root / str(relative), version)
                return False
            except CatalogError:
                if version > publication.latest_version:
                    raise CatalogError(
                        "The API has an older catalog. The installed version was not replaced."
                    ) from None
                active = {}  # Do not replace a good previous reference with a broken one.
        base = await self._snapshot(client, publication.full_snapshot)
        patch = publication.latest_patch()
        target = base
        if patch:
            if shutil.disk_usage(self.root).free < base.stat().st_size * 2 + 64 * 1024 * 1024:
                raise CatalogError("Not enough disk space to apply the catalog update.")
            archive = await self._download(client, patch)
            unpacked, stage = stage_path(self.root, ".patch.db"), stage_path(self.root, ".db")
            try:
                self._state.phase = "decompressing"
                await disk_work(decompress, archive, unpacked)
                self._state.phase = "installing"
                await disk_work(
                    apply_catalog_patch,
                    base,
                    unpacked,
                    stage,
                    publication.full_snapshot.version,
                    patch.version,
                )
                target = self.root / "versions" / f"{patch.version}.db"
                await disk_work(activate_file, stage, target)
            finally:
                unpacked.unlink(missing_ok=True)
                stage.unlink(missing_ok=True)
        self._state.phase = "verifying"
        manifest = await disk_work(validate_snapshot, target, publication.latest_version)
        self._state.phase = "installing"
        reference: dict[str, Any] = {
            "version": publication.latest_version,
            "base_version": publication.full_snapshot.version,
            "path": str(target.relative_to(self.root)),
            "source_cutoff_at": manifest["source_cutoff_at"],
        }
        if active:
            await disk_work(atomic_json, self.root / "previous.json", active)
        await disk_work(atomic_json, self.root / "active.json", reference)
        self._state.installed_version = publication.latest_version
        self._state.source_cutoff_at = str(manifest["source_cutoff_at"])
        self._state.catalog_size_bytes = target.stat().st_size
        self._state.last_updated_at = datetime.now(UTC)
        return True


@lru_cache(maxsize=4)
def _service(root: Path, base_url: str) -> CatalogService:
    return CatalogService(root, base_url)


def get_catalog_service() -> CatalogService:
    settings = get_settings()
    return _service(settings.data_dir / "catalog", settings.pullbox_data_api_base_url)
