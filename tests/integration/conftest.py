"""Fixtures for operator-facing Compose examples."""

import shutil
import subprocess

import pytest


@pytest.fixture(scope="module")
def docker_cli():
    docker = shutil.which("docker")
    if (
        not docker
        or subprocess.run(
            [docker, "compose", "version"], capture_output=True, timeout=10
        ).returncode
    ):
        pytest.skip("Docker Compose CLI not installed")
    return docker
