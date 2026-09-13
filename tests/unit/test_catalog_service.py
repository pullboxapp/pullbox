"""Downloads remain resumable and a failed update preserves the active catalog."""

import asyncio
import hashlib

import httpx
import pytest
import zstandard
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pullbox.services.catalog.contract import CatalogError
from pullbox.services.catalog.service import CatalogService
from tests.catalog_fixtures import build_patch, build_snapshot
from tests.unit.test_catalog_contract import publication_payload, signed_publication


class Server:
    def __init__(self, tmp_path):
        self.key = Ed25519PrivateKey.generate()
        self.payload = publication_payload()
        self.requests = []
        self.artifacts = {}
        self.etag = '"publication-1"'
        self.fail_download = False
        self.base = tmp_path / "producer.db"
        build_snapshot(self.base)
        self.publish(self.base)

    def publish(self, path, *, base=None, version="20260913T050000Z"):
        data = zstandard.ZstdCompressor().compress(path.read_bytes())
        url = (
            f"/api/v2/catalog/patches/{base}/{version}"
            if base
            else f"/api/v2/catalog/snapshots/{version}"
        )
        item = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "download_path": url,
        }
        item.update(
            {"base_version": base, "target_version": version} if base else {"version": version}
        )
        if base:
            self.payload["patches"] = [item]
        else:
            self.payload["full_snapshot"] = item
        self.payload["latest_version"] = version
        self.artifacts[url] = data
        self.etag = f'"{version}"'

    async def handle(self, request):
        self.requests.append(request)
        if request.url.path.endswith("/latest"):
            if request.headers.get("if-none-match") == self.etag:
                return httpx.Response(304)
            return httpx.Response(
                200, content=signed_publication(self.payload, self.key), headers={"etag": self.etag}
            )
        if self.fail_download:
            return httpx.Response(503)
        data = self.artifacts[request.url.path]
        start = int(request.headers.get("range", "bytes=0-").split("=")[1].split("-")[0])
        headers = {"etag": f'"{hashlib.sha256(data).hexdigest()}"'}
        if start:
            headers["content-range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
        return httpx.Response(206 if start else 200, content=data[start:], headers=headers)

    def service(self, root):
        return CatalogService(
            root,
            "https://catalog.example",
            keys={"test": self.key.public_key()},
            transport=httpx.MockTransport(self.handle),
        )


async def test_first_download_requires_opt_in_then_installs_verified_catalog(tmp_path):
    server = Server(tmp_path)
    service = server.service(tmp_path / "local")
    assert await service.sync() is False
    assert server.requests == []
    assert await service.sync(manual=True) is True
    state = service.status()
    assert state.installed_version == "20260913T050000Z"
    assert state.phase == "current"
    assert (service.root / "active.json").is_file()
    count = len(server.requests)
    assert await service.sync() is False
    assert len(server.requests) == count


async def test_manual_retry_repairs_missing_installed_file(tmp_path):
    server = Server(tmp_path)
    service = server.service(tmp_path / "local")
    await service.sync(manual=True)
    installed = service.root / "bases/20260913T050000Z.db"
    installed.unlink()
    assert await service.sync(manual=True) is True
    assert installed.is_file()


async def test_cancellation_releases_lock_and_allows_retry(tmp_path):
    server = Server(tmp_path)
    service = server.service(tmp_path / "local")
    started = asyncio.Event()
    original = server.handle

    async def waiting(request):
        started.set()
        await asyncio.Event().wait()

    service.transport = httpx.MockTransport(waiting)
    task = asyncio.create_task(service.sync(manual=True))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.status().phase == "interrupted"
    assert not (service.root / "active.json").exists()
    service.transport = httpx.MockTransport(original)
    assert await service.sync(manual=True) is True


async def test_disabling_daily_updates_survives_process_restart(tmp_path):
    server = Server(tmp_path)
    service = server.service(tmp_path / "local")
    await service.sync(manual=True)
    await service.set_automatic_updates(False)
    restored = server.service(service.root)
    assert restored.status().automatic_updates is False
    server.requests.clear()
    assert await restored.sync() is False
    assert server.requests == []


async def test_success_discards_compressed_downloads_and_stale_staging(tmp_path):
    server = Server(tmp_path)
    service = server.service(tmp_path / "local")
    staging = service.root / "staging"
    staging.mkdir(parents=True)
    (staging / "catalog-abandoned.db").write_bytes(b"incomplete")
    await service.sync(manual=True)
    assert list((service.root / "downloads").iterdir()) == []
    assert list(staging.iterdir()) == []


async def test_resumes_partial_artifact_using_signed_checksum_as_identity(tmp_path):
    server = Server(tmp_path)
    root = tmp_path / "local"
    download = root / "downloads"
    download.mkdir(parents=True)
    artifact = server.payload["full_snapshot"]
    data = server.artifacts[artifact["download_path"]]
    (download / f"{artifact['sha256']}.part").write_bytes(data[:100])
    assert await server.service(root).sync(manual=True) is True
    assert server.requests[-1].headers["range"] == "bytes=100-"


async def test_installs_cumulative_patch_without_redownloading_weekly_base(tmp_path):
    server = Server(tmp_path)
    service = server.service(tmp_path / "local")
    assert await service.sync(manual=True) is True
    target, patch = tmp_path / "next.db", tmp_path / "patch.db"
    build_snapshot(target, "20260914T050000Z", "Updated")
    build_patch(patch, server.base, target)
    server.publish(patch, base="20260913T050000Z", version="20260914T050000Z")
    assert await service.sync(manual=True) is True
    assert service.status().installed_version == "20260914T050000Z"
    assert len([r for r in server.requests if "/snapshots/" in r.url.path]) == 1


async def test_failed_update_keeps_active_catalog_and_safe_error(tmp_path):
    server = Server(tmp_path)
    service = server.service(tmp_path / "local")
    assert await service.sync(manual=True) is True
    active = (service.root / "active.json").read_bytes()
    target = tmp_path / "next.db"
    build_snapshot(target, "20260920T050000Z")
    server.publish(target, version="20260920T050000Z")
    server.fail_download = True
    with pytest.raises(CatalogError):
        await service.sync(manual=True)
    assert (service.root / "active.json").read_bytes() == active
    assert service.status().installed_version == "20260913T050000Z"
    assert service.status().phase == "failed"


async def test_corrupt_download_never_becomes_active(tmp_path):
    server = Server(tmp_path)
    artifact = server.payload["full_snapshot"]
    server.artifacts[artifact["download_path"]] = b"x" * artifact["size_bytes"]
    service = server.service(tmp_path / "local")
    with pytest.raises(CatalogError, match="checksum"):
        await service.sync(manual=True)
    assert not (service.root / "active.json").exists()


async def test_manual_and_scheduled_attempts_do_not_overlap(tmp_path):
    server = Server(tmp_path)
    service = server.service(tmp_path / "local")
    results = await asyncio.gather(service.sync(manual=True), service.sync(manual=True))
    assert sorted(results) == [False, True]
    assert len([r for r in server.requests if "/snapshots/" in r.url.path]) == 1
