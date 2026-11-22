"""Verify a separately restored deployment and its explicit key without starting Pullbox."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException

from pullbox.core.secret_validation import WeakApplicationSecretError, validate_application_secret
from pullbox.core.xml_security import parse_untrusted_xml
from pullbox.services.migration_asset_files import (
    AssetVerificationError,
    file_stamp,
    open_directory,
)
from pullbox.services.migration_assets import AssetReport, check_saved_credentials, checked_snapshot
from pullbox.services.migration_backup_input import BackupInputError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


@dataclass
class RestoreReport:
    restore_verified: bool = False
    cutover_ready: bool = False
    snapshot_sha256: str | None = None
    settings_files_compared: int = 0
    credentials_checked: int = 0
    key_evidence: str = "not_checked"
    blockers: dict[str, int] = field(default_factory=dict)

    @property
    def next_step(self) -> str:
        if {"snapshot_mismatch", "invalid_fingerprint"} & self.blockers.keys():
            return "Use the restored backup and the snapshot_sha256 from the original capture."
        if {"invalid_recovery_key", "credential_not_decryptable"} & self.blockers.keys():
            return "Restore the original effective application key. Do not generate a new key."
        if "configuration_mismatch" in self.blockers:
            return (
                "The settings differ. Restore the original deployment files, including overrides."
            )
        if "configuration_incomplete" in self.blockers or "ambiguous_key_source" in self.blockers:
            return "Include the deployment definition and select the actual source of the key."
        if "unsafe_configuration" in self.blockers:
            return "Use two independent owner-only directories with regular files, not links."
        if self.blockers:
            return "Check the SQLite backup and both private configuration sets. Do not cut over."
        if self.key_evidence == "no_ciphertext":
            return (
                "The files match, but no saved ciphertext proves the historical key. "
                "Confirm it against the original secret store. Do not cut over."
            )
        return "Keep the protected backup and check media and mounts separately. Do not cut over."


@dataclass(repr=False)
class _Settings:
    hashes: dict[tuple[str, ...], bytes | None] = field(default_factory=dict)
    stamps: dict[tuple[str, ...], tuple[int, ...]] = field(default_factory=dict)
    key_bytes: bytes = field(default=b"", repr=False)
    size: int = 0


def _read(root: int, prefix: tuple[str, ...], key_file: str, settings: _Settings) -> None:
    with os.scandir(root) as entries:
        for entry in entries:
            parts = (*prefix, entry.name)
            if len(parts) > 12 or len(settings.hashes) >= 128:
                raise AssetVerificationError("configuration_too_large")
            info = entry.stat(follow_symlinks=False)
            settings.stamps[parts] = file_stamp(info)
            if stat.S_ISDIR(info.st_mode):
                settings.hashes[parts] = None
                with open_directory(root, (entry.name,)) as child:
                    if not os.path.samestat(info, os.fstat(child)):
                        raise AssetVerificationError("configuration_changed")
                    _read(child, parts, key_file, settings)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise AssetVerificationError("unsafe_configuration")
            settings.size += info.st_size
            if info.st_size > 4 * 1024**2 or settings.size > 32 * 1024**2:
                raise AssetVerificationError("configuration_too_large")
            fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            with os.fdopen(fd, "rb") as stream:
                if not os.path.samestat(info, os.fstat(fd)):
                    raise AssetVerificationError("configuration_changed")
                data = stream.read(info.st_size + 1)
                if len(data) != info.st_size or file_stamp(os.fstat(fd)) != file_stamp(info):
                    raise AssetVerificationError("configuration_changed")
            if parts == (key_file,):
                settings.key_bytes = data
            settings.hashes[parts] = hashlib.sha256(data).digest()


@contextmanager
def _settings(path: Path, key_file: str, definition: tuple[str, ...]) -> Iterator[_Settings]:
    fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
    try:
        initial = os.fstat(fd)
        if initial.st_mode & 0o077:
            raise AssetVerificationError("unsafe_configuration")
        settings = _Settings()
        _read(fd, (), key_file, settings)
        if not settings.key_bytes or settings.hashes.get(definition) in (
            None,
            hashlib.sha256(b"").digest(),
        ):
            raise AssetVerificationError("configuration_incomplete")
        if key_file == "config.xml" and ("application-key",) in settings.hashes:
            raise AssetVerificationError("ambiguous_key_source")
        yield settings
        # Do not approve a mixed-time view if a backup tool replaced files while we checked.
        for parts, stamp in settings.stamps.items():
            with open_directory(fd, parts[:-1]) as parent:
                if file_stamp(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)) != stamp:
                    raise AssetVerificationError("configuration_changed")
        if file_stamp(path.lstat()) != file_stamp(initial):
            raise AssetVerificationError("configuration_changed")
    finally:
        os.close(fd)


def restored_key(raw: bytes, key_source: str) -> str:
    try:
        if key_source == "config-xml":
            tree = parse_untrusted_xml(raw)
            elements = tree.findall("SecretKey")
            if tree.tag != "Config" or len(elements) != 1:
                raise AssetVerificationError("invalid_recovery_key")
            value = (elements[0].text or "").strip()
        else:
            lines = raw.decode("utf-8").splitlines()
            if len(lines) != 1 or "\x00" in lines[0]:
                raise AssetVerificationError("invalid_recovery_key")
            value = lines[0].strip()
        validate_application_secret(value, allow_test_override=False)
        return value
    except (
        ParseError,
        DefusedXmlException,
        WeakApplicationSecretError,
        UnicodeError,
        LookupError,
    ) as exc:
        raise AssetVerificationError("invalid_recovery_key") from exc


def check_restore(
    backup: Path,
    digest: str,
    original: Path,
    restored: Path,
    key_source: str,
    configured_url: str,
    *,
    deployment_file: str = "compose.yaml",
    on_verified_key: Callable[[str], None] | None = None,
) -> RestoreReport:
    report = RestoreReport()
    try:
        if key_source not in {"config-xml", "secret-file"}:
            raise AssetVerificationError("invalid_recovery_key")
        definition = tuple(deployment_file.split("/"))
        if any(part in ("", ".", "..") or "\x00" in part for part in definition) or definition in {
            ("config.xml",),
            ("application-key",),
            (".env",),
        }:
            raise AssetVerificationError("invalid_deployment_file")
        old, new = original.resolve(strict=True), restored.resolve(strict=True)
        if old.is_relative_to(new) or new.is_relative_to(old) or old.samefile(new):
            raise AssetVerificationError("unsafe_configuration")
        key_file = "config.xml" if key_source == "config-xml" else "application-key"
        with (
            checked_snapshot(backup, digest, configured_url) as (db, checks),
            _settings(original, key_file, definition) as reference,
            _settings(restored, key_file, definition) as recovered,
        ):
            if reference.hashes != recovered.hashes:
                raise AssetVerificationError("configuration_mismatch")
            if {s[:2] for s in reference.stamps.values()} & {
                s[:2] for s in recovered.stamps.values()
            }:
                raise AssetVerificationError("unsafe_configuration")
            report.settings_files_compared = sum(h is not None for h in recovered.hashes.values())
            report.snapshot_sha256 = digest.lower()
            if checks.blockers:
                report.blockers.update(dict.fromkeys(checks.blockers, 1))
            else:
                evidence = AssetReport()
                recovered_key = restored_key(recovered.key_bytes, key_source)
                check_saved_credentials(db, recovered_key, evidence)
                report.credentials_checked = evidence.credentials_checked
                report.key_evidence = evidence.key_evidence
                report.blockers.update(evidence.blockers)
    except (AssetVerificationError, BackupInputError) as exc:
        report.blockers[str(exc)] = 1
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        report.blockers["restore_unavailable"] = 1
    report.restore_verified = not report.blockers
    if report.restore_verified and on_verified_key is not None:
        on_verified_key(recovered_key)
    return report
