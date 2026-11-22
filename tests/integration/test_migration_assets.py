"""Offline asset proof is bound to the staged snapshot, not the active application."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from pullbox.core.encryption import encrypt_secret
from tests.integration import test_database_migration_target as targets
from tests.integration import test_postgres_transfer_rehearsal as rehearsals
from tests.integration import test_postgres_transfer_stage as stages

backup = rehearsals.backup
server = targets.server


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@pytest.fixture
def assets(backup, tmp_path, monkeypatch):
    from pullbox.core import config_resolver, encryption

    monkeypatch.setattr(config_resolver, "get_application_secret", lambda: "ab7c" * 16)
    encryption._get_fernet.cache_clear()
    ciphertext = encrypt_secret("sample-provider-value-do-not-print")
    encryption._get_fernet.cache_clear()
    library = tmp_path / "mapped"
    library.mkdir()
    with closing(sqlite3.connect(backup)) as db, db:
        db.execute(
            "INSERT INTO system_config(key,value,value_type) "
            "VALUES('comicvine_api_key',?,'secret') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (ciphertext,),
        )
        db.execute(
            "INSERT INTO direct_provider_configs(provider_id,display_name,endpoint,"
            "encrypted_configuration,encrypted_bearer_token) VALUES('f','f',"
            "'https://fixture.invalid',?,?)",
            (json.dumps({"key": ciphertext}), ciphertext),
        )
        db.execute(
            "INSERT INTO library_roots(id,path,name,enabled) VALUES(77,'/comics','private',1)"
        )
        for n in range(1, 6):
            p = library / f"private-{n}.cbz"
            p.write_bytes(b"fictional archive" + bytes([n]))
            db.execute(
                "INSERT INTO library_files(id,library_root_id,file_path,file_name,file_size,"
                "file_format,file_modified_at,match_confidence,has_comicinfo,file_hash)"
                "VALUES(?,77,?,?,?,'CBZ',CURRENT_TIMESTAMP,'UNMATCHED',0,?)",
                (
                    400 + n,
                    "/comics/" + p.name,
                    p.name,
                    p.stat().st_size,
                    digest(p) if n == 1 else None,
                ),
            )
    return backup, library


def run(assets, *args, key="ab7c" * 16, snapshot=None, roots=True, config_dir=None):
    db, library = assets
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pullbox.cli",
            "migration-check-assets",
            "--backup",
            str(db),
            "--snapshot-sha256",
            snapshot or digest(db),
            *(["--root", "77=" + str(library)] if roots else []),
            *args,
        ],
        env={
            **os.environ,
            "PULLBOX_SECRET_KEY": key,
            "PULLBOX_DATA_DIR": str(config_dir or library.parent / "absent-config-dir"),
            "PULLBOX_DB_URL": "sqlite:////not-the-actual-database",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert not result.stderr
    assert not any(
        s in result.stdout
        for s in ("sample-provider-value-do-not-print", "private-", "enc:", str(library))
    )
    assert not (library.parent / "absent-config-dir").exists()
    return result.returncode, json.loads(result.stdout)


def test_uses_existing_configuration_without_regenerating_key(assets, tmp_path):
    config_dir = tmp_path / "existing"
    config_dir.mkdir()
    config = config_dir / "config.xml"
    config.write_text("<Config><SecretKey>" + ("ab7c" * 16) + "</SecretKey></Config>")
    before = digest(config), config.stat().st_mtime_ns
    code, report = run(assets, key="", config_dir=config_dir)
    assert code == 0 and report["key_evidence"] == "verified"
    assert before == (digest(config), config.stat().st_mtime_ns)


def test_capture_then_compare_without_changing_library_or_backup(assets):
    db, library = assets
    before = digest(db), [(digest(p), p.stat().st_mtime_ns) for p in sorted(library.iterdir())]
    code, report = run(assets)
    assert code == 0, report
    assert report["checks_passed"] and not report["cutover_ready"]
    assert report["credentials_checked"] == 3
    assert report["files_checked"] == 5 and report["files_hashed"] == 3
    assert report["saved_hashes_matched"] == 1 and not report["sample_compared"]
    code, second = run(assets, "--compare-sample", report["sample_sha256"])
    assert code == 0 and second["sample_compared"]
    assert (
        digest(db),
        [(digest(p), p.stat().st_mtime_ns) for p in sorted(library.iterdir())],
    ) == before


def test_wrong_key_and_plaintext_are_not_reported_as_verified(assets):
    code, report = run(assets, key="fa14" * 16)
    assert code == 1 and report["blockers"]["credential_not_decryptable"] == 3
    db, _ = assets
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE system_config SET value='legacy-token' WHERE key='comicvine_api_key'")
    code, report = run(assets)
    assert code == 1 and "credential_not_encrypted" in report["blockers"]
    assert "legacy-token" not in str(report)


def test_verifies_other_durable_credential_fields_without_network(assets):
    with closing(sqlite3.connect(assets[0])) as conn, conn:
        token = conn.execute(
            "SELECT encrypted_bearer_token FROM direct_provider_configs"
        ).fetchone()[0]
        mapping = json.dumps({"test": token})
        conn.execute(
            "INSERT INTO metadata_source_configs(source,enabled,priority,revision,settings,"
            "domain_priorities,credential_secret) VALUES('metron_api',0,3,1,'{}','{}',?) "
            "ON CONFLICT(source) DO UPDATE SET credential_secret=excluded.credential_secret",
            (token,),
        )
        conn.execute(
            "INSERT INTO indexer_configs(name,indexer_type,url,api_key,enabled,priority,"
            "source,enable_rss,enable_automatic_search,enable_interactive_search,failure_count)"
            " VALUES('test','TORZNAB','https://fixture.invalid',?,0,1,'manual',0,0,0,0)",
            (token,),
        )
        conn.execute(
            "INSERT INTO download_client_configs(name,client_type,url,enabled,priority,api_key,"
            "password) VALUES('test','SABNZBD','https://fixture.invalid',0,1,?,?)",
            (token, token),
        )
        conn.execute(
            "INSERT INTO direct_resolver_configs(encrypted_auth_headers) VALUES(?) "
            "ON CONFLICT(resolver_kind) DO UPDATE SET "
            "encrypted_auth_headers=excluded.encrypted_auth_headers",
            (mapping,),
        )
        conn.execute(
            "INSERT INTO direct_host_configs(host_kind,encrypted_credentials) VALUES('mega',?) "
            "ON CONFLICT(host_kind) DO UPDATE SET "
            "encrypted_credentials=excluded.encrypted_credentials",
            (mapping,),
        )
        conn.execute(
            "INSERT INTO pending_matches(issue_id,release_title,download_url,is_torrent,"
            "confidence,match_details,status) VALUES(83,'fixture','https://fixture.invalid',0,"
            "'low',?,'expired')",
            (json.dumps({"dc_route_snapshot": token}),),
        )
    code, report = run(assets)
    assert code == 0, report
    assert report["credentials_checked"] == 10


def test_sample_detects_wrong_same_size_comic_without_saved_digest(assets):
    code, before = run(assets)
    assert code == 0
    p = assets[1] / "private-5.cbz"
    p.write_bytes(b"z" * p.stat().st_size)
    code, report = run(assets, "--compare-sample", before["sample_sha256"])
    assert code == 1 and "sample_mismatch" in report["blockers"]


def test_missing_and_symlink_files_are_not_followed(assets):
    _, library = assets
    (library / "private-5.cbz").unlink()
    p = library / "private-3.cbz"
    p.unlink()
    p.symlink_to(library / "private-2.cbz")
    code, report = run(assets)
    assert code == 1
    assert report["blockers"]["file_missing"] == 1 and report["blockers"]["unsafe_path"] == 1
    assert set(report["file_examples"]["unsafe_path"]) == {403}
    assert not report["sample_sha256"]


def test_refuses_wrong_snapshot_before_asset_access(assets):
    code, report = run(assets, key="bad", snapshot="0" * 64, roots=False)
    assert code == 1 and list(report["blockers"]) == ["snapshot_mismatch"]
    assert not report["files_checked"] and not report["credentials_checked"]


def test_pending_publication_blocks_before_keys_or_comics_are_read(assets):
    with closing(sqlite3.connect(assets[0])) as conn, conn:
        conn.execute(
            "INSERT INTO archive_metadata_publications"
            "(operation_id,active_path_key,revision,state,plan_json) "
            "VALUES('fixture','test',1,'review','{}')"
        )
    code, report = run(assets, key="", roots=False)
    assert code == 1 and list(report["blockers"]) == ["archive_metadata_publications"]
    assert report["key_evidence"] == "not_checked" and report["files_checked"] == 0


def test_unsampled_missing_file_and_missing_key_both_block(assets):
    (assets[1] / "private-2.cbz").unlink()
    code, report = run(assets, key="")
    assert code == 1 and report["blockers"] == {"application_key_unavailable": 1, "file_missing": 1}
    assert report["file_examples"]["file_missing"] == [402]


def test_full_comparison_and_saved_placement_hash(assets):
    db, library = assets
    p = library / "private-5.cbz"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "UPDATE library_files SET source_signature=? WHERE id=405",
            (json.dumps({"content_digest_algorithm": "sha256", "content_digest": digest(p)}),),
        )
    code, report = run(assets, "--all-files")
    assert code == 0 and report["files_hashed"] == 5 and report["saved_hashes_matched"] == 2
    p.write_bytes(b"w" * p.stat().st_size)
    code, report = run(assets, "--all-files")
    assert code == 1 and report["file_examples"]["file_hash_mismatch"] == [405]


def test_backup_paths_cannot_select_host_directories(assets, tmp_path):
    db, library = assets
    (library / "sibling").symlink_to(tmp_path, target_is_directory=True)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "UPDATE library_files SET file_path='/comics/sibling/private.cbz' WHERE id=401"
        )
        conn.execute(
            "UPDATE library_files SET file_path='/comics/../elsewhere/item.cbz' WHERE id=402"
        )
        conn.execute("UPDATE library_files SET file_path='/comics-extra/item.cbz' WHERE id=403")
    code, report = run(assets)
    assert code == 1
    assert report["file_examples"]["unsafe_path"] == [401]
    assert report["file_examples"]["outside_library_root"] == [402, 403]
    code, report = run(assets, roots=False)
    assert code == 1 and report["root_files"]["77"] == 5
    assert report["blockers"]["missing_root_mapping"] == 1 and report["files_checked"] == 0


def test_offline_check_uses_committed_stage_fingerprint(server, assets, tmp_path):
    from pullbox.services.backup_service import DB_FILENAME, METADATA_FILENAME

    db, library = assets
    name, url, sql = stages.target(server)
    code, stage = stages.stage(db, url)
    assert code == 0 and stage["rows_verified"]
    archive = tmp_path / "fixture.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as z:
        z.write(db, DB_FILENAME)
        z.writestr(
            METADATA_FILENAME,
            json.dumps({"pullbox_version": "2.0.0-dev", "db_size_bytes": db.stat().st_size}),
        )
    code, original = run((archive, library), snapshot=stage["snapshot_sha256"])
    assert code == 0 and original["key_evidence"] == "verified"
    other_mount = shutil.copytree(library, tmp_path / "after")
    code, checked = run(
        (archive, other_mount),
        "--compare-sample",
        original["sample_sha256"],
        snapshot=stage["snapshot_sha256"],
    )
    assert code == 0 and checked["sample_compared"] and not checked["cutover_ready"]
    assert stages.state(sql, name) == "false:pullbox:offline-stage:rows-verified"
    stages.bootstrap_is_blocked(url)


def test_empty_credential_store_does_not_prove_key(assets):
    db, _ = assets
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("DELETE FROM direct_provider_configs")
        conn.execute("UPDATE system_config SET value='' WHERE value_type='secret'")
    code, report = run(assets, key="fa14" * 16)
    assert code == 0 and report["key_evidence"] == "no_ciphertext"
    assert report["credentials_checked"] == 0 and not report["cutover_ready"]


def test_changed_file_during_read_is_never_certified(assets, monkeypatch):
    from pullbox.services import migration_asset_files as files

    _, root = assets
    path = root / "private-1.cbz"
    read_hash = files._read_hash

    def replace_after_read(fd, size):
        value = read_hash(fd, size)
        # Same content and size, but the path no longer names the descriptor inspected.
        replacement = root / "replacement.cbz"
        shutil.copyfile(path, replacement)
        replacement.replace(path)
        return value

    monkeypatch.setattr(files, "_read_hash", replace_after_read)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(files.AssetVerificationError, match="file_changed"):
            files.inspect_file(
                fd,
                "/comics",
                "/comics/" + path.name,
                path.stat().st_size,
                digest(path),
                "{}",
                sample=True,
            )
    finally:
        os.close(fd)


def test_malformed_secret_mapping_and_conflicting_hashes_block(assets):
    db, _ = assets
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "UPDATE direct_provider_configs SET encrypted_configuration=?",
            (json.dumps({"key": None}),),
        )
        conn.execute(
            "UPDATE library_files SET source_signature=? WHERE id=401",
            (json.dumps({"content_digest_algorithm": "sha256", "content_digest": "0" * 64}),),
        )
    code, report = run(assets)
    assert code == 1 and report["credential_stores_with_errors"] == ["direct_providers"]
    assert report["blockers"] == {"invalid_credential_structure": 1, "conflicting_file_hashes": 1}
    assert not report["sample_sha256"]


def test_referenced_files_in_disabled_root_still_require_the_correct_mount(assets, tmp_path):
    db, _ = assets
    second = tmp_path / "old-library"
    second.mkdir()
    p = second / "reference.cbz"
    p.write_bytes(b"another fixture")
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO library_roots(id,path,name,enabled) VALUES(78,'/old','old',0)")
        conn.execute(
            "INSERT INTO library_files(id,library_root_id,file_path,file_name,file_size,"
            "file_format,file_modified_at,match_confidence,has_comicinfo,file_hash,storage_mode) "
            "VALUES(506,78,'/old/reference.cbz','reference.cbz',?,'CBZ',CURRENT_TIMESTAMP,"
            "'UNMATCHED',0,?,'referenced')",
            (p.stat().st_size, digest(p)),
        )
    code, report = run(assets)
    assert code == 1 and report["root_problems"] == {"78": "missing_root_mapping"}
    code, report = run(assets, "--root", "78=" + str(second))
    assert code == 0 and report["files_checked"] == 6 and report["files_hashed"] == 4
    assert report["saved_hashes_matched"] == 2
