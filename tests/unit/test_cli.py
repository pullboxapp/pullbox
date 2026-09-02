"""Tests for Pullbox management CLI contracts."""

from __future__ import annotations

import io

import pytest

from pullbox import cli


def test_recheck_cli_defaults_to_dry_run_and_requires_offline_acknowledgement():
    args = cli.build_parser().parse_args(
        [
            "recheck-import",
            "--job",
            "1",
            "--source-root",
            "/comics",
            "--offline",
        ]
    )
    assert args.apply is False
    assert args.accept_replaced_files is False
    assert args.source_root == ["/comics"]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["recheck-import", "--job", "1", "--source-root", "/comics", "--apply"]
        )


def test_reset_password_parser_does_not_accept_cleartext_password_argument() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["reset-password", "--user", "admin", "--password-stdin"])

    assert args.password_stdin is True
    with pytest.raises(SystemExit):
        parser.parse_args(["reset-password", "--user", "admin", "--password", "Secret1!"])


def test_read_password_from_stdin_strips_line_ending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("NewPass1!\n"))

    assert cli._read_password(password_stdin=True) == "NewPass1!"


def test_read_password_from_stdin_rejects_empty_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("\n"))

    with pytest.raises(SystemExit):
        cli._read_password(password_stdin=True)


def test_read_password_prompts_and_confirms(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = iter(["NewPass1!", "NewPass1!"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(prompts))

    assert cli._read_password(password_stdin=False) == "NewPass1!"


def test_read_password_rejects_prompt_confirmation_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = iter(["NewPass1!", "Different1!"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: next(prompts))

    with pytest.raises(SystemExit):
        cli._read_password(password_stdin=False)


@pytest.mark.asyncio
async def test_reset_password_rejects_policy_violations_without_db_access() -> None:
    with pytest.raises(SystemExit):
        await cli._reset_password("admin", "short")


def test_main_dispatches_reset_password_without_exposing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_read_password(*, password_stdin: bool) -> str:
        captured["password_stdin"] = password_stdin
        return "NewPass1!"

    def fake_reset_password(username: str, candidate_secret: str) -> tuple[str, str]:
        captured["username"] = username
        captured["candidate_secret"] = candidate_secret
        return ("reset", username)

    def fake_run(awaitable: object) -> None:
        captured["awaitable"] = awaitable

    monkeypatch.setattr(cli.sys, "argv", ["pullbox", "reset-password", "-u", "admin"])
    monkeypatch.setattr(cli, "_read_password", fake_read_password)
    monkeypatch.setattr(cli, "_reset_password", fake_reset_password)
    monkeypatch.setattr(cli.asyncio, "run", fake_run)

    cli.main()

    assert captured == {
        "password_stdin": False,
        "username": "admin",
        "candidate_secret": "NewPass1!",
        "awaitable": ("reset", "admin"),
    }


@pytest.mark.asyncio
async def test_reset_password_reports_missing_user_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposed = False

    class FakeEngine:
        async def dispose(self) -> None:
            nonlocal disposed
            disposed = True

    class FakeResult:
        def scalar_one_or_none(self) -> None:
            return None

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def execute(self, _stmt: object) -> FakeResult:
            return FakeResult()

    monkeypatch.setattr(
        cli, "get_settings", lambda: type("Settings", (), {"db_url": "sqlite://"})()
    )
    monkeypatch.setattr(cli, "create_async_engine", lambda _url: FakeEngine())
    monkeypatch.setattr(
        cli,
        "async_sessionmaker",
        lambda _engine, expire_on_commit: lambda: FakeSession(),
    )

    with pytest.raises(SystemExit):
        await cli._reset_password("missing", "NewPass1!")

    assert disposed is True


@pytest.mark.asyncio
async def test_reset_password_updates_hash_and_session_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposed = False
    committed = False
    user = type("User", (), {"password_hash": "old", "session_version": 2})()

    class FakeEngine:
        async def dispose(self) -> None:
            nonlocal disposed
            disposed = True

    class FakeResult:
        def scalar_one_or_none(self) -> object:
            return user

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def execute(self, _stmt: object) -> FakeResult:
            return FakeResult()

        async def commit(self) -> None:
            nonlocal committed
            committed = True

    monkeypatch.setattr(
        cli, "get_settings", lambda: type("Settings", (), {"db_url": "sqlite://"})()
    )
    monkeypatch.setattr(cli, "create_async_engine", lambda _url: FakeEngine())
    monkeypatch.setattr(
        cli,
        "async_sessionmaker",
        lambda _engine, expire_on_commit: lambda: FakeSession(),
    )
    monkeypatch.setattr(cli.AuthService, "hash_password", lambda secret: f"hashed:{secret}")

    await cli._reset_password("admin", "NewPass1!")

    assert user.password_hash == "hashed:NewPass1!"
    assert user.session_version == 3
    assert committed is True
    assert disposed is True
