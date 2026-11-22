"""The approved runtime must verify the original stage before releasing normal startup."""

import hashlib
import secrets
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

from tests.integration import test_postgres_stage_review as review
from tests.integration import test_postgres_transfer_stage as stages

server = stages.server
backup = stages.backup
populated = review.populated


async def test_cutover_refuses_drift_then_releases_original_database(
    server, populated, tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(stages.ROOT / "scripts"))
    from pullbox.services import migration_cutover as cutover
    from pullbox.services.migration_first_start import BindingError, check_binding
    from scripts import stage_postgres_transfer as staging

    db, url, sql = stages.target(server)
    secret = secrets.token_hex(32)
    token = tmp_path / "token"
    token.write_text(secret)
    token.chmod(0o400)
    digest = hashlib.sha256(populated.read_bytes()).hexdigest()
    source = "ab" * 32
    review.hook_terminal(monkeypatch, ["test-passphrase"], ["confirm"])

    monkeypatch.setattr(
        staging.SourceCapture,
        "read",
        lambda _: SimpleNamespace(snapshot=digest, fingerprint=source),
    )
    await staging.stage(populated, url, {}, review=True, token_file=token, capture_report="fixture")
    binding = await check_binding(url, token, digest, source=source)
    original = stages.state(sql, db)
    checks = []

    async def confirm(challenge):
        assert await check_binding(url, token, digest, source=source, waiting=challenge) == binding
        checks.append(challenge)
        return cutover.sign_grant(bytes.fromhex(secret), binding, challenge)

    async def admit(callback):
        await cutover.admit_verified_stage(
            url, populated, token, digest, binding, int(time.time()) + 300, callback
        )

    def change(query):
        sql("postgres", f"ALTER DATABASE {db} ALLOW_CONNECTIONS true")
        sql(db, query)
        sql("postgres", f"ALTER DATABASE {db} ALLOW_CONNECTIONS false")

    with monkeypatch.context() as patch:
        connect = cutover._connect

        async def interrupt(stack, locator, session):
            if session == "pullbox_stage_inspection":
                stages.targets._migrate(
                    url, "upgrade", "head", expected_error="Offline migration stage"
                )
                raise OSError("inspection disconnected")
            return await connect(stack, locator, session)

        patch.setattr(cutover, "_connect", interrupt)
        with pytest.raises(OSError, match="disconnected"):
            await admit(confirm)
        assert stages.state(sql, db) == original

    change("UPDATE series SET title='drifted' WHERE id=71")
    with pytest.raises(BindingError, match="row_mismatch:series"):
        await admit(confirm)
    assert not checks and stages.state(sql, db) == original
    with closing(sqlite3.connect(populated)) as sqlite:
        title = sqlite.execute("SELECT title FROM series WHERE id=71").fetchone()[0]
    change(
        f"UPDATE series SET title='{title}' WHERE id=71; "
        "SELECT nextval(pg_get_serial_sequence('story_arcs','id'))"
    )
    with pytest.raises(BindingError, match="sequence_mismatch:story_arcs"):
        await admit(confirm)
    assert not checks and stages.state(sql, db) == original
    change("SELECT setval(pg_get_serial_sequence('story_arcs','id'),876,true)")
    change("ALTER TABLE story_arcs ALTER COLUMN id SET DEFAULT 1")
    with pytest.raises(BindingError, match="sequence_mismatch:story_arcs"):
        await admit(confirm)
    assert not checks and stages.state(sql, db) == original
    change("ALTER TABLE story_arcs ALTER COLUMN id SET DEFAULT nextval('story_arcs_id_seq')")
    sql("postgres", f"ALTER DATABASE {db} ALLOW_CONNECTIONS true")
    constraint, ddl = sql(
        db,
        "SELECT conname || '|' || pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid='issue_story_arcs'::regclass AND confrelid='issues'::regclass",
    ).split("|")
    change(f'ALTER TABLE issue_story_arcs DROP CONSTRAINT "{constraint}"')
    with pytest.raises(BindingError, match="foreign_keys:issue_story_arcs"):
        await admit(confirm)
    assert not checks and stages.state(sql, db) == original
    change(f'ALTER TABLE issue_story_arcs ADD CONSTRAINT "{constraint}" {ddl}')

    async def stale(challenge):
        return cutover.sign_grant(bytes.fromhex(secret), binding, "aa" * 16)

    with pytest.raises(BindingError, match="host"):
        await admit(stale)

    async def expired(challenge):
        return cutover.sign_grant(
            bytes.fromhex(secret), binding, challenge, issued=int(time.time()) - 61
        )

    with pytest.raises(BindingError, match="host"):
        await admit(expired)
    assert stages.state(sql, db) == original
    await admit(confirm)
    assert len(checks) == 1 and stages.state(sql, db).startswith("true:")
    stages.targets._migrate(url, "upgrade", "head")
    assert sql(db, "SELECT nextval(pg_get_serial_sequence('story_arcs','id'))") == "877"
    assert sql(
        db, "UPDATE series SET title='after migration' WHERE id=71 RETURNING title"
    ).startswith("after migration")
    with pytest.raises(BindingError):
        await admit(confirm)
