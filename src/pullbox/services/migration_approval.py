"""Private, expiring operator approval for one reviewed deployment and library."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import tempfile
import time
from typing import TYPE_CHECKING

from pullbox.core.config_file import ConfigFileProvider
from pullbox.services.migration_assets import check_assets
from pullbox.services.migration_first_start import (
    SOURCE_PREFIX,
    BindingError,
    check_binding,
    read_token,
)

WINDOW = 3600

if TYPE_CHECKING:
    from pathlib import Path

    from pullbox.config import PullboxSettings


def _signature(token: bytes, secret: str, payload: dict[str, object]) -> str:
    # Even when the library has no encrypted credentials, require the same effective key.
    key = hmac.digest(token, b"pullbox:cutover:key:v1\0" + secret.encode(), "sha256")
    return hmac.digest(
        key, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(), "sha256"
    ).hex()


def write_approval(
    path: Path, binding: str, media: str, token: bytes, secret: str, *, all_files: bool
) -> None:
    if not binding.startswith(SOURCE_PREFIX):
        raise BindingError("Only a source-bound stage can receive approval.")
    payload: dict[str, object] = {
        "version": 1,
        "binding": hashlib.sha256(binding.encode()).hexdigest(),
        "media_sha256": media,
        "all_files": all_files,
        "expires": int(time.time()) + WINDOW,
    }
    document = {"approval": payload, "signature": _signature(token, secret, payload)}
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent) as temp:
        json.dump(document, temp, sort_keys=True)
        temp.flush()
        os.fsync(temp.fileno())
        os.link(temp.name, path)  # Publish complete, private bytes; never replace an approval.
        try:
            fd = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            if os.path.samestat(path.lstat(), os.fstat(temp.fileno())):
                path.unlink()
            raise


def _read(path: Path, binding: str, key: bytes, secret: str) -> tuple[str, bool, int]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
                raise ValueError
            raw = stream.read(4097)
        if len(raw) != info.st_size:
            raise ValueError
        doc = json.loads(raw)
        data = doc["approval"]
        if (
            data["version"] != 1
            or type(data["all_files"]) is not bool
            or type(data["expires"]) is not int
            or not 0 < data["expires"] - time.time() <= WINDOW
            or data["binding"] != hashlib.sha256(binding.encode()).hexdigest()
            or not hmac.compare_digest(doc["signature"], _signature(key, secret, data))
        ):
            raise ValueError
        return data["media_sha256"], data["all_files"], data["expires"]
    except (KeyError, TypeError, ValueError, RecursionError, OSError) as exc:
        raise BindingError(
            "Approval is invalid, expired, or for a different stage or application key."
        ) from exc


async def verify_deployment(
    settings: PullboxSettings, backup: Path, approval: Path, token_file: Path, digest: str
) -> None:
    if not os.statvfs(backup).f_flag & os.ST_RDONLY:
        raise BindingError("Mount the restored SQLite backup read-only before checking approval.")
    binding = await check_binding(settings.db_url, token_file, digest)
    config = settings.data_dir / "config.xml"
    secret = ConfigFileProvider(config).secret_key()
    media, all_files, expires = _read(approval, binding, read_token(token_file), secret)
    legacy = settings.data_dir / "covers"
    assets = check_assets(
        backup,
        digest,
        None,
        compare=media,
        all_files=all_files,
        configured_url=settings.db_url,
        config_path=config,
        covers_dir=settings.covers_dir,
        verified_key=secret,
        legacy_covers_dir=legacy
        if legacy.exists() and not legacy.samefile(settings.covers_dir)
        else None,
    )
    if not assets.checks_passed or not assets.sample_compared:
        raise BindingError(
            "The restored backup or deployed media mounts did not match: "
            + ", ".join(sorted(assets.blockers))
        )
    if (
        binding != await check_binding(settings.db_url, token_file, digest)
        or time.time() >= expires
    ):
        raise BindingError("The approval expired or the target changed during verification.")
