"""A preserved SQLite library needs its own recoverable deployment config and effective key."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from zipfile import ZipFile

import pytest

from pullbox.core.encryption import derive_secret_cipher
from tests.integration import test_postgres_transfer_rehearsal as rehearsals

backup = rehearsals.backup
KEY = "a8f2" * 16
OTHER = "bb91" * 16
SECRET = "private-synthetic-provider-token"


@pytest.fixture
def preserved(backup, tmp_path):
    encrypted = "enc:" + derive_secret_cipher(KEY).encrypt(SECRET.encode()).decode()
    with closing(sqlite3.connect(backup)) as db, db:
        db.execute(
            "INSERT INTO system_config(key,value,value_type) VALUES(?,?,'secret')",
            ("restore_probe", encrypted),
        )
    orig = tmp_path / "settings-original"
    orig.mkdir(mode=0o700)
    (orig / "compose.yaml").write_text("services:\n  pullbox:\n    image: local-test\n")
    (orig / ".env").write_text("EXAMPLE=private-synthetic-setting\n")
    (orig / "config.xml").write_text("<Config><SecretKey>" + KEY + "</SecretKey></Config>")
    # Restore actual archived bytes into a new directory, not links to the reference set.
    restored = tmp_path / "restored"
    restored.mkdir(mode=0o700)
    with ZipFile(tmp_path / "disposable-backup.zip", "w") as archive:
        archive.write(backup, "library.db")
        for f in orig.iterdir():
            archive.write(f, "settings/" + f.name)
    with ZipFile(tmp_path / "disposable-backup.zip") as archive:
        archive.extractall(restored)
    settings = restored / "settings"
    settings.chmod(0o700)
    return restored / "library.db", hashlib.sha256(backup.read_bytes()).hexdigest(), orig, settings


def check(preserved, source="config-xml", env=KEY, deployment=None):
    db, digest, original, restored = preserved
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pullbox.cli",
            "migration-check-restore",
            "--backup",
            str(db),
            "--snapshot-sha256",
            digest,
            "--original-settings",
            str(original),
            "--restored-settings",
            str(restored),
            "--key-source",
            source,
            *(["--deployment-file", deployment] if deployment else []),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "PULLBOX_SECRET_KEY": env,
            "PULLBOX_ALLOW_WEAK_SECRET_FOR_TESTS": "1",
            "PULLBOX_DATA_DIR": str(restored / "not-created"),
            "PULLBOX_DB_URL": "sqlite:////unused.db",
        },
    )
    assert not result.stderr
    assert all(
        secret not in result.stdout
        for secret in (KEY, OTHER, SECRET, "private-synthetic-setting", "enc:", str(db.parent))
    )
    assert not (restored / "not-created").exists()
    report = json.loads(result.stdout)
    assert result.returncode in (0, 1) and not report["cutover_ready"]
    assert report["restore_verified"] == (result.returncode == 0)
    return report


def test_restored_config_decrypts_exact_snapshot_without_ambient_key(preserved):
    db, digest, _, _ = preserved
    before = db.stat().st_mtime_ns
    report = check(preserved, env=OTHER)
    assert report["restore_verified"], report
    assert report["settings_files_compared"] == 3
    assert report["snapshot_sha256"] == digest
    assert report["credentials_checked"] == 1 and report["key_evidence"] == "verified"
    assert db.stat().st_mtime_ns == before and hashlib.sha256(db.read_bytes()).hexdigest() == digest


def test_escrowed_override_not_stale_xml_or_shell_key(preserved):
    for folder in preserved[2:]:
        (folder / "config.xml").write_text("<Config><SecretKey>" + OTHER + "</SecretKey></Config>")
        (folder / "application-key").write_text(KEY + "\n")
    report = check(preserved, source="secret-file", env=OTHER)
    assert report["restore_verified"] and report["credentials_checked"] == 1
    # Identical original and restored files do not prove that either contains the correct key.
    for folder in preserved[2:]:
        (folder / "application-key").write_text(OTHER)
    assert "credential_not_decryptable" in check(preserved, source="secret-file")["blockers"]


@pytest.mark.parametrize(
    "problem,reason",
    [
        ("missing-key", "configuration_incomplete"),
        ("changed", "configuration_mismatch"),
        ("extra", "configuration_mismatch"),
        ("symlink", "unsafe_configuration"),
        ("hardlink", "unsafe_configuration"),
        ("same", "unsafe_configuration"),
        ("digest", "snapshot_mismatch"),
        ("weak", "invalid_recovery_key"),
    ],
)
def test_unrestorable_inputs_block_without_disclosing_values(preserved, problem, reason):
    db, digest, orig, restored = preserved
    if problem == "missing-key":
        for folder in (orig, restored):
            (folder / "config.xml").unlink()
    elif problem == "changed":
        (restored / ".env").write_text("changed")
    elif problem == "extra":
        (restored / "extra-private-name").touch()
    elif problem in ("symlink", "hardlink"):
        (restored / ".env").unlink()
        if problem == "symlink":
            (restored / ".env").symlink_to(orig / ".env")
        else:
            os.link(orig / ".env", restored / ".env")
    elif problem == "same":
        preserved = (db, digest, orig, orig)
    elif problem == "digest":
        preserved = (db, "0" * 64, orig, restored)
    elif problem == "weak":
        for folder in (orig, restored):
            (folder / "config.xml").write_text("<Config><SecretKey>weak</SecretKey></Config>")
    report = check(preserved)
    assert reason in report["blockers"] and not report["restore_verified"]
    assert "extra-private-name" not in str(report)


def test_no_saved_credentials_does_not_claim_decryption_evidence(preserved):
    db, _, orig, restored = preserved
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("DELETE FROM system_config WHERE key='restore_probe'")
    report = check((db, hashlib.sha256(db.read_bytes()).hexdigest(), orig, restored))
    assert report["restore_verified"] and report["key_evidence"] == "no_ciphertext"
    assert report["credentials_checked"] == 0
    assert "no saved" in report["next_step"].lower()


def test_invalid_xml_encoding_does_not_leak_parser_input(preserved):
    for folder in preserved[2:]:
        (folder / "config.xml").write_bytes(
            b"<?xml version='1.0' encoding='private-synthetic-setting'?><Config/>"
        )
    assert "invalid_recovery_key" in check(preserved)["blockers"]


def test_nonstandard_nested_deployment_definition_and_unsafe_selector(preserved):
    for folder in preserved[2:]:
        (folder / "stack").mkdir()
        (folder / "compose.yaml").rename(folder / "stack" / "pullbox-prod.yml")
    assert check(preserved, deployment="stack/pullbox-prod.yml")["restore_verified"]
    assert "invalid_deployment_file" in check(preserved, deployment="../config.xml")["blockers"]


def test_packaged_runtime_can_read_the_restored_credentials_offline(preserved):
    image = os.environ.get("PULLBOX_TEST_SQLITE_SNAPSHOT_IMAGE")
    if not image or os.environ.get("PULLBOX_TEST_MIGRATION_TARGET_DOCKER") != "1":
        pytest.skip("requires an explicitly selected local Pullbox image and disposable Docker")
    db, _, _, _ = preserved
    assert check(preserved)["restore_verified"]
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--pull=never",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            f"type=bind,src={db.parent},dst=/restored,readonly",
            "-e",
            "PULLBOX_SECRET_KEY=",
            "-e",
            "PULLBOX_DATA_DIR=/restored/settings",
            "--entrypoint=python",
            image,
            "-B",
            "-c",
            """
import hashlib, sqlite3
from pathlib import Path
from pullbox.core.config_file import init_config_provider
from pullbox.core.encryption import decrypt_secret
init_config_provider(Path('/restored/settings'))
conn = sqlite3.connect('file:/restored/library.db?mode=ro&immutable=1', uri=True)
token, = conn.execute("SELECT value FROM system_config WHERE key='restore_probe'").fetchone()
assert hashlib.sha256(decrypt_secret(token).encode()).hexdigest() == __import__('sys').argv[1]
""",
            hashlib.sha256(SECRET.encode()).hexdigest(),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert SECRET not in result.stdout + result.stderr and KEY not in result.stdout + result.stderr
    assert result.returncode == 0, result.stderr
