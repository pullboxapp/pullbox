"""Static security contracts for GitHub Actions and dependency automation."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from pullbox.core.secret_validation import MIN_APPLICATION_SECRET_LENGTH

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
DEPENDABOT_CONFIG = REPO_ROOT / ".github" / "dependabot.yml"
CODEQL_CONFIG = REPO_ROOT / ".github" / "codeql" / "codeql-config.yml"
GRYPE_CONFIG = REPO_ROOT / ".grype.yaml"
ACTIONLINT_CONFIG = REPO_ROOT / ".github" / "actionlint.yaml"
RELEASE_SYNC_SCRIPT = REPO_ROOT / ".github" / "scripts" / "validate-release-sync-pr.py"

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


def _load_release_sync_module() -> Any:
    spec = importlib.util.spec_from_file_location("release_sync_validator", RELEASE_SYNC_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


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


def test_security_workflow_runs_required_scanners_on_pr_full_gate() -> None:
    security_workflow = WORKFLOW_DIR / "security.yml"
    data = _load_yaml(security_workflow)
    text = security_workflow.read_text(encoding="utf-8")

    # PyYAML follows YAML 1.1 and parses the key "on" as True.
    triggers = data.get(True, data.get("on"))
    assert isinstance(triggers, dict)
    assert "workflow_dispatch" in triggers
    pull_request = triggers.get("pull_request")
    assert isinstance(pull_request, dict)
    assert pull_request.get("branches") == ["develop", "main"]
    assert {
        "opened",
        "synchronize",
        "reopened",
        "ready_for_review",
        "labeled",
        "unlabeled",
    } <= set(pull_request.get("types", []))
    assert "schedule" not in triggers
    assert "merge_group" not in triggers
    assert "push" not in triggers
    assert "contains(github.event.pull_request.labels.*.name, 'ci:full')" in text

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


def test_codeql_branch_probe_is_manual_fallback_with_summary() -> None:
    workflow_path = WORKFLOW_DIR / "codeql-branch-probe.yml"
    data = _load_yaml(workflow_path)
    workflow_text = workflow_path.read_text(encoding="utf-8")

    # PyYAML follows YAML 1.1 and parses the key "on" as True.
    triggers = data.get(True, data.get("on"))
    assert isinstance(triggers, dict)
    assert "workflow_dispatch" in triggers
    assert "push" not in triggers
    assert "pull_request" not in triggers
    assert "merge_group" not in triggers

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


def test_ci_and_local_full_ci_enforce_v1_coverage_gate() -> None:
    ci_workflow = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    ci_local_match = re.search(r"^ci-local:.*?(?=^\S|\Z)", makefile, re.MULTILINE | re.DOTALL)

    assert "--cov-fail-under=90" in ci_workflow
    assert "--cov-fail-under=60" not in ci_workflow
    assert ci_local_match is not None
    assert "--cov-fail-under=90 -v" in ci_local_match.group(0)
    assert "--cov-fail-under=60" not in ci_local_match.group(0)


def test_ci_uploads_coverage_for_each_python_matrix_version() -> None:
    """Coverage artifacts should be inspectable for every tested Python version."""
    ci_workflow = _load_yaml(WORKFLOW_DIR / "ci.yml")
    jobs = ci_workflow.get("jobs")
    assert isinstance(jobs, dict)
    test_job = jobs.get("test")
    assert isinstance(test_job, dict)

    upload_steps = [
        step
        for step in test_job.get("steps", [])
        if isinstance(step, dict) and step.get("name") == "Upload coverage report"
    ]
    assert len(upload_steps) == 1
    upload_step = upload_steps[0]

    assert upload_step.get("if") == "always()"
    upload_with = upload_step.get("with")
    assert isinstance(upload_with, dict)
    assert upload_with.get("name") == "coverage-report-py${{ matrix.python-version }}"


def test_required_gate_workflows_are_pr_triggered_and_label_gated() -> None:
    """Required PR gates should stay cheap until maintainers request full CI."""
    for workflow_name in ["ci.yml", "security.yml", "workflow-hygiene.yml", "docker-validate.yml"]:
        workflow_path = WORKFLOW_DIR / workflow_name
        workflow_text = workflow_path.read_text(encoding="utf-8")
        data = _load_yaml(WORKFLOW_DIR / workflow_name)
        triggers = data.get(True, data.get("on"))
        assert isinstance(triggers, dict)
        assert "workflow_dispatch" in triggers
        pull_request = triggers.get("pull_request")
        assert isinstance(pull_request, dict)
        assert pull_request.get("branches") == ["develop", "main"]
        assert {
            "opened",
            "synchronize",
            "reopened",
            "ready_for_review",
            "labeled",
            "unlabeled",
        } <= set(pull_request.get("types", []))
        assert "push" not in triggers
        assert "merge_group" not in triggers
        assert "schedule" not in triggers
        assert "full_ci_check:" in workflow_text
        assert "contains(github.event.pull_request.labels.*.name, 'ci:full')" in workflow_text


def test_clean_room_workflow_stays_manual_only_without_schedule() -> None:
    data = _load_yaml(WORKFLOW_DIR / "clean-room.yml")
    triggers = data.get(True, data.get("on"))
    assert isinstance(triggers, dict)
    assert "workflow_dispatch" in triggers
    assert "pull_request" not in triggers
    assert "push" not in triggers
    assert "schedule" not in triggers


def test_release_sync_fast_path_requires_next_patch_dev_version() -> None:
    validator = _load_release_sync_module()

    assert validator.extract_version('__version__ = "0.9.10"\n') == "0.9.10"
    assert validator.expected_next_dev_version("0.9.10") == "0.9.11-dev"

    valid = validator.version_bump_is_release_sync(
        '__version__ = "0.9.10"\n',
        '__version__ = "0.9.11-dev"\n',
    )
    assert valid.is_sync is True

    unchanged = validator.version_bump_is_release_sync(
        '__version__ = "0.9.10"\n',
        '__version__ = "0.9.10"\n',
    )
    assert unchanged.is_sync is False
    assert "0.9.11-dev" in unchanged.reason

    extra_version_file_change = validator.version_bump_is_release_sync(
        '__version__ = "0.9.10"\nSTARTED_AT = "safe"\n',
        '__version__ = "0.9.11-dev"\nSTARTED_AT = "changed"\n',
    )
    assert extra_version_file_change.is_sync is False
    assert "beyond the expected version bump" in extra_version_file_change.reason


def test_release_sync_validator_accepts_only_main_plus_next_dev_bump(tmp_path: Path) -> None:
    validator = _load_release_sync_module()
    repo = tmp_path / "repo"
    repo.mkdir()

    _git(repo, "init", "--initial-branch=develop")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Pullbox Tests")
    version_file = repo / "src" / "pullbox" / "__init__.py"
    version_file.parent.mkdir(parents=True)
    version_file.write_text('__version__ = "0.9.9-dev"\n', encoding="utf-8")
    _git(repo, "add", "src/pullbox/__init__.py")
    _git(repo, "commit", "-m", "seed develop")
    _git(repo, "update-ref", "refs/remotes/origin/develop", "HEAD")

    _git(repo, "switch", "-c", "main")
    version_file.write_text('__version__ = "0.9.10"\n', encoding="utf-8")
    _git(repo, "commit", "-am", "release")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    _git(repo, "switch", "-c", "feature/sync-develop-0.9.10")
    version_file.write_text('__version__ = "0.9.11-dev"\n', encoding="utf-8")
    _git(repo, "commit", "-am", "dev bump")

    env = {
        "RELEASE_SYNC_EVENT_NAME": "pull_request",
        "RELEASE_SYNC_BASE_REF": "develop",
        "RELEASE_SYNC_HEAD_REF": "feature/sync-develop-0.9.10",
        "RELEASE_SYNC_REPOSITORY": "pullboxapp/pullbox",
        "RELEASE_SYNC_HEAD_REPOSITORY": "pullboxapp/pullbox",
        "RELEASE_SYNC_ACTOR": "maintainer",
    }

    valid = validator.validate_release_sync_pr(env, repo, fetch_refs=False)
    assert valid.is_sync is True

    (repo / "README.md").write_text("extra change\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "extra change")

    invalid = validator.validate_release_sync_pr(env, repo, fetch_refs=False)
    assert invalid.is_sync is False
    assert "only allows" in invalid.reason


def test_release_sync_fast_path_is_wired_to_required_aggregate_workflows() -> None:
    required_contracts = {
        "ci.yml": {
            "aggregate": "ci-required",
            "heavy": [
                "quality-gate",
                "typecheck",
                "alembic-check",
                "test",
                "accessibility",
                "e2e",
            ],
            "message": "heavyweight CI jobs intentionally skipped",
        },
        "security.yml": {
            "aggregate": "security-required",
            "heavy": ["gitleaks", "dependency-audit", "safety-check", "bandit", "codeql"],
            "message": "heavyweight security jobs intentionally skipped",
        },
        "workflow-hygiene.yml": {
            "aggregate": "workflow-hygiene-required",
            "heavy": ["actionlint"],
            "message": "actionlint intentionally skipped",
        },
    }

    for workflow_name, contract in required_contracts.items():
        workflow_path = WORKFLOW_DIR / workflow_name
        workflow_text = workflow_path.read_text(encoding="utf-8")
        data = _load_yaml(workflow_path)
        jobs = data.get("jobs")
        assert isinstance(jobs, dict)

        release_sync = jobs.get("release_sync_check")
        assert isinstance(release_sync, dict)
        assert release_sync.get("name") == "Release Sync Check"
        assert release_sync.get("runs-on") == "ubuntu-latest"
        assert release_sync.get("outputs", {}).get("is_sync") == (
            "${{ steps.release-sync.outputs.is_sync }}"
        )
        assert "git show origin/main:.github/scripts/validate-release-sync-pr.py" in workflow_text
        assert "python /tmp/pullbox-release-sync-validator.py" in workflow_text
        assert "python .github/scripts/validate-release-sync-pr.py" not in workflow_text

        aggregate = jobs.get(contract["aggregate"])
        assert isinstance(aggregate, dict)
        assert "release_sync_check" in aggregate.get("needs", [])
        assert "RELEASE_SYNC_PR" in workflow_text
        assert contract["message"] in workflow_text

        for job_name in contract["heavy"]:
            job = jobs.get(job_name)
            assert isinstance(job, dict)
            needs = job.get("needs")
            if isinstance(needs, str):
                needs = [needs]
            assert "release_sync_check" in needs
            assert "needs.release_sync_check.outputs.is_sync != 'true'" in str(job.get("if"))


def test_docker_validation_skips_trusted_docker_runner_for_release_sync_prs() -> None:
    workflow_path = WORKFLOW_DIR / "docker-validate.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    data = _load_yaml(workflow_path)
    jobs = data.get("jobs")
    assert isinstance(jobs, dict)

    release_sync = jobs.get("release_sync_check")
    assert isinstance(release_sync, dict)
    assert release_sync.get("runs-on") == "ubuntu-latest"

    trusted = jobs.get("trusted-production-validate")
    assert isinstance(trusted, dict)
    assert trusted.get("needs") == ["release_sync_check", "full_ci_check"]
    assert "needs.release_sync_check.outputs.is_sync != 'true'" in trusted.get("if", "")
    assert "needs.full_ci_check.outputs.trusted_full == 'true'" in trusted.get("if", "")
    assert "git show origin/main:.github/scripts/validate-release-sync-pr.py" in workflow_text
    assert "python /tmp/pullbox-release-sync-validator.py" in workflow_text
    assert "python .github/scripts/validate-release-sync-pr.py" not in workflow_text


def test_trusted_jobs_use_explicit_self_hosted_runner_labels() -> None:
    """Avoid generic self-hosted routing so untrusted code cannot drift onto runners."""
    failures: list[str] = []
    for workflow in _workflow_files():
        data = _load_yaml(workflow)
        jobs = data.get("jobs")
        assert isinstance(jobs, dict)
        for job_name, job_config in jobs.items():
            assert isinstance(job_config, dict)
            runs_on = job_config.get("runs-on")
            if runs_on == "self-hosted":
                failures.append(f"{workflow.name}:{job_name}")
    assert failures == []


def test_actionlint_allowlist_declares_every_custom_runner_label() -> None:
    config = _load_yaml(ACTIONLINT_CONFIG)
    labels = config.get("self-hosted-runner", {}).get("labels")
    assert set(labels) == {"checks", "ci", "docker"}


def test_self_hosted_jobs_separate_heavy_ci_from_lightweight_checks() -> None:
    """Keep matrix tests off runners used by accessibility and security checks."""
    expected_labels = {
        "ci.yml": {
            "ci": ["quality-gate", "typecheck", "test", "alembic-check", "e2e"],
            "checks": ["accessibility"],
        },
        "security.yml": {
            "checks": ["gitleaks", "dependency-audit", "safety-check", "bandit"],
        },
        "workflow-hygiene.yml": {"checks": ["actionlint"]},
    }

    for workflow_name, labels in expected_labels.items():
        jobs = _load_yaml(WORKFLOW_DIR / workflow_name).get("jobs")
        assert isinstance(jobs, dict)
        for label, job_names in labels.items():
            selector = f'["self-hosted","Linux","X64","{label}"]'
            for job_name in job_names:
                job = jobs.get(job_name)
                assert isinstance(job, dict)
                runs_on = job.get("runs-on")
                assert isinstance(runs_on, str)
                assert "ubuntu-latest" in runs_on
                assert selector in runs_on


def test_ci_python_matrix_uses_five_parallel_workers() -> None:
    workflow = _load_yaml(WORKFLOW_DIR / "ci.yml")
    env = workflow.get("env")
    assert isinstance(env, dict)
    assert env.get("PYTEST_WORKERS") == "5"

    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    test_job = jobs.get("test")
    assert isinstance(test_job, dict)
    test_step = next(
        step for step in test_job.get("steps", []) if step.get("name") == "Run tests with coverage"
    )
    assert "--dist=worksteal" in test_step.get("run", "")


def test_e2e_matrix_shards_each_browser_across_three_workers() -> None:
    workflow = _load_yaml(WORKFLOW_DIR / "ci.yml")
    env = workflow.get("env")
    assert isinstance(env, dict)
    assert env.get("E2E_WORKERS") == "3"

    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    e2e_job = jobs.get("e2e")
    assert isinstance(e2e_job, dict)
    e2e_step = next(
        step for step in e2e_job.get("steps", []) if step.get("name") == "Run E2E tests"
    )
    command = e2e_step.get("run", "")
    assert '-n "${E2E_WORKERS}"' in command
    assert "--dist=worksteal" in command


def test_ci_video_and_trace_capture_are_manual_diagnostics_only() -> None:
    workflow = _load_yaml(WORKFLOW_DIR / "ci.yml")
    triggers = workflow.get(True, workflow.get("on"))
    assert isinstance(triggers, dict)
    dispatch = triggers.get("workflow_dispatch")
    assert isinstance(dispatch, dict)
    diagnostic_input = dispatch.get("inputs", {}).get("e2e_diagnostics")
    assert diagnostic_input == {
        "description": "Retain E2E failure video and traces",
        "required": False,
        "type": "boolean",
        "default": False,
    }

    env = workflow.get("env")
    assert isinstance(env, dict)
    assert env.get("E2E_VIDEO_MODE") == (
        "${{ github.event_name == 'workflow_dispatch' && inputs.e2e_diagnostics "
        "&& 'retain-on-failure' || 'off' }}"
    )
    assert env.get("PULLBOX_E2E_TRACE") == (
        "${{ github.event_name == 'workflow_dispatch' && inputs.e2e_diagnostics "
        "&& 'true' || 'false' }}"
    )

    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    for job_name, step_name in [
        ("accessibility", "Run accessibility browser tests"),
        ("e2e", "Run E2E tests"),
    ]:
        job = jobs.get(job_name)
        assert isinstance(job, dict)
        step = next(step for step in job.get("steps", []) if step.get("name") == step_name)
        command = step.get("run", "")
        assert '--video "${E2E_VIDEO_MODE}"' in command
        assert "--video retain-on-failure" not in command


def test_browser_smoke_and_accessibility_artifacts_survive_failures_and_cancels() -> None:
    ci_workflow = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
    docker_validate = (WORKFLOW_DIR / "docker-validate.yml").read_text(encoding="utf-8")
    docker_release = (WORKFLOW_DIR / "docker-release.yml").read_text(encoding="utf-8")

    assert "Upload accessibility failure artifacts" in ci_workflow
    assert "Upload Playwright failure artifacts" in ci_workflow
    assert "if: failure() || cancelled()" in ci_workflow
    assert "if-no-files-found: ignore" in ci_workflow

    assert "Upload Docker validation failure artifacts" in docker_validate
    assert "if: failure() || cancelled()" in docker_validate
    assert "if-no-files-found: ignore" in docker_validate

    assert "Upload smoke traces" in docker_release
    assert "if: failure() || cancelled()" in docker_release
    assert "if-no-files-found: ignore" in docker_release


def test_ci_uses_shared_tailwind_build_script() -> None:
    ci_workflow = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
    package_json = (REPO_ROOT / "package.json").read_text(encoding="utf-8")

    assert "npm run css:build" in ci_workflow
    assert "ensure-final-newline.mjs src/pullbox/ui/static/css/tailwind.css" in package_json


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


def test_docker_validation_workflow_never_publishes_images() -> None:
    docker_validate_path = WORKFLOW_DIR / "docker-validate.yml"
    docker_validate = docker_validate_path.read_text(encoding="utf-8")
    data = _load_yaml(docker_validate_path)
    triggers = data.get(True, data.get("on"))
    assert isinstance(triggers, dict)
    assert "workflow_dispatch" in triggers
    pull_request = triggers.get("pull_request")
    assert isinstance(pull_request, dict)
    assert pull_request.get("branches") == ["develop", "main"]
    assert "push" not in triggers
    assert "merge_group" not in triggers
    assert "schedule" not in triggers

    assert "push: true" not in docker_validate
    assert "packages: write" not in docker_validate
    assert "ghcr.io/${{ github.repository }}" not in docker_validate
    assert "docker.io/pullbox/pullbox" not in docker_validate
    assert "github.actor == 'dependabot[bot]'" in docker_validate
    assert "Docker Sanity (untrusted PR)" in docker_validate

    jobs = data.get("jobs")
    assert isinstance(jobs, dict)
    assert jobs["untrusted-sanity"].get("runs-on") == "ubuntu-latest"
    assert jobs["trusted-production-validate"].get("runs-on") == [
        "self-hosted",
        "Linux",
        "X64",
        "docker",
    ]


def test_docker_smoke_workflows_use_ephemeral_host_ports() -> None:
    """Self-hosted runners may already have Pullbox bound on 8585."""
    contracts = [
        {
            "workflow": "docker-validate.yml",
            "container": "pullbox-validate",
            "url_env": "PULLBOX_VALIDATE_URL",
        },
        {
            "workflow": "docker-release.yml",
            "container": '"${SMOKE_CONTAINER}"',
            "url_env": "PULLBOX_SMOKE_URL",
        },
    ]

    for contract in contracts:
        workflow_text = (WORKFLOW_DIR / contract["workflow"]).read_text(encoding="utf-8")
        container = contract["container"]
        url_env = contract["url_env"]

        assert "-p 8585:8585" not in workflow_text
        assert "-p 127.0.0.1::8585" in workflow_text
        assert f"docker port {container} 8585/tcp" in workflow_text
        assert f"{url_env}=http://127.0.0.1:" in workflow_text
        assert f"${{{url_env}}}/ping" in workflow_text


def test_docker_smoke_workflows_use_valid_application_secrets() -> None:
    """Docker smoke checks should not fail before startup due to weak test secrets."""
    failures: list[str] = []
    secret_re = re.compile(r"PULLBOX_SECRET_KEY=([^\\\n\"' ]+)")

    for workflow_name in ["docker-validate.yml", "docker-release.yml"]:
        workflow_text = (WORKFLOW_DIR / workflow_name).read_text(encoding="utf-8")
        for secret in secret_re.findall(workflow_text):
            if len(secret) < MIN_APPLICATION_SECRET_LENGTH:
                failures.append(f"{workflow_name}: {secret}")

    assert failures == []


def test_docker_release_workflow_is_tag_or_manual_only_and_scans_before_publish() -> None:
    docker_workflow_path = WORKFLOW_DIR / "docker-release.yml"
    docker_workflow = docker_workflow_path.read_text(encoding="utf-8")
    data = _load_yaml(docker_workflow_path)
    triggers = data.get(True, data.get("on"))
    assert isinstance(triggers, dict)
    push = triggers.get("push")
    assert isinstance(push, dict)
    assert push.get("tags") == ["v*"]
    assert "pull_request" not in triggers
    assert "workflow_run" not in triggers
    assert "workflow_dispatch" in triggers

    jobs = data.get("jobs")
    assert isinstance(jobs, dict)
    push_job = jobs.get("push")
    assert isinstance(push_job, dict)
    validate_job = jobs.get("validate-amd64")
    assert isinstance(validate_job, dict)

    assert "anchore/scan-action@" in docker_workflow
    assert "config: .grype.yaml" in docker_workflow
    assert validate_job.get("needs") == ["build", "build-amd64"]
    assert push_job.get("needs") == [
        "build",
        "build-amd64",
        "build-arm64",
        "validate-amd64",
    ]
    assert push_job.get("runs-on") == "ubuntu-latest"


def test_docker_release_distributes_platform_builds_before_manifest_publish() -> None:
    """Release platforms should build concurrently and publish tags only after validation."""
    workflow_path = WORKFLOW_DIR / "docker-release.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    data = _load_yaml(workflow_path)
    jobs = data.get("jobs")
    assert isinstance(jobs, dict)

    prepare = jobs.get("build")
    amd64 = jobs.get("build-amd64")
    arm64 = jobs.get("build-arm64")
    validate = jobs.get("validate-amd64")
    push = jobs.get("push")
    for job in [prepare, amd64, arm64, validate, push]:
        assert isinstance(job, dict)

    assert prepare.get("runs-on") == "ubuntu-latest"
    assert prepare.get("outputs", {}).get("image-tags") == "${{ steps.meta.outputs.tags }}"
    assert prepare.get("outputs", {}).get("build-date") == (
        "${{ steps.release-metadata.outputs.build_date }}"
    )

    expected_runner = ["self-hosted", "Linux", "X64", "docker"]
    for platform_job, platform, cache_scope in [
        (amd64, "linux/amd64", "pullbox-release-amd64-v1"),
        (arm64, "linux/arm64", "pullbox-release-arm64-v1"),
    ]:
        assert platform_job.get("runs-on") == expected_runner
        assert platform_job.get("needs") == ["build"]
        assert platform_job.get("permissions", {}).get("packages") == "write"
        build_step = next(
            step
            for step in platform_job.get("steps", [])
            if isinstance(step, dict) and step.get("name", "").startswith("Build and push")
        )
        build_with = build_step.get("with")
        assert isinstance(build_with, dict)
        assert build_with.get("platforms") == platform
        assert "push-by-digest=true" in build_with.get("outputs", "")
        assert "${{ env.GHCR_IMAGE }},${{ env.DOCKERHUB_IMAGE }}" in build_with.get("outputs", "")
        assert build_with.get("cache-from") == f"type=gha,scope={cache_scope}"
        assert build_with.get("cache-to") == f"type=gha,mode=max,scope={cache_scope}"
        assert build_with.get("provenance") == "mode=max"
        assert build_with.get("sbom") is True

    assert "Set up QEMU" not in {
        step.get("name") for step in amd64.get("steps", []) if isinstance(step, dict)
    }
    assert "Set up QEMU" in {
        step.get("name") for step in arm64.get("steps", []) if isinstance(step, dict)
    }
    assert "Verify ARM64 container security runtime" in {
        step.get("name") for step in arm64.get("steps", []) if isinstance(step, dict)
    }

    assert validate.get("needs") == ["build", "build-amd64"]
    assert validate.get("runs-on") == expected_runner
    validate_names = {
        step.get("name") for step in validate.get("steps", []) if isinstance(step, dict)
    }
    assert {
        "Run Grype scan",
        "Verify packaged static assets",
        "Run smoke tests",
    } <= validate_names

    assert push.get("needs") == [
        "build",
        "build-amd64",
        "build-arm64",
        "validate-amd64",
    ]
    assert push.get("runs-on") == "ubuntu-latest"
    push_names = {step.get("name") for step in push.get("steps", []) if isinstance(step, dict)}
    assert {
        "Checkout",
        "Download platform digests",
        "Set up QEMU",
        "Publish GHCR multi-platform manifest",
        "Publish Docker Hub multi-platform manifest",
        "Validate pushed image metadata",
        "Verify published platform security runtimes",
    } <= push_names
    push_steps = [step for step in push.get("steps", []) if isinstance(step, dict)]
    checkout_step = next(step for step in push_steps if step.get("name") == "Checkout")
    assert checkout_step.get("with", {}).get("ref") == "${{ needs.build.outputs.build-sha }}"
    assert next(
        index for index, step in enumerate(push_steps) if step.get("name") == "Checkout"
    ) < next(
        index
        for index, step in enumerate(push_steps)
        if step.get("name") == "Verify published platform security runtimes"
    )
    assert "Build and push multi-arch" not in push_names
    assert "pattern: release-digest-*" in workflow
    assert "merge-multiple: true" in workflow
    assert "docker buildx imagetools create" in workflow
    assert "GHCR_DIGEST" in workflow
    assert "DOCKERHUB_DIGEST" in workflow
    assert "Published registry digests differ" in workflow


def test_docker_release_benchmark_is_manual_isolated_and_non_release() -> None:
    """Benchmark runs must not mutate either production registry namespace."""
    workflow_path = WORKFLOW_DIR / "docker-release-benchmark.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    data = _load_yaml(workflow_path)
    triggers = data.get(True, data.get("on"))
    assert triggers == {
        "workflow_dispatch": {
            "inputs": {
                "cache_generation": {
                    "description": ("Cache generation; reuse for warm runs, change for cold runs"),
                    "required": True,
                    "default": "v1",
                    "type": "string",
                },
                "cleanup": {
                    "description": ("Delete the temporary GHCR package after the benchmark"),
                    "required": True,
                    "default": True,
                    "type": "boolean",
                },
            }
        }
    }

    jobs = data.get("jobs")
    assert isinstance(jobs, dict)
    assert jobs["build-amd64"].get("runs-on") == [
        "self-hosted",
        "Linux",
        "X64",
        "docker",
    ]
    assert jobs["build-arm64"].get("runs-on") == [
        "self-hosted",
        "Linux",
        "X64",
        "docker",
    ]
    assert jobs["validate-amd64"].get("needs") == ["prepare", "build-amd64"]
    assert jobs["manifest"].get("needs") == [
        "prepare",
        "build-amd64",
        "build-arm64",
        "validate-amd64",
    ]

    assert "REPOSITORY=$(printf '%s' \"${GITHUB_REPOSITORY}\"" in workflow
    assert 'PACKAGE_NAME="pullbox-benchmark-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in workflow
    assert "push-by-digest=true" in workflow
    assert "pullbox-benchmark-amd64-${{ inputs.cache_generation }}" in workflow
    assert "pullbox-benchmark-arm64-${{ inputs.cache_generation }}" in workflow
    assert "Delete isolated GHCR package" in workflow
    assert "if: ${{ always() && inputs.cleanup }}" in workflow
    assert "docker.io/pullbox/pullbox" not in workflow
    assert "DOCKERHUB" not in workflow
    assert "cosign" not in workflow.lower()
    assert "type=raw,value=latest" not in workflow
    assert "type=raw,value=edge" not in workflow
    assert "release-image-digest" not in workflow


def test_docker_release_uses_trigger_tag_without_sha_rediscovery() -> None:
    docker_workflow_path = WORKFLOW_DIR / "docker-release.yml"
    docker_workflow = docker_workflow_path.read_text(encoding="utf-8")
    data = _load_yaml(docker_workflow_path)
    jobs = data.get("jobs")
    assert isinstance(jobs, dict)

    build = jobs.get("build")
    assert isinstance(build, dict)
    assert build.get("outputs", {}).get("release-tag") == "${{ steps.resolve-tag.outputs.tag }}"
    assert build.get("outputs", {}).get("build-sha") == "${{ steps.resolve-sha.outputs.sha }}"

    assert "GITHUB_REF_NAME" in docker_workflow
    assert "GITHUB_REF_TYPE" in docker_workflow
    assert 'TAG="${{ steps.resolve-sha.outputs.release_tag }}"' in docker_workflow
    assert "git tag --points-at" not in docker_workflow
    assert "tag.txt" in docker_workflow


def test_docker_release_prerelease_tags_do_not_update_latest() -> None:
    docker_workflow = (WORKFLOW_DIR / "docker-release.yml").read_text(encoding="utf-8")

    assert "is_prerelease" in docker_workflow
    assert 'echo "is_prerelease=true" >> "$GITHUB_OUTPUT"' in docker_workflow
    assert 'echo "is_prerelease=false" >> "$GITHUB_OUTPUT"' in docker_workflow
    latest_guard = (
        "type=raw,value=latest,enable=${{ steps.resolve-tag.outputs.is_release == 'true' && "
        "steps.resolve-tag.outputs.is_prerelease != 'true' }}"
    )
    assert docker_workflow.count(latest_guard) == 1
    assert (
        "type=raw,value=latest,enable=${{ steps.resolve-tag.outputs.is_release }}"
        not in docker_workflow
    )


def test_grype_config_tracks_current_dhi_runtime() -> None:
    config_text = GRYPE_CONFIG.read_text(encoding="utf-8")
    config = _load_yaml(GRYPE_CONFIG)

    assert "Docker Hardened Images Python 3.14 on Debian 13" in config_text
    assert "python:3.13-slim" not in config_text
    assert "CVE-2026-7210" in config_text
    assert "CVE-2026-11822" in config_text
    assert "CVE-2026-11824" in config_text
    assert "3.14.6" in config_text
    assert config.get("ignore")


def test_docker_workflow_signs_and_verifies_published_images() -> None:
    docker_workflow_path = WORKFLOW_DIR / "docker-release.yml"
    docker_workflow = docker_workflow_path.read_text(encoding="utf-8")
    data = _load_yaml(docker_workflow_path)
    concurrency = data.get("concurrency")
    assert isinstance(concurrency, dict)
    assert concurrency.get("cancel-in-progress") is False

    jobs = data.get("jobs")
    assert isinstance(jobs, dict)
    push_job = jobs.get("push")
    assert isinstance(push_job, dict)
    sign_job = jobs.get("sign")
    assert isinstance(sign_job, dict)

    push_permissions = push_job.get("permissions")
    assert isinstance(push_permissions, dict)
    assert push_permissions.get("packages") == "write"
    assert "id-token" not in push_permissions

    push_outputs = push_job.get("outputs")
    assert isinstance(push_outputs, dict)
    assert push_outputs.get("image-digest") == "${{ steps.inspect.outputs.digest }}"

    steps = push_job.get("steps")
    assert isinstance(steps, list)
    assert push_job.get("runs-on") == "ubuntu-latest"
    assert push_job.get("needs") == [
        "build",
        "build-amd64",
        "build-arm64",
        "validate-amd64",
    ]

    build_job = jobs.get("build")
    assert isinstance(build_job, dict)
    metadata_steps = [
        step
        for step in build_job.get("steps", [])
        if isinstance(step, dict) and step.get("name") == "Docker metadata"
    ]
    assert len(metadata_steps) == 1
    metadata_env = metadata_steps[0].get("env")
    assert isinstance(metadata_env, dict)
    assert metadata_env.get("DOCKER_METADATA_ANNOTATIONS_LEVELS") == "manifest,index"
    metadata_with = metadata_steps[0].get("with")
    assert isinstance(metadata_with, dict)
    assert (
        "org.opencontainers.image.description=Modern comic book management and acquisition platform"
    ) in metadata_with.get("labels", "")
    assert (
        "org.opencontainers.image.description=Modern comic book management and "
        "acquisition platform for self-hosted environments"
    ) in metadata_with.get("annotations", "")
    assert "expected_description = (" in docker_workflow
    assert "Modern comic book management and acquisition platform for " in docker_workflow
    assert "self-hosted environments" in docker_workflow

    assert sign_job.get("runs-on") == "ubuntu-latest"
    assert sign_job.get("needs") == ["build", "push"]
    sign_permissions = sign_job.get("permissions")
    assert isinstance(sign_permissions, dict)
    assert sign_permissions.get("packages") == "write"
    assert sign_permissions.get("id-token") == "write"

    sign_steps = sign_job.get("steps")
    assert isinstance(sign_steps, list)
    sign_step_names = {step.get("name") for step in sign_steps if isinstance(step, dict)}
    assert {
        "Log in to GHCR",
        "Validate Docker Hub authentication",
        "Log in to Docker Hub",
        "Install Cosign",
        "Sign published image digests",
        "Verify published image signatures",
        "Write release image digest artifact",
        "Upload release image digest artifact",
    } <= sign_step_names

    assert "sigstore/cosign-installer@" in docker_workflow
    assert "cosign sign --yes" in docker_workflow
    assert "needs.push.outputs.image-digest" in docker_workflow
    assert "release-image-digest" in docker_workflow
    assert "digest.txt" in docker_workflow
    assert "tag.txt" in docker_workflow
    assert "actions/upload-artifact@" in docker_workflow
    assert "Validate pushed image metadata" in docker_workflow
    assert "python3 - <<'PY'" in docker_workflow
    assert "python - <<'PY'" not in docker_workflow
    assert "org.opencontainers.image.description" in docker_workflow
    assert "attestation-manifest" in docker_workflow
    assert "cosign verify" in docker_workflow
    assert "verify_image_signature()" in docker_workflow
    assert "Signature for ${label} was not discoverable yet" in docker_workflow
    assert "--certificate-identity-regexp" in docker_workflow
    assert "docker-release\\.yml" in docker_workflow
    assert "--certificate-oidc-issuer" in docker_workflow


def test_docker_release_verifies_each_published_platform_runtime() -> None:
    data = _load_yaml(WORKFLOW_DIR / "docker-release.yml")
    push_job = data["jobs"]["push"]
    steps = push_job["steps"]
    step_names = [step.get("name") for step in steps if isinstance(step, dict)]

    assert "Verify published platform security runtimes" in step_names
    verify_step = next(
        step
        for step in steps
        if isinstance(step, dict)
        and step.get("name") == "Verify published platform security runtimes"
    )
    verify_run = verify_step.get("run", "")
    assert "for platform in linux/amd64 linux/arm64" in verify_run
    assert '"${GHCR_IMAGE}@${DIGEST}"' in verify_run
    assert "scripts/verify_container_security_runtime.py" in verify_run
    assert step_names.index("Validate pushed image metadata") < step_names.index(
        "Verify published platform security runtimes"
    )


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


def test_release_workflow_only_runs_for_tag_push_docker_publish() -> None:
    release_workflow_path = WORKFLOW_DIR / "release.yml"
    release_workflow = release_workflow_path.read_text(encoding="utf-8")
    data = _load_yaml(release_workflow_path)
    triggers = data.get(True, data.get("on"))
    assert isinstance(triggers, dict)
    workflow_run = triggers.get("workflow_run")
    assert isinstance(workflow_run, dict)
    assert workflow_run.get("workflows") == ["Docker Release"]

    concurrency = data.get("concurrency")
    assert isinstance(concurrency, dict)
    assert concurrency.get("cancel-in-progress") is False
    group = concurrency.get("group")
    assert isinstance(group, str)
    assert "github.event.workflow_run.event" in group
    assert "github.event.workflow_run.head_sha" in group

    jobs = data.get("jobs")
    assert isinstance(jobs, dict)
    release_job = jobs.get("create-release")
    assert isinstance(release_job, dict)
    release_if = release_job.get("if")
    assert isinstance(release_if, str)
    assert "github.event.workflow_run.conclusion == 'success'" in release_if
    assert "github.event.workflow_run.event == 'push'" in release_if
    assert "release-image-digest/tag.txt" in release_workflow
    assert "TAG=$(tr -d '[:space:]' < \"${TAG_FILE}\")" in release_workflow
    assert 'TAG_SHA=$(git rev-list -n 1 "${TAG}")' in release_workflow
    assert "git tag --points-at" not in release_workflow


def test_release_workflow_marks_only_hyphenated_tags_as_prereleases() -> None:
    release_workflow = (WORKFLOW_DIR / "release.yml").read_text(encoding="utf-8")

    assert '[[ "$TAG" == *-* ]]' in release_workflow
    assert '[[ "$TAG" == v0.* ]]' not in release_workflow
    assert "prerelease: ${{ steps.version.outputs.prerelease == 'true' }}" in release_workflow


def test_release_notes_use_curated_changelog_before_commit_details() -> None:
    release_workflow = (WORKFLOW_DIR / "release.yml").read_text(encoding="utf-8")

    assert "scripts/extract_changelog_section.py" in release_workflow
    assert "--output curated_changelog.md" in release_workflow
    assert "cat curated_changelog.md" in release_workflow
    assert "## Commit Details" in release_workflow
