"""Tests for container runtime security assertions."""

from __future__ import annotations

import pytest

from scripts import verify_container_security_runtime as runtime
from scripts.verify_container_security_runtime import verify_expat_version


def test_verify_expat_version_accepts_reviewed_minimum() -> None:
    verify_expat_version((2, 8, 1))


def test_verify_expat_version_rejects_older_runtime() -> None:
    with pytest.raises(SystemExit, match=r"Expat 2\.8\.1 or newer is required"):
        verify_expat_version((2, 7, 1))


@pytest.mark.parametrize("package", ["nltk", "safety"])
def test_production_check_rejects_development_scanner_packages(
    monkeypatch: pytest.MonkeyPatch, package: str
) -> None:
    monkeypatch.setattr(runtime, "find_spec", lambda name: object() if name == package else None)
    with pytest.raises(SystemExit, match=package):
        runtime.main()


def test_production_check_accepts_runtime_without_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "find_spec", lambda name: None)
    monkeypatch.setattr(runtime.pyexpat, "version_info", runtime.MINIMUM_EXPAT_VERSION)
    runtime.main()
