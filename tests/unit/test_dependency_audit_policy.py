"""The advisory Safety tool must not weaken the blocking dependency gate."""

from __future__ import annotations

import copy
import json
import subprocess
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import pytest

from scripts import run_dependency_audit as audit

if TYPE_CHECKING:
    from pathlib import Path

TODAY = date(2026, 9, 3)
ADVISORY_IDS = ["PYSEC-2026-3740", "GHSA-8mgp-746c-j5xp", "CVE-2026-81726"]


@pytest.fixture
def report() -> dict[str, Any]:
    return {
        "dependencies": [
            {"name": "safety", "version": "3.8.1", "vulns": []},
            {
                "name": "nltk",
                "version": "3.10.3",
                "vulns": [{"id": ADVISORY_IDS[0], "aliases": ADVISORY_IDS[1:], "fix_versions": []}],
            },
        ],
        "fixes": [],
    }


@pytest.mark.parametrize("advisory_id", ADVISORY_IDS)
def test_only_reviewed_development_finding_is_temporarily_accepted(
    report: dict[str, Any], advisory_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    report["dependencies"][1]["vulns"][0]["id"] = advisory_id

    assert audit.evaluate_report(report, 1, {"fastapi"}, TODAY) == 0
    output = capsys.readouterr().out
    assert "ACCEPTED" in output
    assert "nltk==3.10.3" in output
    assert "2026-10-03" in output


@pytest.mark.parametrize("day", [date(2026, 10, 3), date(2027, 1, 1)])
def test_exception_expires_at_start_of_review_date(report: dict[str, Any], day: date) -> None:
    assert audit.evaluate_report(report, 1, set(), day) == 1


def test_exception_still_valid_day_before_expiry(report: dict[str, Any]) -> None:
    assert audit.evaluate_report(report, 1, set(), date(2026, 10, 2)) == 0


@pytest.mark.parametrize("package,version", [(0, "3.8.2"), (1, "3.10.2"), (1, "3.10.4")])
def test_changed_dependency_versions_require_new_review(
    report: dict[str, Any], package: int, version: str
) -> None:
    report["dependencies"][package]["version"] = version
    assert audit.evaluate_report(report, 1, set(), TODAY) == 1


@pytest.mark.parametrize("name", ["safety", "nltk"])
def test_runtime_dependency_is_never_accepted(report: dict[str, Any], name: str) -> None:
    assert audit.evaluate_report(report, 1, {name}, TODAY) == 1


def test_missing_safety_is_not_the_reviewed_dependency_path(report: dict[str, Any]) -> None:
    report["dependencies"].pop(0)
    assert audit.evaluate_report(report, 1, set(), TODAY) == 1


@pytest.mark.parametrize("name", ["nltk", "another-package"])
def test_other_findings_still_block(report: dict[str, Any], name: str) -> None:
    extra = copy.deepcopy(report["dependencies"][1])
    extra["name"] = name
    extra["vulns"][0] = {"id": "CVE-OTHER", "aliases": [], "fix_versions": []}
    if name == "nltk":
        report["dependencies"][1]["vulns"].extend(extra["vulns"])
    else:
        report["dependencies"].append(extra)
    assert audit.evaluate_report(report, 1, set(), TODAY) == 1


def test_same_advisory_on_other_package_is_not_accepted(report: dict[str, Any]) -> None:
    report["dependencies"][1]["name"] = "not-nltk"
    assert audit.evaluate_report(report, 1, set(), TODAY) == 1


def test_available_fix_requires_upgrade_instead_of_exception(report: dict[str, Any]) -> None:
    report["dependencies"][1]["vulns"][0]["fix_versions"] = ["3.10.4"]
    assert audit.evaluate_report(report, 1, set(), TODAY) == 1


@pytest.mark.parametrize("bad_report", [None, {}, {"dependencies": []}, {"dependencies": [None]}])
def test_missing_or_malformed_report_fails_closed(bad_report: object) -> None:
    assert audit.evaluate_report(bad_report, 0, set(), TODAY) == 1


@pytest.mark.parametrize(
    "broken",
    [
        {"name": "nltk", "skip_reason": "collection failed"},
        {"name": "nltk", "version": "3.10.3"},
        {"name": "nltk", "version": "3.10.3", "vulns": [None]},
        {"name": "nltk", "version": "3.10.3", "vulns": [{"id": None}]},
    ],
)
def test_incomplete_dependency_collection_fails_closed(
    report: dict[str, Any], broken: dict[str, Any]
) -> None:
    report["dependencies"][1] = broken
    assert audit.evaluate_report(report, 1, set(), TODAY) == 1


def test_duplicate_dependency_records_fail_closed(report: dict[str, Any]) -> None:
    report["dependencies"].append(copy.deepcopy(report["dependencies"][1]))
    assert audit.evaluate_report(report, 1, set(), TODAY) == 1


@pytest.mark.parametrize("status", [0, 2, -9])
def test_scanner_failure_or_inconsistent_status_cannot_be_overridden(
    report: dict[str, Any], status: int
) -> None:
    assert audit.evaluate_report(report, status, set(), TODAY) == 1


def test_clean_scan_passes_even_after_exception_expiry(report: dict[str, Any]) -> None:
    report["dependencies"][1]["vulns"] = []
    assert audit.evaluate_report(report, 0, set(), date(2027, 1, 1)) == 0
    assert audit.evaluate_report(report, 1, set(), TODAY) == 1


def test_runtime_scope_includes_all_non_development_extras(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[project]\ndependencies=["FastAPI>=1"]\n'
        "[project.optional-dependencies]\n"
        'dev=["safety~=3.0", "nltk>=3.10.0"]\n'
        'e2e=["playwright"]\n'
        'prod=["nltk>=3.10.3"]\n'
        "postgres=[\"Safety>=3.8.1; python_version >= '3.12'\"]\n",
        encoding="utf-8",
    )
    assert audit.runtime_dependencies(project) == {"fastapi", "nltk", "safety"}


def test_runner_preserves_raw_report_and_does_not_globally_ignore_nltk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: dict[str, Any]
) -> None:
    raw = json.dumps(report)
    calls: list[list[str]] = []

    def scan(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(command, 1, stdout=raw, stderr="Found 1 vulnerability\n")

    monkeypatch.setattr(subprocess, "run", scan)
    evidence = tmp_path / "audit.json"
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("safety==3.8.1\nnltk==3.10.3\n", encoding="utf-8")
    assert audit.run_audit(requirements, evidence, today=TODAY) == 0
    assert evidence.read_text() == raw
    command = calls[0]
    assert "pip_audit" in command
    assert "--strict" in command
    assert "--no-deps" in command
    assert "--disable-pip" in command
    assert command[command.index("--vulnerability-service") + 1] == "pypi"
    assert command[command.index("--format") + 1] == "json"
    assert command[command.index("--aliases") + 1] == "on"
    assert command[command.index("--ignore-vuln") + 1] == "CVE-2026-4539"
    assert all(advisory not in command for advisory in ADVISORY_IDS)


@pytest.mark.parametrize("requirements_text", ["", "safety==3.8.1\nnltk==3.10.3\nmissing==1.0\n"])
def test_runner_rejects_empty_inventory_or_partial_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: dict[str, Any], requirements_text: str
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(requirements_text, encoding="utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 1, stdout=json.dumps(report), stderr=""
        ),
    )
    assert audit.run_audit(requirements, tmp_path / "audit.json", today=TODAY) == 1


@pytest.mark.parametrize("pins", ["safety==3.8.2\nnltk==3.10.4\n", "safety~=3.8\nnltk>=3.10\n"])
def test_report_must_match_exact_exported_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: dict[str, Any], pins: str
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(pins, encoding="utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 1, stdout=json.dumps(report), stderr=""
        ),
    )
    assert audit.run_audit(requirements, tmp_path / "audit.json", today=TODAY) == 1


def test_cli_evaluates_expiry_after_scanner_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: dict[str, Any]
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("safety==3.8.1\nnltk==3.10.3\n", encoding="utf-8")
    current_time = datetime(2026, 10, 2, 23, 59, 59, tzinfo=UTC)

    class Clock:
        @staticmethod
        def now(tz: object) -> datetime:
            return current_time

    def scan(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal current_time
        current_time = datetime(2026, 10, 3, 0, 0, 1, tzinfo=UTC)
        return subprocess.CompletedProcess([], 1, stdout=json.dumps(report), stderr="")

    monkeypatch.setattr(audit, "datetime", Clock)
    monkeypatch.setattr(subprocess, "run", scan)
    monkeypatch.setattr(
        "sys.argv", ["audit", "-r", str(requirements), "--report", str(tmp_path / "audit.json")]
    )
    assert audit.main() == 1


@pytest.mark.parametrize("raw", ["", "not JSON", '{"dependencies": []}'])
def test_runner_does_not_reuse_stale_success_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    evidence = tmp_path / "audit.json"
    evidence.write_text("stale successful report", encoding="utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, stdout=raw, stderr="failed"),
    )
    assert audit.run_audit(tmp_path / "requirements.txt", evidence, today=TODAY) == 1
    assert evidence.read_text() == raw
