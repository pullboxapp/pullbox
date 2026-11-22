"""Read-only target checks and fresh-install rehearsals on disposable databases."""

import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest


def check(url):
    run = subprocess.run(
        [sys.executable, "-m", "pullbox.cli", "migration-check-target", "--url-stdin", "--json"],
        input=url + "\n",
        capture_output=True,
        text=True,
        timeout=20,
        env={**os.environ, "PULLBOX_DB_URL": "sqlite:////do-not-use/live.db"},
    )
    assert run.returncode in {0, 1}, "The CLI does not provide a target check"
    assert "postgresql+" not in run.stdout + run.stderr and not run.stderr
    return run.returncode, json.loads(run.stdout)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "sqlite:////data/pullbox.db",
        "postgresql+asyncpg://localhost/postgres",
        "postgresql+asyncpg://user@host/template1",
        "postgresql+asyncpg://user@remote.example/db?ssl=disable",
        "postgresql+asyncpg://user@localhost/db?host=elsewhere",
        "postgresql+asyncpg://user@localhost/",
        "postgresql+asyncpg:///db",
    ],
)
def test_declines_implicit_unsafe_or_non_target_urls(url):
    code, report = check(url)
    assert code == 1 and "target_url" in report["blockers"]
    assert not report["target_checks_passed"] and not report["cutover_ready"]


def test_unreachable_target_is_bounded_and_redacted():
    code, report = check("postgresql+asyncpg://private:DO_NOT_DISCLOSE@127.0.0.1:1/private_db")
    assert code == 1 and "connection" in report["blockers"]
    assert "DO_NOT_DISCLOSE" not in str(report) and "private" not in str(report)


@pytest.fixture(scope="module", params=[16, 18])
def server(request):
    if os.environ.get("PULLBOX_TEST_MIGRATION_TARGET_DOCKER") != "1":
        pytest.skip("opt in to disposable Docker PostgreSQL 16 and 18")
    major = request.param
    password = secrets.token_hex(20)
    container = "pullbox-target-probe-" + uuid4().hex

    def docker(*args, **kwargs):
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=45, **kwargs
        )

    mount = "/var/lib/postgresql/data" if major == 16 else "/var/lib/postgresql"
    docker(
        "run",
        "--rm",
        "--detach",
        "--pull=never",
        "--name",
        container,
        "--tmpfs",
        mount,
        "--publish",
        "127.0.0.1::5432",
        "--env",
        "POSTGRES_PASSWORD",
        f"postgres:{major}-bookworm",
        env={**os.environ, "POSTGRES_PASSWORD": password},
        check=True,
    )
    try:
        for _ in range(100):
            if not docker(
                "exec", container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"
            ).returncode:
                break
            time.sleep(0.15)
        else:
            pytest.fail("Disposable database did not become ready")

        def sql(database, command):
            return docker(
                "exec",
                "-i",
                container,
                "psql",
                "-X",
                "-A",
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                "-d",
                database,
                input=command,
                check=True,
            ).stdout.strip()

        sql("postgres", f"CREATE ROLE probe_owner LOGIN PASSWORD '{password}';")
        port = int(docker("port", container, "5432/tcp", check=True).stdout.strip().split(":")[-1])
        yield major, password, port, sql
    finally:
        docker("rm", "-f", container, check=True)


@pytest.mark.parametrize(
    ("setup", "blocker"),
    [
        ("", None),
        (
            "CREATE TABLE public.other_library(id int); "
            "INSERT INTO public.other_library VALUES (42)",
            "target_not_empty",
        ),
        ("CREATE SCHEMA elsewhere", "target_not_empty"),
        ("CREATE TYPE public.marker AS ENUM ('x')", "target_not_empty"),
        ("SELECT lo_create(1234)", "target_not_empty"),
        ("GRANT CREATE ON SCHEMA public TO PUBLIC", "target_shared"),
        ("ALTER SCHEMA public OWNER TO postgres", "target_ownership"),
        ("ALTER DATABASE {db} OWNER TO postgres", "target_ownership"),
        ("ALTER DATABASE {db} SET default_transaction_read_only = on", "target_read_only"),
    ],
)
def test_dedicated_target(server, setup, blocker):
    major, password, port, sql = server
    database = "probe_" + uuid4().hex
    sql("postgres", f"CREATE DATABASE {database} OWNER probe_owner TEMPLATE template0")
    if setup:
        sql(database, setup.format(db=database))
    code, report = check(f"postgresql+asyncpg://probe_owner:{password}@127.0.0.1:{port}/{database}")
    assert report["server_major"] == major and not report["cutover_ready"]
    assert password not in str(report) and database not in str(report)
    assert code == int(blocker is not None)
    assert report["target_checks_passed"] == (blocker is None)
    if blocker:
        assert blocker in report["blockers"]
    if "other_library" in setup:
        assert sql(database, "SELECT id FROM public.other_library") == "42"
    else:
        assert (
            sql(database, "SELECT count(*) FROM pg_class WHERE relnamespace='public'::regnamespace")
            == "0"
        )


def test_bad_password_is_reported_without_a_driver_trace(server):
    _, _, port, sql = server
    db = "probe_" + uuid4().hex
    sql("postgres", f"CREATE DATABASE {db} OWNER probe_owner TEMPLATE template0")
    code, report = check(f"postgresql+asyncpg://probe_owner:DO_NOT_DISCLOSE@127.0.0.1:{port}/{db}")
    assert code == 1 and "connection" in report["blockers"]
    assert "DO_NOT_DISCLOSE" not in str(report)


async def test_connected_target_is_blocked_then_passes_after_disconnect(server):
    from sqlalchemy.ext.asyncio import create_async_engine

    _, password, port, sql = server
    db = "probe_" + uuid4().hex
    sql("postgres", f"CREATE DATABASE {db} OWNER probe_owner TEMPLATE template0")
    url = f"postgresql+asyncpg://probe_owner:{password}@127.0.0.1:{port}/{db}"
    # Use a different login so the probe has no special access to that session.
    engine = create_async_engine(f"postgresql+asyncpg://postgres:{password}@127.0.0.1:{port}/{db}")
    try:
        async with engine.connect():
            code, report = check(url)
            assert code == 1 and "target_busy" in report["blockers"]
    finally:
        await engine.dispose()
    assert check(url)[0] == 0


@pytest.mark.parametrize("name", ["Dockerfile", "Dockerfile.dev"])
def test_images_install_the_existing_postgres_extra(name):
    recipe = (Path(__file__).resolve().parents[2] / "docker" / name).read_text()
    group = "prod" if name == "Dockerfile" else "dev"
    assert f'".[{group},postgres]"' in recipe


_CONFIG = str(Path(__file__).resolve().parents[2] / "alembic/alembic.ini")


def _check_migration_login(role, password, port, db, *, connects):
    encoded = quote(password, safe="")
    url = f"postgresql+asyncpg://{role}:{encoded}@127.0.0.1:{port}/{db}"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", _CONFIG, "current"],
        env={**os.environ, "PULLBOX_DB_URL": url},
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = result.stdout + result.stderr
    if password in output or encoded in output:
        pytest.fail(
            "Migration diagnostics included the supplied database credential", pytrace=False
        )
    assert (result.returncode == 0) == connects


def test_migration_connection_error_keeps_encoded_credentials_private():
    _check_migration_login("fixture", secrets.token_hex(20) + "@:%", 1, "fixture", connects=False)


def test_migration_accepts_encoded_database_credentials(server):
    _, _, port, sql = server
    role, db = "owner_" + uuid4().hex, "login_" + uuid4().hex
    password = secrets.token_hex(20) + "@:%"
    sql("postgres", f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
    sql("postgres", f"CREATE DATABASE {db} OWNER {role} TEMPLATE template0")
    _check_migration_login(role, password, port, db, connects=True)


def _migrate(url, command, revision, *, expected_error=None):
    run = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", _CONFIG, command, revision],
        env={**os.environ, "PULLBOX_DB_URL": url},
        capture_output=True,
        text=True,
        timeout=120,
    )
    if expected_error is not None:
        assert run.returncode != 0 and expected_error in run.stderr
    elif run.returncode:
        from sqlalchemy.engine import make_url

        step = [line for line in run.stderr.splitlines() if "Running " in line][-1:]
        error = [line for line in run.stderr.splitlines() if "Error:" in line][-1:]
        message = " ".join(step + error)
        if password := make_url(url).password:
            message = message.replace(password, "[redacted]")
        pytest.fail(message, pytrace=False)
    return run.stderr


def test_fresh_postgres_migrations_reach_head(server):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from pullbox.models import Base
    from pullbox.models.library import FileFormat

    _, password, port, sql = server
    database = "install_" + uuid4().hex
    sql("postgres", f"CREATE DATABASE {database} OWNER probe_owner TEMPLATE template0")
    url = f"postgresql+asyncpg://probe_owner:{password}@127.0.0.1:{port}/{database}"
    head = ScriptDirectory.from_config(Config(_CONFIG)).get_current_head()
    _migrate(url, "upgrade", "head")
    assert "Running upgrade" not in _migrate(url, "upgrade", "head")
    assert sql(database, "SELECT version_num FROM alembic_version") == head
    tables = sql(database, "SELECT tablename FROM pg_tables WHERE schemaname='public'")
    assert set(tables.splitlines()) == set(Base.metadata.tables) | {"alembic_version"}
    defaults = sql(
        database,
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='indexer_configs' "
        "AND column_name IN ('enable_rss','enable_automatic_search',"
        "'enable_interactive_search')",
    )
    assert defaults.splitlines() == ["true"] * 3
    formats = sql(database, "SELECT enumlabel FROM pg_enum WHERE enumtypid='fileformat'::regtype")
    assert set(formats.splitlines()) == {value.name for value in FileFormat}


def _populated_upgrade(url, execute, scalar):
    _migrate(url, "upgrade", "b5e2a8f41c03")
    for statement in (
        "INSERT INTO series(id,title,sort_title,status,issue_count,monitored,monitor_type,"
        "year_start) VALUES (71,'Test series','Test series','ENDED',1,true,'ALL',1993)",
        "INSERT INTO issues(id,series_id,issue_number,status,release_date) "
        "VALUES (82,71,0.5,'OWNED','1995-06-01')",
        "INSERT INTO download_history(id,issue_id,title,download_url,download_client,state) "
        "VALUES(93,82,'Existing download','fixture','QBITTORRENT','IMPORTED')",
        "INSERT INTO system_config(key,value,value_type) "
        "VALUES('search_log_retention_days','31','int')",
        "INSERT INTO indexer_configs(id,name,indexer_type,url,api_key,enabled,"
        "priority,failure_count) "
        "VALUES(69,'Existing indexer','NEWZNAB','fixture','fixture',true,10,0)",
    ):
        execute(statement)
    # Commit the type-creating revision, exercise rollback/reapply, then continue.
    _migrate(url, "upgrade", "h3c4d5e6f789")
    _migrate(url, "downgrade", "g2b3d4e5f678")
    _migrate(url, "upgrade", "head")
    checks = {
        "SELECT value FROM system_config WHERE key='search_log_retention_days'": "31",
        "SELECT year_end FROM series WHERE id=71 AND series_type='STANDARD'": "1995",
        "SELECT status FROM issues WHERE id=82 AND series_id=71 AND issue_number=0.5": "OWNED",
        "SELECT protocol FROM download_history WHERE id=93 AND issue_id=82 AND "
        "download_client='QBITTORRENT' AND state='IMPORTED'": "torrent",
        "SELECT count(*) FROM indexer_configs WHERE id=69 AND enable_rss "
        "AND enable_automatic_search AND enable_interactive_search": "1",
    }
    for statement, expected in checks.items():
        assert str(scalar(statement)) == expected


def test_populated_postgres_migration_preserves_values_and_backfills(server):
    _, password, port, sql = server
    db = "upgrade_" + uuid4().hex
    sql("postgres", f"CREATE DATABASE {db} OWNER probe_owner TEMPLATE template0")
    url = f"postgresql+asyncpg://probe_owner:{password}@127.0.0.1:{port}/{db}"

    def query(statement):
        return sql(db, statement)

    _populated_upgrade(url, query, query)
    labels = query("SELECT enumlabel FROM pg_enum WHERE enumtypid='downloadclienttype'::regtype")
    assert {"AIRDCPP", "NZBGET", "DELUGE", "TRANSMISSION", "DIRECT"} <= set(labels.splitlines())
    # The dashboard filters on retry-pending even when there are no such downloads.
    query("UPDATE download_history SET state='RETRY_PENDING' WHERE id=93 AND issue_id=82")
    for command, revision in [("downgrade", "m0a123456789"), ("upgrade", "head")]:
        _migrate(url, command, revision)
        assert (
            query("SELECT state FROM download_history WHERE id=93 AND issue_id=82")
            == "RETRY_PENDING"
        )


def test_populated_sqlite_migration_keeps_supported_layout_and_behavior(tmp_path):
    from pullbox.services.database_migration_preflight import inspect_migration_backup

    db = tmp_path / "legacy.db"

    def execute(statement):
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute(statement)

    def scalar(statement):
        with closing(sqlite3.connect(db)) as conn:
            return conn.execute(statement).fetchone()[0]

    _populated_upgrade(f"sqlite+aiosqlite:///{db}", execute, scalar)
    report = inspect_migration_backup(db, configured_url="sqlite:///:memory:")
    assert report.source_checks_passed, report.blockers


async def _suggestion_status_rehearsal(url, *, sqlite):
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from pullbox.models.matching_suggestion import MatchingSuggestion, SuggestionStatus
    from pullbox.services.dashboard_intelligence_service import DashboardIntelligenceService

    _migrate(url, "upgrade", "m0a123456789")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO library_roots(id,name,path,enabled) "
                    "VALUES (71,'Fixture','/fixture',true)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO library_files(id,library_root_id,file_path,"
                    "file_name,file_size,file_format,file_modified_at,"
                    "match_confidence,has_comicinfo) VALUES (82,71,"
                    "'/fixture/test.cbz','test.cbz',10,'CBZ',CURRENT_TIMESTAMP,"
                    "'UNMATCHED',false)"
                )
            )
            # Omission exercises the historical database default, not the ORM default.
            await conn.execute(
                text("INSERT INTO matching_suggestions(id,library_file_id) VALUES (91,82)")
            )
            for i, status in enumerate(SuggestionStatus, start=92):
                await conn.execute(
                    text(
                        "INSERT INTO matching_suggestions(id,library_file_id,"
                        "status) VALUES (:id,82,:status)"
                    ),
                    {"id": i, "status": status.name if sqlite else status.value},
                )

        _migrate(url, "upgrade", "head")
        async with AsyncSession(engine) as session:
            dashboard = DashboardIntelligenceService(session)
            assert (await dashboard._load_review_debt(None)).suggestions == 2
            rows = (
                await session.scalars(select(MatchingSuggestion).order_by(MatchingSuggestion.id))
            ).all()
            assert [(r.id, r.library_file_id, r.status) for r in rows] == [
                (91, 82, SuggestionStatus.PENDING),
                (92, 82, SuggestionStatus.PENDING),
                (93, 82, SuggestionStatus.ACCEPTED),
                (94, 82, SuggestionStatus.DISMISSED),
            ]
            rows[0].status = SuggestionStatus.ACCEPTED
            rows[1].status = SuggestionStatus.DISMISSED
            session.add(MatchingSuggestion(id=95, library_file_id=82))
            await session.commit()
            assert (await dashboard._load_review_debt(None)).suggestions == 1
        async with engine.connect() as conn:
            stored = (
                (await conn.execute(text("SELECT status FROM matching_suggestions ORDER BY id")))
                .scalars()
                .all()
            )
            assert stored == ["accepted", "dismissed", "accepted", "dismissed", "pending"]
        _migrate(url, "downgrade", "m0a123456789")
        async with engine.connect() as conn:
            restored = (
                (await conn.execute(text("SELECT status FROM matching_suggestions ORDER BY id")))
                .scalars()
                .all()
            )
            assert restored == ([v.upper() for v in stored] if sqlite else stored)
    finally:
        await engine.dispose()


async def test_dashboard_suggestions_on_migrated_postgres(server):
    _, password, port, sql = server
    db = "dashboard_" + uuid4().hex
    sql("postgres", f"CREATE DATABASE {db} OWNER probe_owner TEMPLATE template0")
    await _suggestion_status_rehearsal(
        f"postgresql+asyncpg://probe_owner:{password}@127.0.0.1:{port}/{db}", sqlite=False
    )


async def test_dashboard_suggestions_on_migrated_sqlite(tmp_path):
    file = tmp_path / "dashboard.db"
    url = f"sqlite+aiosqlite:///{file}"
    await _suggestion_status_rehearsal(url, sqlite=True)
    # An unrecognized legacy decision must not be guessed, or its neighbors changed.
    with closing(sqlite3.connect(file)) as db, db:
        db.execute("UPDATE matching_suggestions SET status='other' WHERE id=94")
        before = db.execute("SELECT id,status FROM matching_suggestions ORDER BY id").fetchall()
    _migrate(url, "upgrade", "head", expected_error="Unrecognized matching suggestion status")
    with closing(sqlite3.connect(file)) as db:
        assert (
            db.execute("SELECT id,status FROM matching_suggestions ORDER BY id").fetchall()
            == before
        )
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("m0a123456789",)
