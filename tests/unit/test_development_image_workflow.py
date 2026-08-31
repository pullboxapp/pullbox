"""Offline behavior contracts for the opt-in signed development image channel."""

from __future__ import annotations

import copy
import importlib.util
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPT = ROOT / ".github" / "scripts" / "validate-development-image.py"
REPOSITORY = "pullboxapp/pullbox"
SHA = "a" * 40


def _workflow(name: str = "docker-release.yml") -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def _step(job: str, name: str) -> dict[str, Any]:
    return next(step for step in _workflow()["jobs"][job]["steps"] if step.get("name") == name)


@pytest.fixture
def validator() -> Any:
    spec = importlib.util.spec_from_file_location("development_image_validator", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(workflow: str = "ci.yml", **overrides: Any) -> dict[str, Any]:
    result = {
        "id": 100,
        "run_attempt": 1,
        "path": f".github/workflows/{workflow}",
        "event": "workflow_dispatch",
        "head_sha": SHA,
        "head_branch": "develop",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
        "check_suite_id": 200,
    }
    result.update(overrides)
    return result


def _suite() -> dict[str, Any]:
    return {
        "id": 200,
        "head_sha": SHA,
        "app": {"slug": "github-actions", "owner": {"login": "github"}},
    }


def _jobs(workflow: str, validator: Any) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "run_id": 100,
            "run_attempt": 1,
            "head_sha": SHA,
            "status": "completed",
            "conclusion": "success",
            "steps": [
                {"name": step, "status": "completed", "conclusion": "success"} for step in steps
            ],
        }
        for name, steps in validator.REQUIRED_JOBS[workflow].items()
    ]


@pytest.mark.parametrize(
    "workflow", ["ci.yml", "security.yml", "workflow-hygiene.yml", "docker-validate.yml"]
)
def test_accepts_complete_exact_commit_manual_validation(validator: Any, workflow: str) -> None:
    validator.validate_evidence(
        workflow, _run(workflow), _suite(), _jobs(workflow, validator), REPOSITORY, SHA
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"head_sha": "b" * 40},
        {"head_branch": "feature/untrusted"},
        {"event": "pull_request"},
        {"path": ".github/workflows/pretend-ci.yml"},
        {"head_repository": {"full_name": "someone/pullbox"}},
        {"repository": {"full_name": "someone/pullbox"}},
        {"status": "in_progress"},
        {"conclusion": "failure"},
        {"conclusion": "skipped"},
    ],
)
def test_rejects_untrusted_stale_or_non_successful_runs(
    validator: Any, overrides: dict[str, Any]
) -> None:
    with pytest.raises(validator.ValidationError):
        validator.validate_evidence(
            "ci.yml", _run(**overrides), _suite(), _jobs("ci.yml", validator), REPOSITORY, SHA
        )


@pytest.mark.parametrize(
    "field,value", [("app", {"slug": "pretend-ci"}), ("head_sha", "b" * 40), ("id", 201)]
)
def test_requires_github_actions_check_suite_provenance(
    validator: Any, field: str, value: Any
) -> None:
    suite = _suite()
    suite[field] = value
    with pytest.raises(validator.ValidationError):
        validator.validate_evidence(
            "ci.yml", _run(), suite, _jobs("ci.yml", validator), REPOSITORY, SHA
        )


@pytest.mark.parametrize(
    "mode",
    [
        "missing",
        "skipped-job",
        "skipped-step",
        "failed-step",
        "other-attempt",
        "other-sha",
        "duplicate",
    ],
)
def test_aggregate_success_cannot_substitute_for_actual_validation(
    validator: Any, mode: str
) -> None:
    jobs = _jobs("ci.yml", validator)
    target = next(job for job in jobs if job["name"] == "Test (Python 3.14)")
    if mode == "missing":
        jobs.remove(target)
    elif mode == "skipped-job":
        target["conclusion"] = "skipped"
    elif mode == "skipped-step":
        target["steps"][0]["conclusion"] = "skipped"
    elif mode == "failed-step":
        target["steps"][0]["conclusion"] = "failure"
    elif mode == "other-attempt":
        target["run_attempt"] = 2
    elif mode == "other-sha":
        target["head_sha"] = "b" * 40
    else:
        jobs.append(copy.deepcopy(target))
    with pytest.raises(validator.ValidationError):
        validator.validate_evidence("ci.yml", _run(), _suite(), jobs, REPOSITORY, SHA)


def test_latest_failed_run_is_not_replaced_with_older_success(validator: Any) -> None:
    latest = validator.latest_run([_run(id=100), _run(id=101, conclusion="failure")])
    assert latest["id"] == 101
    with pytest.raises(validator.ValidationError):
        validator.validate_evidence(
            "ci.yml", latest, _suite(), _jobs("ci.yml", validator), REPOSITORY, SHA
        )


def test_required_job_and_step_names_exist_in_actual_workflows(validator: Any) -> None:
    """Keep the gate tied to real matrix names and meaningful work, not synthetic fixtures."""
    for workflow, requirements in validator.REQUIRED_JOBS.items():
        actual: dict[str, set[str]] = {}
        for job in _workflow(workflow)["jobs"].values():
            name = job["name"]
            steps = {step["name"] for step in job["steps"] if "name" in step}
            if "matrix.python-version" in name:
                for version in job["strategy"]["matrix"]["python-version"]:
                    actual[name.replace("${{ matrix.python-version }}", version)] = steps
            elif "matrix.browser" in name:
                for entry in job["strategy"]["matrix"]["include"]:
                    actual[name.replace("${{ matrix.browser }}", entry["browser"])] = steps
            else:
                actual[name] = steps
        for job_name, required_steps in requirements.items():
            assert job_name in actual
            assert set(required_steps) <= actual[job_name]


@pytest.mark.parametrize(
    "event,ref,allowed",
    [
        ("workflow_dispatch", "refs/heads/develop", True),
        ("workflow_dispatch", "refs/heads/main", False),
        ("workflow_dispatch", "refs/heads/feature/test", False),
        ("workflow_dispatch", "refs/tags/v1.3.0", False),
        ("push", "refs/tags/v1.3.0", True),
        ("push", "refs/heads/develop", False),
    ],
)
def test_dispatch_ref_guard_runs_before_checkout(
    event: str, ref: str, allowed: bool, tmp_path: Path
) -> None:
    steps = _workflow()["jobs"]["build"]["steps"]
    guard = _step("build", "Validate publishing trigger")
    assert steps.index(guard) < next(index for index, step in enumerate(steps) if "uses" in step)
    result = subprocess.run(
        ["bash", "-e", "-c", guard["run"]],
        env={**os.environ, "GITHUB_EVENT_NAME": event, "GITHUB_REF": ref},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is allowed


def test_dev_metadata_is_fixed_and_distinct_per_build_attempt() -> None:
    workflow = _workflow()
    assert workflow.get("on", workflow.get(True))["workflow_dispatch"] in ({}, None)
    tags = _step("build", "Docker metadata")["with"]["tags"]
    assert "tag_override" not in tags
    assert "type=raw,value=edge,enable=${{ github.event_name == 'workflow_dispatch' }}" in tags
    assert "sha-${{ github.sha }}-run-${{ github.run_id }}-${{ github.run_attempt }}" in tags


def test_dev_tags_are_only_promoted_after_both_signatures_verify() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert jobs["build"]["permissions"]["actions"] == "read"
    assert jobs["build"]["permissions"]["checks"] == "read"
    gate = _step("build", "Require validated development commit")
    assert gate["if"] == "github.event_name == 'workflow_dispatch'"
    assert "validate-development-image.py" in gate["run"]
    for name in (
        "Publish GHCR multi-platform manifest",
        "Publish Docker Hub multi-platform manifest",
        "Validate pushed image metadata",
    ):
        tags = _step("push", name)["env"]["TAGS"]
        assert "needs.build.outputs.candidate-tags" in tags
        assert "github.event_name == 'workflow_dispatch'" in tags
    steps = jobs["sign"]["steps"]
    verify = _step("sign", "Verify published image signatures")
    promote = _step("sign", "Promote verified development image")
    assert steps.index(verify) < steps.index(promote)
    assert promote["if"] == "github.event_name == 'workflow_dispatch'"
    assert "@${DIGEST}" in promote["run"]
    assert "--certificate-identity" in verify["run"]
    assert "heads/.*" not in str(verify)


def test_read_only_evidence_fetch_uses_exact_attempt_and_all_pages(
    validator: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def get_json(endpoint: str) -> dict[str, Any]:
        seen.append(endpoint)
        if endpoint.endswith("&page=1"):
            return {"total_count": 101, "jobs": [{"id": number} for number in range(100)]}
        return {"total_count": 101, "jobs": [{"id": 100}]}

    monkeypatch.setattr(validator, "get_json", get_json)
    records = validator.list_all(
        "repos/pullboxapp/pullbox/actions/runs/100/attempts/2/jobs", "jobs"
    )
    assert len(records) == 101
    assert seen == [
        "repos/pullboxapp/pullbox/actions/runs/100/attempts/2/jobs?per_page=100&page=1",
        "repos/pullboxapp/pullbox/actions/runs/100/attempts/2/jobs?per_page=100&page=2",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"total_count": 2, "jobs": [{"id": 1}]},
        {"total_count": 1001, "jobs": []},
        {"jobs": []},
        {"total_count": 0, "jobs": None},
    ],
)
def test_incomplete_or_malformed_evidence_blocks_publication(
    validator: Any, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    monkeypatch.setattr(validator, "get_json", lambda _: payload)
    with pytest.raises(validator.ValidationError):
        validator.list_all("repos/pullboxapp/pullbox/actions/runs/100/attempts/2/jobs", "jobs")


def test_advisory_bandit_findings_are_allowed_but_skipping_is_not(validator: Any) -> None:
    jobs = _jobs("security.yml", validator)
    bandit = next(job for job in jobs if job["name"] == "Bandit")
    step = next(step for step in bandit["steps"] if step["name"] == "Run Bandit")
    step["conclusion"] = "failure"
    validator.validate_evidence(
        "security.yml", _run("security.yml"), _suite(), jobs, REPOSITORY, SHA
    )
    step["conclusion"] = "skipped"
    with pytest.raises(validator.ValidationError):
        validator.validate_evidence(
            "security.yml", _run("security.yml"), _suite(), jobs, REPOSITORY, SHA
        )


def test_main_reads_only_exact_commit_runs_suites_and_attempt_jobs(
    validator: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in {
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_SHA": SHA,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/develop",
    }.items():
        monkeypatch.setenv(key, value)
    seen: list[str] = []
    names = list(validator.REQUIRED_JOBS)

    def get_json(endpoint: str) -> dict[str, Any]:
        seen.append(endpoint)
        for index, workflow in enumerate(names):
            if f"/workflows/{workflow}/runs?" in endpoint:
                assert f"head_sha={SHA}" in endpoint
                assert "event=workflow_dispatch&branch=develop" in endpoint
                return {
                    "total_count": 1,
                    "workflow_runs": [_run(workflow, id=100 + index, check_suite_id=200 + index)],
                }
            if f"/check-suites/{200 + index}" in endpoint:
                return {**_suite(), "id": 200 + index}
            if f"/runs/{100 + index}/attempts/1/jobs?" in endpoint:
                jobs = _jobs(workflow, validator)
                for job in jobs:
                    job["run_id"] = 100 + index
                return {"total_count": len(jobs), "jobs": jobs}
        raise AssertionError(f"Unexpected endpoint: {endpoint}")

    monkeypatch.setattr(validator, "get_json", get_json)
    assert validator.main() == 0
    assert len(seen) == 12


def test_main_rejects_feature_dispatch_before_fetching_evidence(
    validator: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in {
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_SHA": SHA,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/feature/test",
    }.items():
        monkeypatch.setenv(key, value)

    def unexpected_fetch(_: str) -> dict[str, Any]:
        pytest.fail("Untrusted dispatch must not query validation evidence.")

    monkeypatch.setattr(validator, "get_json", unexpected_fetch)
    assert validator.main() == 1


@pytest.mark.parametrize("signature_failure", ["", "ghcr", "dockerhub"])
def test_shell_publication_promotes_only_verified_digest_without_rebuilding(
    signature_failure: str, tmp_path: Path
) -> None:
    """Execute the actual shell steps with fake commands; never contact Docker or a registry."""
    digest = "sha256:" + "b" * 64
    ghcr = "ghcr.io/pullboxapp/pullbox"
    dockerhub = "docker.io/pullbox/pullbox"
    tag = f"sha-{SHA}-run-123-1"
    fake_commands = r"""
    cosign() {
      if [ "$1" = "verify" ]; then
        case "$SIGNATURE_FAILURE:$*" in
          ghcr:*ghcr.io/*|dockerhub:*docker.io/*) return 1 ;;
        esac
      fi
      return 0
    }
    sleep() { :; }
    docker() {
      if [ "$3" = "create" ]; then
        printf 'CREATE %s\n' "$*" >&2
      elif [ "$3" = "inspect" ]; then
        printf '{"digest":"%s"}\n' "$DIGEST"
      else
        return 99
      fi
    }
    """
    script = fake_commands + "\n".join(
        _step("sign", name)["run"]
        for name in (
            "Sign published image digests",
            "Verify published image signatures",
            "Promote verified development image",
        )
    )
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={
            **os.environ,
            "GITHUB_SHA": SHA,
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "1",
            "GHCR_IMAGE": ghcr,
            "DOCKERHUB_IMAGE": dockerhub,
            "DIGEST": digest,
            "BUILD_TAG": tag,
            "TAGS": f"{ghcr}:edge\n{ghcr}:{tag}\n{dockerhub}:edge\n{dockerhub}:{tag}",
            "SIGNATURE_FAILURE": signature_failure,
            "CERTIFICATE_IDENTITY": "https://github.com/pullboxapp/pullbox/.github/workflows/docker-release.yml@refs/heads/develop",
            "CERTIFICATE_OIDC_ISSUER": "https://token.actions.githubusercontent.com",
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if signature_failure:
        assert result.returncode != 0
        assert "CREATE" not in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stderr.splitlines() == [
            f"CREATE buildx imagetools create --tag {registry}:edge "
            f"--tag {registry}:{tag} {registry}@{digest}"
            for registry in (ghcr, dockerhub)
        ]


def test_selective_rerun_cannot_promote_rebuilt_digest_under_prior_attempt_tag(
    tmp_path: Path,
) -> None:
    """GitHub retains prepare outputs when only failed/dependent jobs are rerun."""
    previous_build_tag = f"sha-{SHA}-run-123-1"
    rebuilt_digest = "sha256:" + "c" * 64
    ghcr = "ghcr.io/pullboxapp/pullbox"
    dockerhub = "docker.io/pullbox/pullbox"
    fake_docker = r"""
    docker() {
      if [ "$3" = "create" ]; then
        printf 'CREATE %s\n' "$*" >&2
      elif [ "$3" = "inspect" ]; then
        printf '{"digest":"%s"}\n' "$DIGEST"
      else
        return 99
      fi
    }
    """
    result = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            fake_docker + _step("sign", "Promote verified development image")["run"],
        ],
        env={
            **os.environ,
            "GITHUB_SHA": SHA,
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2",
            "BUILD_TAG": previous_build_tag,
            "DIGEST": rebuilt_digest,
            "GHCR_IMAGE": ghcr,
            "DOCKERHUB_IMAGE": dockerhub,
            "TAGS": (
                f"{ghcr}:edge\n{ghcr}:{previous_build_tag}\n"
                f"{dockerhub}:edge\n{dockerhub}:{previous_build_tag}"
            ),
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, "Selective rerun must require fresh preparation."
    assert "CREATE" not in result.stderr


@pytest.mark.parametrize("job", ["build-amd64", "build-arm64"])
@pytest.mark.parametrize("prepared_attempt,allowed", [("1", False), ("2", True)])
def test_selective_rerun_guard_precedes_platform_checkout(
    job: str, prepared_attempt: str, allowed: bool, tmp_path: Path
) -> None:
    steps = _workflow()["jobs"][job]["steps"]
    guard = _step(job, "Validate development build attempt")
    assert guard["if"] == "github.event_name == 'workflow_dispatch'"
    assert steps.index(guard) < next(index for index, step in enumerate(steps) if "uses" in step)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", guard["run"]],
        env={
            **os.environ,
            "GITHUB_SHA": SHA,
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "2",
            "BUILD_TAG": f"sha-{SHA}-run-123-{prepared_attempt}",
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is allowed
