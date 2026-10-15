"""The reviewed Safety source pin must stay in the complete blocking audit."""

from __future__ import annotations

import copy
import importlib.metadata
import json
import subprocess
import tomllib
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from packaging.requirements import Requirement

from scripts import run_dependency_audit as audit

COMMIT = "ac505430fc55403af338ece39489f3e3b01a7d97"
URL = f"https://github.com/pyupio/safety/archive/{COMMIT}.tar.gz"
SHA256 = "01ec8c456a52a8b2605cf87ba0f422c62af815dbe02728dc9251a7ac72e0820a"
PIN = f"safety @ {URL}#sha256={SHA256}"
DIRECT_URL = {
    "url": URL,
    "archive_info": {"hash": f"sha256={SHA256}", "hashes": {"sha256": SHA256}},
}


def test_dev_toolchain_uses_merged_nltk_free_safety_source() -> None:
    config = tomllib.loads(audit.PROJECT.read_text())
    dev = {
        Requirement(value).name: Requirement(value)
        for value in config["project"]["optional-dependencies"]["dev"]
    }
    assert "nltk" not in dev
    assert str(dev["safety"]) == PIN


@pytest.fixture
def source_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        provenance=copy.deepcopy(DIRECT_URL),
        version="3.8.1",
        requires=["safety-schemas==0.0.19"],
        calls=[],
        report={
            "dependencies": [
                {"name": "safety", "version": "3.8.1", "vulns": []},
                {"name": "safety-schemas", "version": "0.0.19", "vulns": []},
            ]
        },
        status=0,
        requirements=tmp_path / "freeze.txt",
        evidence=tmp_path / "audit.json",
    )
    state.requirements.write_text(PIN + "\nsafety-schemas==0.0.19\n")

    def distribution(name: str) -> SimpleNamespace:
        assert name == "safety"
        return SimpleNamespace(
            version=state.version,
            requires=state.requires,
            read_text=lambda filename: (
                json.dumps(state.provenance) if state.provenance is not None else None
            ),
        )

    def scan(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        state.calls.append((command, Path(command[command.index("-r") + 1]).read_text()))
        return subprocess.CompletedProcess(
            command, state.status, stdout=json.dumps(state.report), stderr=""
        )

    monkeypatch.setattr(importlib.metadata, "distribution", distribution)
    monkeypatch.setattr(subprocess, "run", scan)
    return state


def run(state: SimpleNamespace) -> int:
    return audit.run_audit(state.requirements, state.evidence, today=date(2026, 10, 14))


def test_reviewed_source_is_audited_by_version_without_losing_transitives(
    source_scan: SimpleNamespace,
) -> None:
    assert run(source_scan) == 0
    command, scanned = source_scan.calls[0]
    assert scanned == "safety==3.8.1\nsafety-schemas==0.0.19\n"
    assert "--strict" in command
    assert command[command.index("--vulnerability-service") + 1] == "pypi"
    assert source_scan.requirements.read_text().startswith(PIN)
    assert json.loads(source_scan.evidence.read_text()) == source_scan.report
    provenance = json.loads(source_scan.evidence.with_name("audit-provenance.json").read_text())
    assert provenance["safety"]["direct_url"] == DIRECT_URL
    assert provenance["safety"]["audited_requirement"] == "safety==3.8.1"
    assert provenance["safety"]["frozen_requirement"] == PIN


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "malformed",
        "wrong_url",
        "wrong_hash",
        "wrong_hashes",
        "missing_hashes",
        "editable",
        "vcs",
        "version",
        "nltk_dependency",
    ],
)
def test_unreviewed_installed_source_blocks_before_scanning(
    source_scan: SimpleNamespace, change: str
) -> None:
    if change == "missing":
        source_scan.provenance = None
    elif change == "malformed":
        source_scan.provenance = []
    elif change == "wrong_url":
        source_scan.provenance["url"] = "https://example.com/safety.git"
    elif change == "wrong_hash":
        source_scan.provenance["archive_info"]["hash"] = "sha256=unreviewed"
    elif change == "wrong_hashes":
        source_scan.provenance["archive_info"]["hashes"]["sha256"] = "unreviewed"
    elif change == "missing_hashes":
        source_scan.provenance["archive_info"].pop("hashes")
    elif change == "editable":
        source_scan.provenance["dir_info"] = {"editable": True}
    elif change == "vcs":
        source_scan.provenance["vcs_info"] = {"vcs": "git", "commit_id": COMMIT}
    elif change == "version":
        source_scan.version = "3.8.2"
    else:
        source_scan.requires.append("NLTK>=3.9")
    assert run(source_scan) == 1
    assert not source_scan.calls


@pytest.mark.parametrize(
    "pin",
    [
        PIN.replace(COMMIT, "main"),
        PIN.replace("pyupio", "unreviewed"),
        PIN.replace("safety @", "other @"),
        PIN + "; python_version >= '3.12'",
        PIN.replace("safety @", "safety[extra] @"),
    ],
    ids=["moving_ref", "wrong_repository", "other_package", "marker", "extras"],
)
def test_other_url_forms_are_not_silently_normalized(
    source_scan: SimpleNamespace, pin: str
) -> None:
    source_scan.requirements.write_text(pin + "\nsafety-schemas==0.0.19\n")
    assert run(source_scan) == 1
    assert not source_scan.calls


@pytest.mark.parametrize("package", ["safety", "safety-schemas"])
def test_source_normalization_does_not_allow_partial_scan(
    source_scan: SimpleNamespace, package: str
) -> None:
    source_scan.report["dependencies"] = [
        dep for dep in source_scan.report["dependencies"] if dep["name"] != package
    ]
    assert run(source_scan) == 1


def test_source_normalization_never_suppresses_findings(source_scan: SimpleNamespace) -> None:
    source_scan.report["dependencies"][0]["vulns"] = [
        {"id": "CVE-OTHER", "aliases": [], "fix_versions": []}
    ]
    source_scan.status = 1
    assert run(source_scan) == 1
