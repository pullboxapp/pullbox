"""Check deployment assets against the exact backup whose rows passed offline staging."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from itertools import groupby
from operator import itemgetter
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.fernet import InvalidToken

from pullbox.core.config_file import ConfigFileError, ConfigFileProvider
from pullbox.core.encryption import derive_secret_cipher, is_encrypted
from pullbox.services.database_migration_preflight import (
    PreflightReport,
    inspect_snapshot_connection,
)
from pullbox.services.migration_asset_files import AssetVerificationError, hex_digest, inspect_file
from pullbox.services.migration_backup_input import BackupInputError, database_snapshot
from pullbox.services.migration_covers import verify_covers
from pullbox.services.migration_media import MediaMounts, verify_placements

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


@dataclass
class AssetReport:
    checks_passed: bool = False
    cutover_ready: bool = False
    snapshot_sha256: str | None = None
    key_evidence: str = "not_checked"
    credentials_checked: int = 0
    files_checked: int = 0
    files_hashed: int = 0
    placements_checked: int = 0
    covers_checked: int = 0
    cover_cache_mapped: bool = False
    media_examples: dict[str, list[int]] = field(default_factory=dict)
    hash_scope: str = "sample"
    saved_hashes_matched: int = 0
    sample_sha256: str | None = None
    sample_compared: bool = False
    root_files: dict[int, int] = field(default_factory=dict)
    root_problems: dict[int, str] = field(default_factory=dict)
    blockers: dict[str, int] = field(default_factory=dict)
    file_examples: dict[str, list[int]] = field(default_factory=dict)
    credential_stores_with_errors: set[str] = field(default_factory=set)

    @property
    def next_step(self) -> str:
        if "snapshot_mismatch" in self.blockers:
            return "Use the exact backup and snapshot_sha256 from the successful stage report."
        if self.key_evidence in {"failed", "unavailable"}:
            return (
                "Check the original application key and saved credential settings. "
                "Do not generate a replacement key. Correct any file blockers and retry."
            )
        if self.blockers:
            if any(code.startswith("arc_") for code in self.blockers):
                return (
                    "Check the original Story Arc placements, their canonical files and mount "
                    "layout. Review missing or changed copies/links in Story Arcs; do not "
                    "rebind ownership or delete files to pass this check."
                )
            if any("cover" in code for code in self.blockers):
                return (
                    "Check the series folders, --covers-dir and any separate --legacy-covers-dir "
                    "on the original and proposed mounts. Keep existing artwork intact."
                )
            return (
                "Check the reported blockers and original root mappings; keep the backup "
                "and files intact. Do not use this run as a baseline."
            )
        if self.sample_sha256 and not self.sample_compared:
            return (
                "Use sample_sha256 from the original mount with --compare-sample on the "
                "proposed mount, using the same backup and hash scope."
            )
        return "This check does not authorize cutover. Keep the staging database closed."

    def block(self, reason: str, file_id: int | None = None) -> None:
        self.blockers[reason] = self.blockers.get(reason, 0) + 1
        if file_id is not None:
            examples = self.file_examples.setdefault(reason, [])
            if len(examples) < 5:
                examples.append(file_id)

    def media_block(self, reason: str, record_id: int) -> None:
        self.block(reason)
        examples = self.media_examples.setdefault(reason, [])
        if len(examples) < 5:
            examples.append(record_id)


_STORES = (
    (
        "settings",
        "SELECT value FROM system_config WHERE value_type='secret' OR "
        "key IN ('comicvine_api_key','prowlarr_api_key','jackett_api_key')",
        "scalar",
    ),
    ("metadata_sources", "SELECT credential_secret FROM metadata_source_configs", "scalar"),
    ("indexers", "SELECT api_key FROM indexer_configs", "scalar"),
    (
        "download_clients",
        "SELECT api_key FROM download_client_configs UNION ALL "
        "SELECT password FROM download_client_configs",
        "scalar",
    ),
    ("direct_providers", "SELECT encrypted_bearer_token FROM direct_provider_configs", "scalar"),
    ("direct_providers", "SELECT encrypted_configuration FROM direct_provider_configs", "mapping"),
    ("artifact_hosts", "SELECT encrypted_credentials FROM direct_host_configs", "mapping"),
    ("resolvers", "SELECT encrypted_auth_headers FROM direct_resolver_configs", "mapping"),
    ("pending_matches", "SELECT match_details FROM pending_matches", "route"),
)


def _credential_values(raw: object, kind: str) -> Iterable[object]:
    if kind == "scalar" or raw is None:
        return (raw,)
    if not isinstance(raw, str):
        raise ValueError
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError
    if kind == "route":
        return (obj.get("dc_route_snapshot"),)
    if not all(isinstance(v, str) and v for v in obj.values()):
        raise ValueError
    return obj.values()


def _credentials(db: sqlite3.Connection, config: Path, report: AssetReport) -> None:
    try:
        # secret_key() only reads existing config or its environment override.
        secret = ConfigFileProvider(config).secret_key()
    except (ConfigFileError, OSError):
        report.block("application_key_unavailable")
        report.key_evidence = "unavailable"
        return
    check_saved_credentials(db, secret, report)


def check_saved_credentials(db: sqlite3.Connection, secret: str, report: AssetReport) -> None:
    """Test ciphertext with the caller's selected key, without loading runtime settings."""
    cipher = derive_secret_cipher(secret)
    for store, query, kind in _STORES:
        for (raw,) in db.execute(query):
            try:
                for value in _credential_values(raw, kind):
                    if value is None or value == "":
                        continue
                    if not isinstance(value, str) or not is_encrypted(value):
                        report.block("credential_not_encrypted")
                        report.credential_stores_with_errors.add(store)
                        continue
                    try:
                        cipher.decrypt(value.removeprefix("enc:").encode()).decode()
                    except (InvalidToken, UnicodeError):
                        report.block("credential_not_decryptable")
                        report.credential_stores_with_errors.add(store)
                    else:
                        report.credentials_checked += 1
            except (ValueError, RecursionError):
                report.block("invalid_credential_structure")
                report.credential_stores_with_errors.add(store)
    report.key_evidence = (
        "failed"
        if report.credential_stores_with_errors
        else "verified"
        if report.credentials_checked
        else "no_ciphertext"
    )


def _maps(entries: list[str]) -> dict[int, Path]:
    roots: dict[int, Path] = {}
    for entry in entries:
        key, sep, value = entry.partition("=")
        if not sep or not key.isascii() or not key.isdecimal() or len(key) > 19:
            raise AssetVerificationError("invalid_root_mapping")
        path = Path(value)
        if (
            int(key) in roots
            or not path.is_absolute()
            or ".." in path.parts
            or path.resolve() == Path("/")
            or "\0" in value
        ):
            raise AssetVerificationError("invalid_root_mapping")
        roots[int(key)] = path
    return roots


def _files(
    db: sqlite3.Connection,
    mappings: dict[int, Path],
    report: AssetReport,
    *,
    all_files: bool,
    compare: str | None,
    covers_dir: Path | None,
    legacy_covers_dir: Path | None,
) -> None:
    fingerprint = hashlib.sha256(
        f"pullbox-media-sample-v2:{report.snapshot_sha256}:{all_files}\n".encode()
    )
    complete = True
    roots = dict(db.execute("SELECT id,path FROM library_roots"))
    report.root_files = dict.fromkeys(roots, 0)
    report.root_files.update(
        db.execute("SELECT library_root_id,count(*) FROM library_files GROUP BY library_root_id")
    )
    if mappings.keys() - roots.keys():
        report.block("unknown_root_mapping")
        complete = False
    records = db.execute(
        "SELECT library_root_id,id,file_path,file_size,file_hash,source_signature "
        "FROM library_files ORDER BY library_root_id,id"
    )
    for root_id, rows in groupby(records, key=itemgetter(0)):
        path, recorded = mappings.get(root_id), roots[root_id]
        if path is None:
            report.block("missing_root_mapping")
            report.root_problems[root_id] = "missing_root_mapping"
            complete = False
            continue
        try:
            with ExitStack() as handles:
                fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                handles.callback(os.close, fd)
                if os.path.samestat(os.fstat(fd), os.stat("/")):
                    raise AssetVerificationError("invalid_root_mapping")
                last = report.root_files[root_id] - 1
                sample_indices = {0, last // 2, last}
                for index, (_, file_id, file_path, size, saved, sig) in enumerate(rows):
                    try:
                        observed, matched = inspect_file(
                            fd,
                            recorded,
                            file_path,
                            size,
                            saved,
                            sig,
                            sample=all_files or index in sample_indices,
                        )
                    except (AssetVerificationError, ValueError, TypeError, RecursionError) as exc:
                        report.block(
                            str(exc)
                            if isinstance(exc, AssetVerificationError)
                            else "invalid_file_evidence",
                            file_id,
                        )
                        complete = False
                        continue
                    report.files_checked += 1
                    if observed is not None:
                        fingerprint.update(f"{root_id}:{file_id}:{size}:{observed}\n".encode())
                        report.files_hashed += 1
                        report.saved_hashes_matched += int(matched)
                if not os.path.samestat(os.stat(path, follow_symlinks=False), os.fstat(fd)):
                    raise AssetVerificationError("mapped_root_changed")
        except (OSError, AssetVerificationError) as exc:
            reason = str(exc) if isinstance(exc, AssetVerificationError) else "root_unavailable"
            report.block(reason)
            report.root_problems[root_id] = reason
            complete = False
    with ExitStack() as handles:
        mounts = MediaMounts(handles, roots, mappings, report.root_problems)
        verify_placements(db, mounts, report, fingerprint.update)
        verify_covers(db, mounts, report, fingerprint.update, covers_dir, legacy_covers_dir)
        mounts.recheck()
    if (
        complete
        and not report.blockers
        and (
            report.files_hashed or report.placements_checked or report.covers_checked or covers_dir
        )
    ):
        report.sample_sha256 = fingerprint.hexdigest()
    if compare is not None and complete and not report.blockers:
        if report.sample_sha256 != compare.lower():
            report.block("sample_mismatch")
        else:
            report.sample_compared = True


@contextmanager
def checked_snapshot(
    path: Path, expected: str, configured_url: str
) -> Iterator[tuple[sqlite3.Connection, PreflightReport]]:
    if not hex_digest(expected):
        raise AssetVerificationError("invalid_fingerprint")
    with database_snapshot(path, configured_url) as (snapshot, _):
        with snapshot.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected.lower():
                raise AssetVerificationError("snapshot_mismatch")
        with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 32 * 1024 * 1024)
            db.execute("BEGIN")
            checks = PreflightReport()
            inspect_snapshot_connection(db, checks)
            yield db, checks


def check_assets(
    path: Path,
    snapshot_sha256: str,
    root_args: list[str],
    *,
    compare: str | None,
    all_files: bool,
    configured_url: str,
    config_path: Path,
    covers_dir: Path | None = None,
    legacy_covers_dir: Path | None = None,
) -> AssetReport:
    report = AssetReport(hash_scope="all" if all_files else "sample")
    try:
        if compare is not None and not hex_digest(compare):
            raise AssetVerificationError("invalid_fingerprint")
        with checked_snapshot(path, snapshot_sha256, configured_url) as (db, checks):
            report.snapshot_sha256 = snapshot_sha256.lower()
            if checks.blockers:
                for code in checks.blockers:
                    report.block(code)
            else:
                _credentials(db, config_path, report)
                _files(
                    db,
                    _maps(root_args),
                    report,
                    all_files=all_files,
                    compare=compare,
                    covers_dir=covers_dir,
                    legacy_covers_dir=legacy_covers_dir,
                )
    except (BackupInputError, AssetVerificationError) as exc:
        report.block(str(exc))
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        report.block("asset_check_unavailable")
    report.checks_passed = not report.blockers
    if not report.checks_passed:
        # Never issue a usable baseline from an interrupted or otherwise failed run.
        report.sample_sha256 = None
        report.sample_compared = False
    return report
