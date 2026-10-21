"""Keep fresh installs on qualified and patched dependency lines."""

import tomllib
from pathlib import Path

from packaging.requirements import Requirement


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
