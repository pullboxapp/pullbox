"""Run the blocking dependency audit with narrowly reviewed exceptions."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import subprocess
import sys
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
NLTK_ADVISORY_IDS = {"PYSEC-2026-3740", "GHSA-8mgp-746c-j5xp", "CVE-2026-81726"}
NLTK_EXCEPTION_EXPIRES = date(2026, 10, 14)
SAFETY_SOURCE_COMMIT = "ac505430fc55403af338ece39489f3e3b01a7d97"
SAFETY_SOURCE_URL = f"https://github.com/pyupio/safety/archive/{SAFETY_SOURCE_COMMIT}.tar.gz"
SAFETY_SOURCE_SHA256 = "01ec8c456a52a8b2605cf87ba0f422c62af815dbe02728dc9251a7ac72e0820a"
SAFETY_SOURCE_VERSION = "3.8.1"


def audit_requirements(frozen: str) -> tuple[list[Requirement], dict[str, Any]]:
    """Keep every frozen package; normalize only the reviewed upstream Safety build."""
    expected: list[Requirement] = []
    provenance: dict[str, Any] = {}
    seen = set()
    for line in frozen.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        requirement = Requirement(line)
        name = canonicalize_name(requirement.name)
        if name in seen or requirement.marker or requirement.extras:
            raise ValueError(f"Duplicate or conditional audit requirement: {name}")
        seen.add(name)
        if requirement.url:
            if name != "safety" or requirement.url != (
                f"{SAFETY_SOURCE_URL}#sha256={SAFETY_SOURCE_SHA256}"
            ):
                raise ValueError(f"Unreviewed source requirement: {name}")
            installed = importlib.metadata.distribution("safety")
            direct_url = json.loads(installed.read_text("direct_url.json") or "null")
            reviewed_url = {
                "url": SAFETY_SOURCE_URL,
                "archive_info": {
                    "hash": f"sha256={SAFETY_SOURCE_SHA256}",
                    "hashes": {"sha256": SAFETY_SOURCE_SHA256},
                },
            }
            if (
                direct_url != reviewed_url
                or installed.version != SAFETY_SOURCE_VERSION
                or installed.requires is None
                or any(
                    canonicalize_name(Requirement(dep).name) == "nltk" for dep in installed.requires
                )
            ):
                raise ValueError("Installed Safety does not match the reviewed NLTK-free source")
            requirement = Requirement(f"safety=={installed.version}")
            provenance[name] = {
                "frozen_requirement": line.strip(),
                "audited_requirement": str(requirement),
                "direct_url": direct_url,
            }
        pins = list(requirement.specifier)
        if len(pins) != 1 or pins[0].operator not in {"==", "==="} or "*" in pins[0].version:
            raise ValueError(f"Expected an exact installed-version pin for {name}")
        expected.append(requirement)
    if not expected:
        raise ValueError("Empty requirements export")
    return expected, provenance


def _dependencies(report: object) -> dict[str, dict[str, Any]]:
    if not isinstance(report, dict) or not isinstance(report.get("dependencies"), list):
        raise ValueError("Missing dependency audit inventory")
    dependencies: dict[str, dict[str, Any]] = {}
    for dep in report["dependencies"]:
        if (
            not isinstance(dep, dict)
            or "skip_reason" in dep
            or not isinstance(dep.get("name"), str)
            or not dep["name"]
            or not isinstance(dep.get("version"), str)
            or not dep["version"]
            or not isinstance(dep.get("vulns"), list)
        ):
            raise ValueError("Incomplete or malformed dependency audit record")
        name = canonicalize_name(dep["name"])
        if name in dependencies:
            raise ValueError(f"Duplicate dependency audit record: {name}")
        for vuln in dep["vulns"]:
            if (
                not isinstance(vuln, dict)
                or not isinstance(vuln.get("id"), str)
                or not vuln["id"]
                or not isinstance(vuln.get("aliases"), list)
                or not all(isinstance(alias, str) and alias for alias in vuln["aliases"])
                or not isinstance(vuln.get("fix_versions"), list)
                or not all(isinstance(version, str) for version in vuln["fix_versions"])
            ):
                raise ValueError(f"Malformed vulnerability record for {name}")
        dependencies[name] = dep
    if not dependencies:
        raise ValueError("Empty dependency audit inventory")
    return dependencies


def evaluate_report(
    report: object,
    scanner_status: int,
    runtime_dependencies: set[str],
    today: date,
) -> int:
    """Return a failing status unless the complete audit satisfies policy."""
    try:
        dependencies = _dependencies(report)
        findings = sum(len(dep["vulns"]) for dep in dependencies.values())
        if scanner_status != (1 if findings else 0):
            raise ValueError(
                f"Scanner failed or returned inconsistent evidence (exit {scanner_status})"
            )
    except ValueError as exc:
        print(f"BLOCKED: {exc}")
        return 1

    development_only = not {"safety", "nltk"}.intersection(runtime_dependencies)
    reviewed_tool = dependencies.get("safety", {}).get("version") == "3.8.1"
    blocked = 0
    accepted = 0
    for name, dep in dependencies.items():
        for vuln in dep["vulns"]:
            if (
                name == "nltk"
                and dep["version"] == "3.10.3"
                and vuln["id"] in NLTK_ADVISORY_IDS
                and set(vuln["aliases"]) <= NLTK_ADVISORY_IDS
                and not vuln["fix_versions"]
                and reviewed_tool
                and development_only
                and today < NLTK_EXCEPTION_EXPIRES
            ):
                accepted += 1
                print(
                    f"ACCEPTED (temporary risk): nltk==3.10.3 {vuln['id']}; "
                    f"Safety 3.8.1 development-only edit_distance use; "
                    f"expires {NLTK_EXCEPTION_EXPIRES} 00:00 UTC"
                )
            else:
                blocked += 1
                print(f"BLOCKED: {name}=={dep['version']} {vuln['id']}")
                if name == "nltk" and vuln["id"] in NLTK_ADVISORY_IDS:
                    print(
                        "NLTK exception expired or scope changed; upgrade or obtain a new review."
                    )
    print(f"Dependency audit: {blocked} blocking finding(s), {accepted} temporary exception(s).")
    return int(blocked > 0)


def runtime_dependencies(project: Path) -> set[str]:
    """Read every dependency group that can ship in the runtime."""
    with project.open("rb") as stream:
        config = tomllib.load(stream)["project"]
    requirements = list(config["dependencies"])
    for group, extra in config.get("optional-dependencies", {}).items():
        if group not in {"dev", "e2e"}:
            requirements.extend(extra)
    return {canonicalize_name(Requirement(value).name) for value in requirements}


def run_audit(requirements: Path, report_path: Path, *, today: date | None = None) -> int:
    """Collect and evaluate the real scanner report, preserving evidence."""
    try:
        report_path.unlink(missing_ok=True)
        provenance_path = report_path.with_name(f"{report_path.stem}-provenance.json")
        provenance_path.unlink(missing_ok=True)
        expected, provenance = audit_requirements(requirements.read_text(encoding="utf-8"))
        provenance_path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
        with TemporaryDirectory(prefix="pullbox-audit-") as directory:
            normalized = Path(directory) / "requirements.txt"
            normalized.write_text("".join(f"{req}\n" for req in expected), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip_audit",
                    "--strict",
                    # pip freeze already includes transitives; audit every exact installed version.
                    "--no-deps",
                    "--disable-pip",
                    "--vulnerability-service",
                    "pypi",
                    "--desc",
                    "on",
                    "--aliases",
                    "on",
                    "--format",
                    "json",
                    "--output",
                    "stdout",
                    "-r",
                    str(normalized),
                    # Preserve the pre-existing Pygments exception; do not globally ignore NLTK.
                    "--ignore-vuln",
                    "CVE-2026-4539",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        report_path.write_text(result.stdout, encoding="utf-8")
        print(result.stderr, end="", file=sys.stderr)
        report = json.loads(result.stdout)
        dependencies = _dependencies(report)
        for requirement in expected:
            name = canonicalize_name(requirement.name)
            if name not in dependencies or not requirement.specifier.contains(
                dependencies[name]["version"], prereleases=True
            ):
                raise ValueError(f"Incomplete or version-mismatched audit inventory: {name}")
        return evaluate_report(
            report,
            result.returncode,
            runtime_dependencies(PROJECT),
            today or datetime.now(UTC).date(),
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        importlib.metadata.PackageNotFoundError,
    ) as exc:
        print(f"BLOCKED: dependency audit could not complete: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-r", "--requirements", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=Path("dependency-audit-report.json"))
    args = parser.parse_args()
    return run_audit(args.requirements, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
