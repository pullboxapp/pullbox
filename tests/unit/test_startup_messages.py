"""Tests for human-readable startup banner and status messages."""

from __future__ import annotations

import runpy
import sys
from types import SimpleNamespace

import pytest

from pullbox import startup_messages
from pullbox.startup_messages import (
    StartupSummary,
    build_startup_summary,
    render_bootstrap_summary,
    render_ready_summary,
    render_restart_requested,
)


def test_render_bootstrap_summary_includes_banner_and_context() -> None:
    summary = StartupSummary(
        version="0.5.0-dev",
        release_date="2026-05-12",
        branch="feature/ui-followups",
        commit="abcdef1",
        base_url="http://localhost:8585",
        bind_address="0.0.0.0",
        port=8585,
        database="SQLite",
        library_root="/comics",
        startup_log="/data/logs/startup.log",
    )

    rendered = render_bootstrap_summary(summary)

    assert "comic book management & acquisition" in rendered
    assert "[bootstrap] Pullbox v0.5.0-dev" in rendered
    assert "[bootstrap] Released: 2026-05-12" in rendered
    assert "[bootstrap] Branch: feature/ui-followups" in rendered
    assert "[bootstrap] Commit: abcdef1" in rendered
    assert "[bootstrap] Logs: /data/logs/startup.log" in rendered


def test_build_startup_summary_uses_runtime_settings(monkeypatch) -> None:
    settings = SimpleNamespace(
        base_url="https://pullbox.example.test",
        bind_address="127.0.0.1",
        port=9443,
        db_url="sqlite+aiosqlite:////data/pullbox.db",
        library_root="/library",
    )
    metadata = SimpleNamespace(
        release_date="2026-06-17",
        branch="main",
        commit="abc1234",
    )
    monkeypatch.setattr(startup_messages, "get_settings", lambda: settings)
    monkeypatch.setattr(startup_messages, "get_build_metadata", lambda: metadata)
    monkeypatch.setattr(startup_messages, "display_version", lambda: "0.9.5")

    summary = build_startup_summary(startup_log="/data/logs/startup.log")

    assert summary == StartupSummary(
        version="0.9.5",
        release_date="2026-06-17",
        branch="main",
        commit="abc1234",
        base_url="https://pullbox.example.test",
        bind_address="127.0.0.1",
        port=9443,
        database="SQLite",
        library_root="/library",
        startup_log="/data/logs/startup.log",
    )


def test_render_ready_summary_derives_ping_url() -> None:
    rendered = render_ready_summary(
        base_url="http://localhost:8585",
        scheduler_active=True,
    )

    assert "[ready] Pullbox is ready" in rendered
    assert "[ready] Open: http://localhost:8585" in rendered
    assert "[ready] Health: http://localhost:8585/ping" in rendered
    assert "[ready] Scheduler: active" in rendered


@pytest.mark.parametrize(
    ("db_url", "label"),
    [
        ("postgresql+asyncpg://sensitive:private@host/private?sslkey=private", "PostgreSQL"),
        ("postgresql+asyncpg:///private?host=private&passfile=private", "PostgreSQL"),
        ("sqlite+aiosqlite:///private?key=private", "SQLite"),
        ("not a private database URL", "Unknown"),
    ],
)
def test_bootstrap_exposes_only_database_backend(monkeypatch, db_url, label):
    from pullbox.config import get_settings

    settings = get_settings().model_copy(update={"db_url": db_url})
    monkeypatch.setattr(startup_messages, "get_settings", lambda: settings)
    summary = build_startup_summary(startup_log="/data/logs/startup.log")
    rendered = render_bootstrap_summary(summary)
    assert f"[bootstrap] Database: {label}\n" in rendered
    assert db_url not in repr(summary) and db_url not in rendered


def test_render_ready_summary_uses_custom_health_and_inactive_scheduler() -> None:
    rendered = render_ready_summary(
        base_url="http://localhost:8585",
        scheduler_active=False,
        health_url="https://status.example.test/health",
    )

    assert "[ready] Health: https://status.example.test/health" in rendered
    assert "[ready] Scheduler: inactive" in rendered


def test_render_restart_requested_mentions_exit_code() -> None:
    assert (
        render_restart_requested(42)
        == "[bootstrap] Restart requested (exit code 42), relaunching..."
    )


def test_main_renders_bootstrap_summary(monkeypatch, capsys) -> None:
    summary = StartupSummary(
        version="0.9.5",
        release_date=None,
        branch=None,
        commit=None,
        base_url="http://localhost:8585",
        bind_address="0.0.0.0",
        port=8585,
        database="SQLite",
        library_root="/comics",
        startup_log="/data/logs/startup.log",
    )
    requested_logs: list[str] = []

    def fake_build_startup_summary(*, startup_log: str) -> StartupSummary:
        requested_logs.append(startup_log)
        return summary

    monkeypatch.setattr(
        sys,
        "argv",
        ["startup-messages", "bootstrap", "--startup-log", "/logs/startup.log"],
    )
    monkeypatch.setattr(startup_messages, "build_startup_summary", fake_build_startup_summary)
    monkeypatch.setattr(
        startup_messages,
        "render_bootstrap_summary",
        lambda value: f"summary:{value.version}",
    )

    startup_messages.main()

    assert requested_logs == ["/logs/startup.log"]
    assert capsys.readouterr().out == "summary:0.9.5\n"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["startup-messages", "migration-start"], "[bootstrap] Running database migrations..."),
        (["startup-messages", "migration-complete"], "[bootstrap] Database migrations complete"),
        (["startup-messages", "launching"], "[bootstrap] Launching Pullbox web app..."),
        (
            ["startup-messages", "restart-requested", "--exit-code", "42"],
            "[bootstrap] Restart requested (exit code 42), relaunching...",
        ),
    ],
)
def test_main_renders_simple_subcommands(
    monkeypatch,
    capsys,
    argv: list[str],
    expected: str,
) -> None:
    monkeypatch.setattr(sys, "argv", argv)

    startup_messages.main()

    assert capsys.readouterr().out == f"{expected}\n"


def test_main_rejects_unhandled_command(monkeypatch) -> None:
    monkeypatch.setattr(
        startup_messages.argparse.ArgumentParser,
        "parse_args",
        lambda self: SimpleNamespace(command="unknown"),
    )

    with pytest.raises(AssertionError, match="Unhandled startup command: unknown"):
        startup_messages.main()


def test_module_entrypoint_renders_launching_message(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["startup-messages", "launching"])

    runpy.run_path(startup_messages.__file__, run_name="__main__")

    assert capsys.readouterr().out == "[bootstrap] Launching Pullbox web app...\n"
