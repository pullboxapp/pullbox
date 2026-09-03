"""Run the blocking dependency audit with narrowly reviewed exceptions."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

PROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
NLTK_ADVISORY_IDS = {"PYSEC-2026-3740", "GHSA-8mgp-746c-j5xp", "CVE-2026-81726"}
NLTK_EXCEPTION_EXPIRES = date(2026, 10, 3)


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
                str(requirements),
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
        expected = [
            Requirement(line)
            for line in requirements.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not expected:
            raise ValueError("Empty requirements export")
        for requirement in expected:
            name = canonicalize_name(requirement.name)
            pins = list(requirement.specifier)
            if (
                len(pins) != 1
                or pins[0].operator not in {"==", "==="}
                or "*" in pins[0].version
                or requirement.url
                or requirement.marker
            ):
                raise ValueError(f"Expected an exact installed-version pin for {name}")
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
    except (OSError, ValueError, KeyError, TypeError) as exc:
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
