"""Tests for container runtime security assertions."""

from __future__ import annotations

import pytest

from scripts.verify_container_security_runtime import verify_expat_version


def test_verify_expat_version_accepts_reviewed_minimum() -> None:
    verify_expat_version((2, 8, 1))


def test_verify_expat_version_rejects_older_runtime() -> None:
    with pytest.raises(SystemExit, match=r"Expat 2\.8\.1 or newer is required"):
        verify_expat_version((2, 7, 1))
