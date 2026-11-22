"""Recover post-migration writes with native tools, without starting Pullbox."""

import hashlib
import sqlite3
import subprocess
from contextlib import closing
from tempfile import NamedTemporaryFile

import pytest

from pullbox.core.encryption import derive_secret_cipher
from tests.integration import test_database_migration_target as targets
from tests.integration import test_postgres_stage_review as review
from tests.integration import test_postgres_transfer_stage as stages

backup = stages.backup
populated = review.populated


def contents(sql, database):
    # Compare table names, unordered row contents and sequence state, not dump bytes.
    return sql(
        database,
        r"""
        SELECT format(
          'SELECT %L, count(*), md5(coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t))::text, %L))
           FROM %I.%I t',
          schemaname || '.' || tablename, '[]', schemaname, tablename)
        FROM pg_tables WHERE schemaname IN ('public','pullbox_stage') ORDER BY 1
        \gexec
        SELECT format('SELECT %L, last_value, is_called FROM %I.%I',
                      sequencename, schemaname, sequencename)
        FROM pg_sequences WHERE schemaname = 'public' ORDER BY 1
        \gexec
    """,
    )


@pytest.mark.parametrize("major", [16, 18])
def test_native_restore_preserves_new_writes_without_touching_offline_stage(
    major, populated, tmp_path
):
    with targets.disposable_server(major) as (container, server):
        staged, url, sql = stages.target(server)
        code, report = stages.stage(populated, url)
        assert code == 0, report
        original = hashlib.sha256(populated.read_bytes()).digest()
        # Only the disposable clone receives writes. The staging target remains sealed.
        sql("postgres", f"CREATE DATABASE library OWNER probe_owner TEMPLATE {staged}")
        cipher = derive_secret_cipher("7b" * 32)
        token = cipher.encrypt(b"post-migration credential").decode("ascii")
        sql(
            "library",
            f"""
            INSERT INTO system_config(key,value,value_type)
            VALUES ('native_restore_probe','enc:{token}','secret');
            INSERT INTO archive_metadata_publications(operation_id,revision,state,plan_json)
            VALUES ('post-migration-write',1,'settled','{{}}');
        """,
        )
        assert sql("library", "SELECT count(*) FROM issue_story_arcs WHERE issue_id=83") == "1"
        assert sql("library", "SELECT count(*) FROM import_files") == "140"
        before = contents(sql, "library")
        assert "public.issues|1|" in before and "public.archive_metadata_publications|1|" in before
        cmd = ["docker", "exec", "-i", container]
        with NamedTemporaryFile(dir=tmp_path) as dump:
            subprocess.run(
                [*cmd, "pg_dump", "-U", "probe_owner", "-Fc", "-d", "library"],
                stdout=dump,
                check=True,
                timeout=60,
            )
            for name, truncated in (("broken_copy", True), ("restored_copy", False)):
                sql("postgres", f"CREATE DATABASE {name} OWNER probe_owner TEMPLATE template0")
                dump.seek(0)
                restore = [
                    *cmd,
                    "pg_restore",
                    "-U",
                    "probe_owner",
                    "--single-transaction",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                    "-d",
                    name,
                ]
                if truncated:
                    size = dump.seek(0, 2)
                    dump.seek(0)
                    bad = subprocess.run(
                        restore, input=dump.read(3 * size // 4), capture_output=True, timeout=60
                    )
                    assert bad.returncode != 0
                    assert (
                        sql(name, "SELECT count(*) FROM pg_tables WHERE schemaname='public'") == "0"
                    )
                else:
                    subprocess.run(restore, stdin=dump, check=True, timeout=60)
        assert contents(sql, "restored_copy") == before == contents(sql, "library")
        saved = sql(
            "restored_copy", "SELECT value FROM system_config WHERE key='native_restore_probe'"
        )
        assert cipher.decrypt(saved.removeprefix("enc:").encode()) == b"post-migration credential"
        assert (
            sql(
                "restored_copy", "INSERT INTO story_arcs(name) VALUES('New arc') RETURNING id"
            ).splitlines()[0]
            == "877"
        )
        assert stages.state(sql, staged) == "false:pullbox:offline-stage:rows-verified"
        assert hashlib.sha256(populated.read_bytes()).digest() == original
        with closing(sqlite3.connect(f"{populated.as_uri()}?mode=ro", uri=True)) as source:
            assert source.execute(
                "SELECT count(*) FROM archive_metadata_publications"
            ).fetchone() == (0,)
