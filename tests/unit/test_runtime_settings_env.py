from __future__ import annotations

from pullbox.config import PullboxSettings


def test_settings_ignore_unprefixed_dotenv_entries(tmp_path, monkeypatch) -> None:
    """Deployment/runtime .env files may include non-Pullbox keys like TZ."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PULLBOX_PORT", raising=False)
    monkeypatch.delenv("PULLBOX_SECRET_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "TZ=America/Los_Angeles\nPULLBOX_PORT=9999\nPULLBOX_SECRET_KEY=from-dotenv\n",
        encoding="utf-8",
    )

    settings = PullboxSettings()

    assert settings.port == 9999
    assert settings.secret_key == "from-dotenv"


def test_reader_has_default_on_emergency_environment_gate(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PULLBOX_READER_ENABLED", raising=False)
    assert PullboxSettings().reader_enabled is True

    monkeypatch.setenv("PULLBOX_READER_ENABLED", "false")
    assert PullboxSettings().reader_enabled is False


def test_manual_story_arc_creation_defaults_off_and_uses_environment(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PULLBOX_STORY_ARC_MANUAL_CREATE_ENABLED", raising=False)
    assert PullboxSettings().story_arc_manual_create_enabled is False

    monkeypatch.setenv("PULLBOX_STORY_ARC_MANUAL_CREATE_ENABLED", "true")
    assert PullboxSettings().story_arc_manual_create_enabled is True


def test_reader_compression_ratio_minimum_is_configurable(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PULLBOX_READER_COMPRESSION_RATIO_MIN_MB", raising=False)
    assert PullboxSettings().reader_compression_ratio_min_mb == 4

    monkeypatch.setenv("PULLBOX_READER_COMPRESSION_RATIO_MIN_MB", "8")
    assert PullboxSettings().reader_compression_ratio_min_mb == 8
