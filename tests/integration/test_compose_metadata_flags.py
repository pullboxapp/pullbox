"""Render the shipped Compose examples, without a daemon, service or operator environment."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from pullbox.config import PullboxSettings

DOCKER_DIR = Path(__file__).resolve().parents[2] / "docker"
FLAGS = (
    "PULLBOX_METADATA_WHATS_NEW_ACTIONS_ENABLED",
    "PULLBOX_METADATA_PAIRED_IMPORT_WRITER_ENABLED",
    "PULLBOX_METADATA_PAIRED_CONVERSION_WRITER_ENABLED",
    "PULLBOX_METADATA_PAIRED_DOWNLOAD_WRITER_ENABLED",
    "PULLBOX_METADATA_PAIRED_DIRECT_WRITER_ENABLED",
    "PULLBOX_METADATA_GCD_API_V2_ENABLED",
)


@pytest.mark.parametrize(
    "example",
    [
        "docker-compose.yml",
        "docker-compose.dev.yml",
        "docker-compose.prod-test.yml",
        "docker-compose.postgres.yml",
    ],
)
@pytest.mark.parametrize("selected", ["defaults", "blank", "nonexperimental", *FLAGS])
def test_compose_metadata_flags_are_independent(docker_cli, tmp_path, example, selected):
    env_file = tmp_path / ".env"
    env = (DOCKER_DIR / ".env.example").read_text()
    enabled = set(FLAGS[:-1]) if selected == "nonexperimental" else {selected} & set(FLAGS)
    env += "\nSHARED_ENV_MARKER=belongs-to-another-service\n"
    if example == "docker-compose.postgres.yml":
        env += (
            "PULLBOX_PG_ADMIN_PASSWORD=render-only-admin\nPULLBOX_PG_APP_PASSWORD=render-only-app\n"
        )
    for flag in FLAGS:
        if flag in enabled:
            env += f"{flag}=true\n"
        elif selected == "blank":
            env += f"{flag}=\n"
    env_file.write_text(env)
    result = subprocess.run(
        [
            docker_cli,
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            str(DOCKER_DIR / example),
            "config",
            "--format",
            "json",
        ],
        env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]},
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    runtime = json.loads(result.stdout)["services"]["pullbox"]["environment"]
    assert "SHARED_ENV_MARKER" not in runtime
    for flag in FLAGS:
        default = PullboxSettings.model_fields[flag.removeprefix("PULLBOX_").lower()].default
        assert default is False
        value = runtime.get(flag)
        assert (str(default).lower() if value is None else value) == (
            "true" if flag in enabled else "false"
        ), f"{example}: {flag}"
