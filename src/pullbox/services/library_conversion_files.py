"""Private conversion preparation and immutable evidence for short publication."""

import asyncio
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from pullbox.core.exceptions import ValidationError
from pullbox.core.file_publication import publish_file_without_overwrite
from pullbox.services.archive_metadata_binding import (
    FileFingerprint,
    assemble_archive_metadata_state,
)
from pullbox.services.archive_metadata_publication import _digest, _file_work, _fingerprint
from pullbox.services.metadata_series_refresh_state import SeriesRefreshState
from pullbox.utilities.executors.archive_metadata_staging import stage_cbz_metadata_interruptible
from pullbox.utilities.executors.archive_subprocess import (
    convert_file_interruptible,
    transfer_file_interruptible,
)


class ConversionFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: Path
    fingerprint: FileFingerprint
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ConversionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    root_id: int
    root_path: str
    file_id: int | None
    issue_id: int | None
    file_format: str | None
    file_size: int | None
    file_modified_at: datetime | None


class ConversionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    binding: ConversionBinding
    original: ConversionFile
    output: ConversionFile
    backup: ConversionFile
    output_stage: Path
    backup_stage: Path
    directories: tuple[tuple[Path, int, int, int], ...]
    metadata_state_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @property
    def paths(self) -> tuple[Path, ...]:
        return (
            self.original.path,
            self.output.path,
            self.backup.path,
            self.output_stage,
            self.backup_stage,
        )


def decode_plan(encoded: str) -> ConversionPlan:
    if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > 65536:
        raise ValidationError("Conversion recovery evidence exceeds its limit.")
    plan = ConversionPlan.model_validate_json(encoded)
    if (
        any(not path.is_absolute() or ".." in path.parts for path in plan.paths)
        or len(set(plan.paths)) != 5
        or plan.output.path != plan.original.path.with_suffix(".cbz")
        or not plan.original.path.is_relative_to(Path(plan.binding.root_path).resolve())
        or plan.output_stage.parent.parent != plan.output.path.parent
        or plan.backup_stage.parent.parent != plan.backup.path.parent
        or any(
            not stage.parent.name.startswith(".pullbox-conversion-")
            for stage in (plan.output_stage, plan.backup_stage)
        )
    ):
        raise ValidationError("Conversion recovery evidence is invalid.")
    return plan


def conversion_metadata_digest(state: SeriesRefreshState, limit: int, block_dangerous: bool) -> str:
    payload = TypeAdapter(SeriesRefreshState).dump_python(state, mode="json")
    for entity in (payload["series"], *payload["issues"]):
        entity["overrides"] = sorted(entity["overrides"])
    evidence = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(evidence + f"/{limit}/{block_dangerous}".encode("ascii")).hexdigest()


def _regular(path: Path) -> FileFingerprint:
    evidence = _fingerprint(path)
    if evidence is None or not stat.S_ISREG(evidence[5]) or path.resolve() != path:
        raise ValidationError("Conversion requires an unchanged regular file.")
    return evidence


def require_writable_conversion_source(source: Path) -> None:
    if (
        not source.lstat().st_mode & 0o222
        or not source.parent.stat().st_mode & 0o222
        or not os.access(source, os.R_OK | os.W_OK)
        or not os.access(source.parent, os.W_OK | os.X_OK)
    ):
        raise ValidationError(
            "Read-only files cannot be converted; the original was left unchanged."
        )


def directories(*paths: Path) -> tuple[tuple[Path, int, int, int], ...]:
    result = []
    for path in sorted(set(parent for value in paths for parent in value.parents)):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.resolve() != path:
            raise ValidationError("Conversion locations must not contain symbolic links.")
        result.append((path, info.st_dev, info.st_ino, info.st_mode))
    return tuple(result)


def check_directories(plan: ConversionPlan) -> None:
    for path, device, inode, mode in plan.directories:
        info = path.lstat()
        if (info.st_dev, info.st_ino, info.st_mode) != (device, inode, mode):
            raise ValidationError("Conversion locations changed; review before retrying.")


def matches(actual: FileFingerprint | None, expected: FileFingerprint) -> bool:
    # Exclusive rename/link publication may change ctime, but not content identity.
    return bool(actual and actual[:4] == expected[:4] and actual[5] == expected[5])


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(plan: ConversionPlan) -> None:
    check_directories(plan)
    if _fingerprint(plan.original.path) != plan.original.fingerprint:
        raise ValidationError("The original changed during conversion.")
    for stage, destination in ((plan.backup_stage, plan.backup), (plan.output_stage, plan.output)):
        if stage.resolve(strict=True) != stage or _fingerprint(stage) != destination.fingerprint:
            raise ValidationError("Prepared conversion files changed.")
        publish_file_without_overwrite(stage, destination.path)
        sync_directory(destination.path.parent)


@asynccontextmanager
async def prepare_conversion(
    source: Path,
    backup: Path,
    binding: ConversionBinding,
    *,
    metadata_state: SeriesRefreshState | None = None,
    max_uncompressed_bytes: int | None = None,
    block_dangerous: bool = True,
) -> AsyncIterator[ConversionPlan]:
    """No public destination or source mutation; workers are reaped before cleanup."""
    source = source.absolute()
    before = _regular(source)
    metadata_digest = None
    if metadata_state is not None:
        require_writable_conversion_source(source)
        if max_uncompressed_bytes is None:
            raise ValidationError("Paired conversion requires a configured archive size limit.")
        metadata_digest = conversion_metadata_digest(
            metadata_state, max_uncompressed_bytes, block_dangerous
        )
    backup.parent.mkdir(parents=True, exist_ok=True)
    dirs = directories(source, backup)
    with (
        tempfile.TemporaryDirectory(prefix=".pullbox-conversion-", dir=source.parent) as output_dir,
        tempfile.TemporaryDirectory(prefix=".pullbox-conversion-", dir=backup.parent) as backup_dir,
    ):
        output_stage = Path(output_dir) / "output.cbz"
        backup_stage = Path(backup_dir) / "original"
        if metadata_state is None:
            await convert_file_interruptible(source, "cbz", output_path=output_stage)
        else:
            assert max_uncompressed_bytes is not None
            series, issue = assemble_archive_metadata_state(
                metadata_state, None, now=datetime.now(UTC)
            )
            async with stage_cbz_metadata_interruptible(
                source,
                Path(output_dir),
                series,
                issue,
                max_uncompressed_bytes=max_uncompressed_bytes,
                block_dangerous=block_dangerous,
                metadata_state=metadata_state,
            ) as staged:
                staged.check_unchanged()
                await asyncio.to_thread(publish_file_without_overwrite, staged.path, output_stage)
        await transfer_file_interruptible(source, backup_stage, "copy")

        def inspect(stop: Event) -> ConversionPlan:
            if _regular(source) != before:
                raise ValidationError("The original changed during conversion.")
            # Permission metadata is applied only to new owned stages, never to the source.
            os.chmod(output_stage, stat.S_IMODE(before[5]) & 0o777)
            now = datetime.now(UTC).timestamp()
            os.utime(backup_stage, (now, now))
            files = []
            for path in (source, output_stage, backup_stage):
                info = _regular(path)
                digest = _digest(path, info, stop)
                if path != source:
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
                files.append(ConversionFile(path=path, fingerprint=info, digest=digest))
            if files[0].digest != files[2].digest or files[0].fingerprint != before:
                raise ValidationError("The original changed while its backup was prepared.")
            plan = ConversionPlan(
                binding=binding,
                original=files[0],
                output=files[1].model_copy(update={"path": source.with_suffix(".cbz")}),
                backup=files[2].model_copy(update={"path": backup}),
                output_stage=output_stage,
                backup_stage=backup_stage,
                directories=dirs,
                metadata_state_digest=metadata_digest,
            )
            check_directories(plan)
            return decode_plan(plan.model_dump_json())

        yield await _file_work(inspect)


async def inspect_conversion(plan: ConversionPlan) -> dict[str, ConversionFile | None]:
    def inspect(stop: Event) -> dict[str, ConversionFile | None]:
        check_directories(plan)
        result: dict[str, ConversionFile | None] = {}
        for name in ("original", "output", "backup"):
            expected: ConversionFile = getattr(plan, name)
            actual = _fingerprint(expected.path)
            result[name] = None
            if actual is not None:
                digest = "0" * 64
                if matches(actual, expected.fingerprint):
                    digest = _digest(expected.path, actual, stop)
                result[name] = ConversionFile(path=expected.path, fingerprint=actual, digest=digest)
        return result

    return await _file_work(inspect)


async def remove_original(plan: ConversionPlan, inspected: ConversionFile | None) -> None:
    def remove() -> None:
        check_directories(plan)
        if _fingerprint(plan.original.path) != (inspected.fingerprint if inspected else None):
            raise ValidationError("The original changed before conversion cleanup.")
        if inspected:
            plan.original.path.unlink()
            sync_directory(plan.original.path.parent)

    await asyncio.to_thread(remove)
