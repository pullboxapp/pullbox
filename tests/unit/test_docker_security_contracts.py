"""Static contracts for Docker deployment hardening."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile"
DEV_DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile.dev"
COMPOSE_FILES = sorted((REPO_ROOT / "docker").glob("docker-compose*.yml"))
UNRAR_SOURCE_URL = "https://www.rarlab.com/rar/unrarsrc-7.2.6.tar.gz"
UNRAR_SOURCE_SHA256 = "d1afa67ef4121ebc5986815699e05db0ce8648499e5dca854f282a4c3f72c003"


def _dockerfile_lines() -> list[str]:
    return DOCKERFILE.read_text(encoding="utf-8").splitlines()


def _instruction_index(lines: list[str], prefix: str) -> int:
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            return index
    raise AssertionError(f"Missing Dockerfile instruction starting with {prefix!r}")


def _runtime_stage(lines: list[str]) -> list[str]:
    runtime_start = _instruction_index(lines, "FROM dhi.io/python:3.14-debian13 AS runtime")
    return lines[runtime_start:]


def _compose_documents() -> list[tuple[Path, dict[str, Any]]]:
    documents: list[tuple[Path, dict[str, Any]]] = []
    for path in COMPOSE_FILES:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)
        documents.append((path, loaded))
    return documents


def test_dockerfile_uses_expected_dhi_builder_and_runtime_images() -> None:
    """The production image must stay on the approved Python DHI baseline."""
    lines = _dockerfile_lines()

    assert lines[0] == "FROM dhi.io/python:3.14-debian13-dev AS builder"
    assert "FROM dhi.io/python:3.14-debian13 AS runtime" in lines


def test_runtime_user_is_non_root_before_runtime_entrypoints() -> None:
    """The runtime process and Docker healthcheck must execute as UID/GID 65532."""
    lines = _dockerfile_lines()

    user_index = _instruction_index(lines, "USER 65532:65532")
    healthcheck_index = _instruction_index(lines, "HEALTHCHECK")
    entrypoint_index = _instruction_index(lines, "ENTRYPOINT")
    cmd_index = _instruction_index(lines, "CMD")

    assert user_index < healthcheck_index < entrypoint_index < cmd_index


def test_entrypoint_and_healthcheck_use_python_modules() -> None:
    """Shell-less DHI runtime should launch through explicit Python modules."""
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert 'CMD ["python", "-m", "pullbox.docker_healthcheck"]' in text
    assert 'ENTRYPOINT ["python", "-m", "pullbox.docker_entrypoint"]' in text
    assert 'CMD ["python", "-m", "pullbox"]' in text


def test_runtime_stage_does_not_install_debug_or_package_manager_tools() -> None:
    """Runtime stage must stay minimal; packages belong in the builder stage only."""
    runtime_text = "\n".join(_runtime_stage(_dockerfile_lines()))

    forbidden_fragments = (
        "apt-get",
        "apk ",
        "dnf ",
        "yum ",
        "curl",
        "wget",
        "netcat",
        "nmap",
        "tcpdump",
        "openssh",
    )

    for fragment in forbidden_fragments:
        assert fragment not in runtime_text


def test_production_image_does_not_ship_python_bytecode_caches() -> None:
    """Runtime images should avoid precompiled bytecode weight."""
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "PIP_NO_COMPILE=1" in text
    assert "--no-compile" in text
    assert 'find "${VIRTUAL_ENV}" -type d -name __pycache__' in text
    assert "PYTHONDONTWRITEBYTECODE=1" in text


def test_production_image_builds_official_unrar_not_unrar_free() -> None:
    """CBR support should use pinned official UnRAR, not unrar-free."""
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "unrar-free" not in text
    assert UNRAR_SOURCE_URL in text
    assert UNRAR_SOURCE_SHA256 in text
    assert "copy_binary /usr/bin/unrar" in text
    assert "/usr/share/licenses/unrar/license.txt" in text


def test_dev_image_builds_official_unrar_not_unrar_free() -> None:
    """Local dev should use the same RAR extractor as production."""
    text = DEV_DOCKERFILE.read_text(encoding="utf-8")

    assert "unrar-free" not in text
    assert UNRAR_SOURCE_URL in text
    assert UNRAR_SOURCE_SHA256 in text
    assert "/usr/share/licenses/unrar/license.txt" in text


def test_unrar_source_extraction_installs_gzip_in_builders() -> None:
    """The official UnRAR tarball extraction requires gzip in builder images."""
    dockerfiles = (DOCKERFILE, DEV_DOCKERFILE)

    for dockerfile in dockerfiles:
        text = dockerfile.read_text(encoding="utf-8")
        assert "tar -xzf /tmp/unrarsrc.tar.gz" in text
        assert "\n        gzip \\" in text, f"{dockerfile} must install gzip"


def test_runtime_state_paths_are_created_with_non_root_ownership() -> None:
    """Writable application paths must be owned by the runtime UID/GID."""
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY --from=builder --chown=65532:65532 /runtime-dirs/data /data" in text
    assert "COPY --from=builder --chown=65532:65532 /runtime-dirs/comics /comics" in text
    assert "COPY --from=builder --chown=65532:65532 /runtime-dirs/downloads /downloads" in text
    assert "COPY --from=builder --chown=65532:65532 /runtime-dirs/imports /imports" in text
    assert "COPY --from=builder --chown=65532:65532 /opt/venv /opt/venv" in text


def test_runtime_python_loads_copied_packages_across_dhi_layouts() -> None:
    """Runtime Python must load packages without relying on the builder's symlinks."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    pip_upgrade = '"${VIRTUAL_ENV}/bin/python" -m pip install --upgrade pip wheel'
    runtime_path = (
        'PATH="/opt/python/bin:/usr/bin:/opt/venv/bin:/usr/local/sbin:'
        '/usr/local/bin:/usr/sbin:/sbin:/bin"'
    )

    assert pip_upgrade in text
    assert '"${VIRTUAL_ENV}/bin/python" -m pip install --no-cache-dir' in text
    assert runtime_path in text
    assert 'PYTHONPATH="/opt/venv/lib/python3.14/site-packages"' in text
    assert 'ln -sfn /opt/python/bin/python "${VIRTUAL_ENV}/bin/python"' not in text


def test_compose_files_mount_canonical_media_paths() -> None:
    """Compose examples should expose the documented app-side media roots."""
    expected_mounts = {"/comics", "/downloads", "/imports"}

    for path, document in _compose_documents():
        service = document["services"]["pullbox"]
        volumes = service.get("volumes")
        assert isinstance(volumes, list), f"{path}:pullbox must define volumes"
        mounted_targets = {
            str(volume).rsplit(":", maxsplit=1)[-1] for volume in volumes if isinstance(volume, str)
        }
        assert expected_mounts <= mounted_targets


def test_compose_files_do_not_request_privileged_container_access() -> None:
    """Compose examples must not grant broad host/container privileges."""
    forbidden_service_keys = {
        "privileged",
        "cap_add",
        "devices",
        "pid",
        "network_mode",
    }

    for path, document in _compose_documents():
        services = document.get("services")
        assert isinstance(services, dict), f"{path} must define services"
        for service_name, service in services.items():
            assert isinstance(service, dict), f"{path}:{service_name} must be a service mapping"
            present = forbidden_service_keys & service.keys()
            assert present == set(), f"{path}:{service_name} declares {sorted(present)}"


def test_dockerfile_lives_under_canonical_docker_directory() -> None:
    """The canonical production Dockerfile is docker/Dockerfile."""
    assert DOCKERFILE.exists()
    assert not (REPO_ROOT / "Dockerfile").exists()
