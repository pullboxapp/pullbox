"""Static security contracts for GitHub Actions and dependency automation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
DEPENDABOT_CONFIG = REPO_ROOT / ".github" / "dependabot.yml"
CODEQL_CONFIG = REPO_ROOT / ".github" / "codeql" / "codeql-config.yml"
GRYPE_CONFIG = REPO_ROOT / ".grype.yaml"

ACTION_REF_RE = re.compile(
    r"""
    ^(?P<indent>\s*)(?:-\s+)?uses:\s+
    (?P<action>[^@\s]+)@
    (?P<ref>[0-9a-f]{40})
    \s+\#\s+v(?P<version>[0-9][^\s]*)
    \s*$
    """,
    re.VERBOSE,
)


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml"))


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _non_comment_uses_lines(path: Path) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        if "uses:" in line:
            lines.append((line_number, line))
    return lines


def test_workflow_actions_are_pinned_to_full_sha_with_version_comments() -> None:
    """Every third-party action must be pinned and human-auditable."""
    failures: list[str] = []

    for workflow in _workflow_files():
        for line_number, line in _non_comment_uses_lines(workflow):
            if ACTION_REF_RE.match(line):
                continue
            failures.append(f"{workflow.relative_to(REPO_ROOT)}:{line_number}: {line.strip()}")

    assert failures == []


def test_workflows_do_not_use_pull_request_target() -> None:
    """Avoid privileged pull_request_target execution for untrusted PR input."""
    offenders: list[str] = []

    for workflow in _workflow_files():
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.lstrip().startswith("#"):
                continue
            if "pull_request_target" in line:
                offenders.append(f"{workflow.relative_to(REPO_ROOT)}:{line_number}")

    assert offenders == []


def test_workflows_define_explicit_default_and_job_permissions() -> None:
    """Every workflow should default to least privilege and every job should opt in."""
    failures: list[str] = []

    for workflow in _workflow_files():
        data = _load_yaml(workflow)
        workflow_name = workflow.relative_to(REPO_ROOT)

        if "permissions" not in data:
            failures.append(f"{workflow_name}: missing top-level permissions")

        jobs = data.get("jobs")
        assert isinstance(jobs, dict)
        for job_name, job_config in jobs.items():
            assert isinstance(job_config, dict)
            if "permissions" not in job_config:
                failures.append(f"{workflow_name}: job {job_name!r} missing permissions")

    assert failures == []


def test_dependabot_covers_primary_supply_chain_inputs() -> None:
    config = _load_yaml(DEPENDABOT_CONFIG)
    updates = config.get("updates")
    assert isinstance(updates, list)

    ecosystems = {update.get("package-ecosystem") for update in updates if isinstance(update, dict)}

    assert {"pip", "github-actions", "docker", "npm"} <= ecosystems


def test_security_workflow_keeps_required_scanners_and_schedule() -> None:
    security_workflow = WORKFLOW_DIR / "security.yml"
    data = _load_yaml(security_workflow)
    text = security_workflow.read_text(encoding="utf-8")

    # PyYAML follows YAML 1.1 and parses the key "on" as True.
    triggers = data.get(True, data.get("on"))
    assert isinstance(triggers, dict)
    assert "schedule" in triggers

    required_markers = [
        "GITLEAKS_IMAGE: ghcr.io/gitleaks/gitleaks@sha256:",
        "docker run --rm",
        '"$GITLEAKS_IMAGE"',
        "pip-audit --strict",
        "safety check",
        "--save-json safety-report.json",
        "bandit -r src/pullbox/",
    ]
    for marker in required_markers:
        assert marker in text


def test_codeql_analysis_uses_product_scope_config() -> None:
    security_workflow = WORKFLOW_DIR / "security.yml"
    workflow_text = security_workflow.read_text(encoding="utf-8")
    config = _load_yaml(CODEQL_CONFIG)

    assert "config-file: ./.github/codeql/codeql-config.yml" in workflow_text
    assert "queries: +security-extended" in workflow_text
    assert "security-and-quality" not in workflow_text
    assert config.get("name") == "pullbox-product-runtime"
    assert config.get("paths") == ["src/pullbox"]
    assert {
        "tests/**",
        "scripts/**",
        "docs/**",
        "performance-sprint/**",
    } <= set(config.get("paths-ignore", []))


def test_codeql_branch_probe_scans_trusted_push_refs_with_summary() -> None:
    workflow_path = WORKFLOW_DIR / "codeql-branch-probe.yml"
    data = _load_yaml(workflow_path)
    workflow_text = workflow_path.read_text(encoding="utf-8")

    # PyYAML follows YAML 1.1 and parses the key "on" as True.
    triggers = data.get(True, data.get("on"))
    assert isinstance(triggers, dict)
    assert "pull_request" not in triggers

    push = triggers.get("push")
    assert isinstance(push, dict)
    assert push.get("branches") == [
        "develop",
        "feature/code-scanning-*",
        "feature/codeql-*",
        "feature/security-*",
    ]

    jobs = data.get("jobs")
    assert isinstance(jobs, dict)
    codeql_job = jobs.get("codeql-branch-probe")
    assert isinstance(codeql_job, dict)
    assert codeql_job.get("runs-on") == "ubuntu-latest"

    permissions = codeql_job.get("permissions")
    assert isinstance(permissions, dict)
    assert permissions.get("contents") == "read"
    assert permissions.get("actions") == "read"
    assert permissions.get("security-events") == "write"

    assert "config-file: ./.github/codeql/codeql-config.yml" in workflow_text
    assert "queries: +security-extended" in workflow_text
    assert "python .github/scripts/codeql-alert-summary.py" in workflow_text
    assert "refs/heads/<branch>" in workflow_text


def test_local_security_script_writes_valid_safety_json_artifact() -> None:
    script = (REPO_ROOT / "scripts" / "security_check.sh").read_text(encoding="utf-8")

    assert '"${safety_bin}" check' in script
    assert "--save-json safety-report.json" in script
    assert "--output json > safety-report.json" not in script


def test_environment_bootstrap_uses_current_packaging_tool_floor() -> None:
    runner_setup = (REPO_ROOT / ".github" / "scripts" / "setup-runner-venv.sh").read_text(
        encoding="utf-8"
    )
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert 'PACKAGING_TOOLS_VERSION=("pip>=26.0" "wheel")' in runner_setup
    assert '"${PACKAGING_TOOLS_VERSION[@]}"' in runner_setup
    assert '$(PIP) install --upgrade "pip>=26.0" wheel' in makefile


def test_docker_workflow_runs_grype_before_publish() -> None:
    docker_workflow_path = WORKFLOW_DIR / "docker.yml"
    docker_workflow = docker_workflow_path.read_text(encoding="utf-8")
    data = _load_yaml(docker_workflow_path)
    jobs = data.get("jobs")
    assert isinstance(jobs, dict)
    push_job = jobs.get("push")
    assert isinstance(push_job, dict)

    assert "anchore/scan-action@" in docker_workflow
    assert "config: .grype.yaml" in docker_workflow
    assert push_job.get("needs") == ["build", "scan", "smoke-test"]


def test_grype_config_tracks_current_dhi_runtime() -> None:
    config_text = GRYPE_CONFIG.read_text(encoding="utf-8")
    config = _load_yaml(GRYPE_CONFIG)

    assert "Docker Hardened Images Python 3.14 on Debian 13" in config_text
    assert "python:3.13-slim" not in config_text
    assert "CVE-2026-7210" in config_text
    assert "3.14.6" in config_text
    assert config.get("ignore")


def test_docker_workflow_signs_and_verifies_published_images() -> None:
    docker_workflow_path = WORKFLOW_DIR / "docker.yml"
    docker_workflow = docker_workflow_path.read_text(encoding="utf-8")
    data = _load_yaml(docker_workflow_path)
    jobs = data.get("jobs")
    assert isinstance(jobs, dict)
    push_job = jobs.get("push")
    assert isinstance(push_job, dict)

    permissions = push_job.get("permissions")
    assert isinstance(permissions, dict)
    assert permissions.get("packages") == "write"
    assert permissions.get("id-token") == "write"

    steps = push_job.get("steps")
    assert isinstance(steps, list)
    build_steps = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("name") == "Build and push multi-arch"
    ]
    assert len(build_steps) == 1
    build_step = build_steps[0]
    assert build_step.get("id") == "push-image"
    build_with = build_step.get("with")
    assert isinstance(build_with, dict)
    assert build_with.get("provenance") == "mode=max"
    assert build_with.get("sbom") is True

    assert "sigstore/cosign-installer@" in docker_workflow
    assert "cosign sign --yes" in docker_workflow
    assert "steps.push-image.outputs.digest" in docker_workflow
    assert "release-image-digest" in docker_workflow
    assert "digest.txt" in docker_workflow
    assert "actions/upload-artifact@" in docker_workflow
    assert "cosign verify" in docker_workflow
    assert "--certificate-identity-regexp" in docker_workflow
    assert "--certificate-oidc-issuer" in docker_workflow


def test_release_notes_include_image_signature_verification() -> None:
    release_workflow_path = WORKFLOW_DIR / "release.yml"
    release_workflow = release_workflow_path.read_text(encoding="utf-8")
    data = _load_yaml(release_workflow_path)

    permissions = data.get("permissions")
    assert isinstance(permissions, dict)
    assert permissions.get("actions") == "read"

    jobs = data.get("jobs")
    assert isinstance(jobs, dict)
    release_job = jobs.get("create-release")
    assert isinstance(release_job, dict)
    job_permissions = release_job.get("permissions")
    assert isinstance(job_permissions, dict)
    assert job_permissions.get("actions") == "read"

    assert "## 🔐 Image Verification" in release_workflow
    assert "actions/download-artifact@" in release_workflow
    assert "release-image-digest" in release_workflow
    assert "github.event.workflow_run.id" in release_workflow
    assert "steps.image-digest.outputs.digest" in release_workflow
    assert "**Digest:**" in release_workflow
    assert "cosign verify" in release_workflow
    assert release_workflow.count("--certificate-oidc-issuer") == 2
    expected_ghcr_image = (
        "ghcr.io/${{ github.repository }}@${{ steps.image-digest.outputs.digest }}"
    )
    assert expected_ghcr_image in release_workflow
    assert "docker.io/pullbox/pullbox@${{ steps.image-digest.outputs.digest }}" in release_workflow


def test_release_notes_use_curated_changelog_before_commit_details() -> None:
    release_workflow = (WORKFLOW_DIR / "release.yml").read_text(encoding="utf-8")

    assert "scripts/extract_changelog_section.py" in release_workflow
    assert "--output curated_changelog.md" in release_workflow
    assert "cat curated_changelog.md" in release_workflow
    assert "## Commit Details" in release_workflow
