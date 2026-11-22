"""An operator can examine a transferred library without starting Pullbox."""

import hashlib
import io
import json
import os
import select
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from uuid import uuid4

import pytest

from pullbox.services.auth_service import AuthService
from tests.integration import test_postgres_transfer_stage as stages

backup = stages.backup
server = stages.server


@pytest.fixture
def populated(backup):
    with closing(sqlite3.connect(backup)) as db, db:
        db.execute(
            "INSERT INTO users(username,password_hash,is_active) VALUES(?,?,true)",
            ("reader", AuthService.hash_password("test-passphrase")),
        )
        db.execute("INSERT INTO story_arcs(id,name) VALUES(55,'Private arc')")
        db.execute(
            "INSERT INTO issue_story_arcs(story_arc_id,issue_id,sequence_number,source_ordinal) "
            "VALUES(55,83,1,0)"
        )
        db.execute(
            "UPDATE metadata_source_configs SET enabled=true,priority=3,domain_priorities=? "
            "WHERE source='metron_api'",
            ('{"issues":2}',),
        )
        db.execute("UPDATE import_files SET status='FAILED' WHERE id=2")
        db.execute("UPDATE series SET title=? WHERE id=71", ("Private title\x1b[2J",))
    return backup


def hook_terminal(monkeypatch, passwords, commands):
    import postgres_stage_review as review

    display = io.StringIO()
    answers = iter(commands)
    passwords = iter(passwords)
    monkeypatch.setattr(review, "_credentials", lambda: ("reader", next(passwords)))
    monkeypatch.setattr(review, "_answer", lambda: next(answers))
    monkeypatch.setattr(review, "_display", lambda message: print(message, file=display))
    return display


async def test_login_browse_and_confirm_keeps_copy_closed(server, populated, monkeypatch):
    monkeypatch.syspath_prepend(str(stages.ROOT / "scripts"))
    import postgres_stage_review as review

    from scripts import stage_postgres_transfer as staging

    original = review.review_library
    attempted = []

    def reject_external(*args):
        attempted.append(True)
        raise AssertionError("The sealed review must reuse the open connection.")

    async def disconnected(conn, digest, report):
        with monkeypatch.context() as patch:
            patch.setattr(socket.socket, "connect", reject_external)
            patch.setattr(socket.socket, "connect_ex", reject_external)
            patch.setattr(socket, "getaddrinfo", reject_external)
            await original(conn, digest, report)

    monkeypatch.setattr(review, "review_library", disconnected)
    db, url, sql = stages.target(server)
    before = hashlib.sha256(populated.read_bytes()).digest()
    display = hook_terminal(
        monkeypatch, ["not the password", "test-passphrase"], ["next", "confirm"]
    )
    report = {"cutover_ready": False}
    await staging.stage(populated, url, report, review=True)
    assert not attempted
    assert report["rows_verified"] and not report["cutover_ready"]
    assert report["maintenance_review"] == {
        "status": "confirmed",
        "series": 2,
        "issues": 1,
        "library_files": 0,
        "story_arcs": 1,
        "memberships": 1,
        "follow_up_jobs": 1,
        "metadata_sources": 5,
    }
    rendered = display.getvalue()
    for item in (
        "Private title",
        "Private arc",
        "0.5",
        "metron_api",
        '"domain_priorities": {"issues": 2}',
        "follow_up_jobs",
        "file_statuses",
        '"failed": 1',
        "series_statuses",
        "issue_number",
    ):
        assert item in rendered
    assert "\x1b" not in rendered and "private token" not in rendered
    assert "Private" not in json.dumps(report) and "test-passphrase" not in rendered
    assert hashlib.sha256(populated.read_bytes()).digest() == before
    assert stages.state(sql, db) == "false:pullbox:offline-stage:rows-verified"
    stages.bootstrap_is_blocked(url)
    clone = "inspect_" + uuid4().hex
    sql("postgres", f"CREATE DATABASE {clone} TEMPLATE {db}")
    assert sql(clone, "SELECT count(*) FROM users WHERE last_login_at IS NOT NULL") == "0"


async def test_review_cannot_write_even_if_a_reader_attempts_it(server, populated, monkeypatch):
    monkeypatch.syspath_prepend(str(stages.ROOT / "scripts"))
    import postgres_stage_review as review
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from scripts import stage_postgres_transfer as staging

    db, url, sql = stages.target(server)
    hook_terminal(monkeypatch, ["test-passphrase"], ["confirm"])
    observed = []

    async def attempt_write(session, page):
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
        assert stages.state(sql, db) == "false:pullbox:offline-stage:rows-verified"
        try:
            await session.execute(text("UPDATE users SET is_active=false"))
        except DBAPIError as exc:
            observed.append(exc.orig.sqlstate)
            raise

    monkeypatch.setattr(review, "_page", attempt_write)
    report = {}
    await staging.stage(populated, url, report, review=True)
    assert observed == ["25006"]
    assert (
        report["rows_verified"] and report["maintenance_review"]["status"] == "review_unavailable"
    )
    clone = "nowrites_" + uuid4().hex
    sql("postgres", f"CREATE DATABASE {clone} TEMPLATE {db}")
    assert sql(clone, "SELECT is_active::text FROM users") == "true"


@pytest.mark.parametrize("scenario", ["bad_login", "cancel", "disabled", "invalid_policy"])
async def test_incomplete_review_is_not_data_loss(server, populated, monkeypatch, scenario):
    monkeypatch.syspath_prepend(str(stages.ROOT / "scripts"))
    from scripts import stage_postgres_transfer as staging

    if scenario == "disabled":
        with closing(sqlite3.connect(populated)) as conn, conn:
            conn.execute("UPDATE users SET is_active=false")
    if scenario == "invalid_policy":
        with closing(sqlite3.connect(populated)) as conn, conn:
            conn.execute(
                """UPDATE metadata_source_configs SET settings='{"private_bad_key":"secret"}'
                WHERE source='metron_api'"""
            )
    db, url, sql = stages.target(server)
    password = "wrong" if scenario == "bad_login" else "test-passphrase"
    display = hook_terminal(monkeypatch, [password] * 3, ["quit"])
    report = {}
    await staging.stage(populated, url, report, review=True)
    expected = {"cancel": "cancelled", "invalid_policy": "provider_configuration_invalid"}.get(
        scenario, "login_failed"
    )
    assert report["maintenance_review"]["status"] == expected
    assert report["rows_verified"] and report["blockers"]
    if scenario in {"bad_login", "disabled"}:
        assert "Private" not in display.getvalue()
    assert "private_bad_key" not in display.getvalue() + json.dumps(report)
    assert stages.state(sql, db) == "false:pullbox:offline-stage:rows-verified"


async def test_review_refuses_another_snapshot_before_asking_for_login(
    server, populated, monkeypatch
):
    monkeypatch.syspath_prepend(str(stages.ROOT / "scripts"))
    import postgres_stage_review as review

    from scripts import stage_postgres_transfer as staging

    db, url, sql = stages.target(server)
    original = review.review_library
    display = hook_terminal(monkeypatch, [], [])

    async def different_digest(conn, digest, report):
        await original(conn, "0" * 64, report)

    monkeypatch.setattr(review, "review_library", different_digest)
    report = {}
    await staging.stage(populated, url, report, review=True)
    assert report["maintenance_review"]["status"] == "stage_not_verified"
    assert report["rows_verified"] and not display.getvalue()
    assert stages.state(sql, db) == "false:pullbox:offline-stage:rows-verified"


def test_interactive_review_refuses_redirected_input_before_staging(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/stage_postgres_transfer.py",
            "--create-offline-stage",
            "--backup",
            str(tmp_path / "unused.db"),
            "--url-stdin",
            "--review-library",
        ],
        cwd=stages.ROOT,
        input="private target\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    report = json.loads(proc.stdout)
    assert "review_requires_terminal" in report["blockers"]
    assert not report["target_closed"] and "private target" not in proc.stdout + proc.stderr


def test_operator_terminal_paginates_and_exports_only_redacted_report(server, populated):
    pty = pytest.importorskip("pty")
    termios = pytest.importorskip("termios")
    db, url, sql = stages.target(server)
    with closing(sqlite3.connect(populated)) as conn, conn:
        conn.executemany(
            "INSERT INTO series(id,title,sort_title,status,issue_count,monitored) "
            "VALUES(?,?,'series','ENDED',0,false)",
            [(n, f"Private example {n}") for n in range(100, 125)],
        )
    master, slave = pty.openpty()
    try:
        with subprocess.Popen(
            [
                sys.executable,
                "scripts/stage_postgres_transfer.py",
                "--create-offline-stage",
                "--backup",
                str(populated),
                "--review-library",
            ],
            cwd=stages.ROOT,
            stdin=slave,
            stderr=slave,
            stdout=subprocess.PIPE,
            start_new_session=True,
            env={**os.environ, "PULLBOX_DB_URL": "sqlite:////not-the-running-db"},
        ) as proc:
            try:
                transcript = bytearray()
                pos = 0
                deadline = time.monotonic() + 90
                for prompt, answer, hidden in [
                    (b"target URL: ", url, True),
                    (b"username (hidden): ", "reader", True),
                    (b"password (hidden): ", "test-passphrase", True),
                    (b"or quit:", "next", False),
                    (b"or quit:", "confirm", False),
                ]:
                    while prompt not in transcript[pos:]:
                        assert time.monotonic() < deadline and proc.poll() is None
                        if select.select([master], [], [], 0.5)[0]:
                            transcript.extend(os.read(master, 65536))
                    pos = transcript.index(prompt, pos) + len(prompt)
                    if hidden:
                        assert not termios.tcgetattr(slave)[3] & termios.ECHO
                    os.write(master, answer.encode() + b"\n")
                out, _ = proc.communicate(timeout=20)
            finally:
                if proc.poll() is None:
                    proc.kill()
            assert proc.returncode == 0
    finally:
        os.close(master)
        os.close(slave)
    report = json.loads(out)
    assert report["maintenance_review"]["series"] == 27
    assert report["maintenance_review"]["status"] == "confirmed"
    assert b"Page 2 of 2" in transcript and b"Private example 124" in transcript
    assert b"Private" not in out and b"test-passphrase" not in transcript + out
    assert url.encode() not in transcript + out and b"private token" not in transcript + out
    assert stages.state(sql, db) == "false:pullbox:offline-stage:rows-verified"
