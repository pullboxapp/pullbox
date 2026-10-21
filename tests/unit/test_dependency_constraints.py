"""Keep fresh installs on qualified and patched dependency lines."""

import json
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version


def test_sqlalchemy_dependency_stays_on_qualified_21_line() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())
    sqlalchemy = next(
        requirement
        for dependency in project["project"]["dependencies"]
        if (requirement := Requirement(dependency)).name == "sqlalchemy"
    )

    assert "asyncio" in sqlalchemy.extras
    assert "2.0.54" not in sqlalchemy.specifier
    assert "2.1.1" not in sqlalchemy.specifier
    assert "2.1.3" not in sqlalchemy.specifier
    assert "2.1.4" in sqlalchemy.specifier
    assert "2.1.5" in sqlalchemy.specifier
    assert "2.2.0" not in sqlalchemy.specifier
    assert "3.0.0" not in sqlalchemy.specifier


def test_pillow_dependency_requires_patched_13_line() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text())
    pillow = next(
        requirement
        for dependency in project["project"]["dependencies"]
        if (requirement := Requirement(dependency)).name == "pillow"
    )

    assert "12.3.0" not in pillow.specifier
    assert "13.0.0" in pillow.specifier
    assert "13.0.1" in pillow.specifier
    assert "14.0.0" not in pillow.specifier


def test_tailwind_lockfile_uses_patched_source_map_parser() -> None:
    lockfile = json.loads(Path("package-lock.json").read_text())
    parsers = [
        package
        for path, package in lockfile["packages"].items()
        if path.endswith("node_modules/source-map-js")
    ]

    assert parsers
    assert all(Version(package["version"]) >= Version("1.2.2") for package in parsers)


def test_stylesheet_watcher_does_not_reintroduce_vulnerable_braces() -> None:
    packages = json.loads(Path("package-lock.json").read_text())["packages"]

    assert not any(path.endswith("node_modules/braces") for path in packages)
    assert Version(packages["node_modules/@parcel/watcher"]["version"]) >= Version("2.6.0")
