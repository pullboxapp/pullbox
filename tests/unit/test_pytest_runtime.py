"""Keep independent CI jobs and xdist workers out of each other's fallback state."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.conftest import _configure_worker_runtime_environment, pytest_make_parametrize_id


@pytest.fixture
def clean_runtime_env(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for key in list(os.environ):
        if key.startswith(("PULLBOX_", "PYTEST_XDIST_", "_PULLBOX_PYTEST_")):
            monkeypatch.delenv(key)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    return monkeypatch


def _runtime_values() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key.startswith("PULLBOX_")}


def test_separate_jobs_with_same_worker_name_do_not_share_runtime(clean_runtime_env):
    clean_runtime_env.setenv("PYTEST_XDIST_WORKER", "gw0")
    clean_runtime_env.setenv("PYTEST_XDIST_TESTRUNUID", "job-a")
    _configure_worker_runtime_environment()
    first = os.environ["PULLBOX_DB_URL"]
    clean_runtime_env.setenv("PYTEST_XDIST_TESTRUNUID", "job-b")
    _configure_worker_runtime_environment()
    assert os.environ["PULLBOX_DB_URL"] != first


def test_worker_replaces_inherited_controller_defaults(clean_runtime_env):
    _configure_worker_runtime_environment()
    controller = _runtime_values()
    clean_runtime_env.setenv("PYTEST_XDIST_WORKER", "gw0")
    clean_runtime_env.setenv("PYTEST_XDIST_TESTRUNUID", "job-a")
    _configure_worker_runtime_environment()
    worker = _runtime_values()
    for key in controller.keys() - {"PULLBOX_ALLOW_WEAK_SECRET_FOR_TESTS"}:
        assert worker[key] != controller[key], key
    clean_runtime_env.setenv("PYTEST_XDIST_WORKER", "gw1")
    _configure_worker_runtime_environment()
    assert os.environ["PULLBOX_DB_URL"] != worker["PULLBOX_DB_URL"]


def test_serial_invocations_get_distinct_runtime_roots(clean_runtime_env):
    _configure_worker_runtime_environment()
    first = os.environ["PULLBOX_DATA_DIR"]
    # A new independent invocation starts without generated test defaults.
    for key in list(os.environ):
        if key.startswith(("PULLBOX_", "_PULLBOX_PYTEST_")):
            clean_runtime_env.delenv(key)
    _configure_worker_runtime_environment()
    assert os.environ["PULLBOX_DATA_DIR"] != first


def test_explicit_runtime_overrides_are_preserved(clean_runtime_env, tmp_path):
    custom = str(tmp_path / "explicit-library")
    clean_runtime_env.setenv("PULLBOX_LIBRARY_ROOT", custom)
    clean_runtime_env.setenv("PULLBOX_DB_URL", "sqlite+aiosqlite:///:memory:")
    _configure_worker_runtime_environment()
    clean_runtime_env.setenv("PYTEST_XDIST_WORKER", "gw0")
    _configure_worker_runtime_environment()
    assert os.environ["PULLBOX_LIBRARY_ROOT"] == custom
    assert os.environ["PULLBOX_DB_URL"] == "sqlite+aiosqlite:///:memory:"
    assert Path(os.environ["PULLBOX_DATA_DIR"]).is_dir()


def test_runtime_defaults_are_stable_within_one_worker(clean_runtime_env):
    clean_runtime_env.setenv("PYTEST_XDIST_WORKER", "gw0")
    _configure_worker_runtime_environment()
    first = _runtime_values()
    _configure_worker_runtime_environment()
    assert _runtime_values() == first


def test_override_after_bootstrap_is_not_replaced(clean_runtime_env, tmp_path):
    _configure_worker_runtime_environment()
    custom = str(tmp_path / "override")
    clean_runtime_env.setenv("PULLBOX_DATA_DIR", custom)
    clean_runtime_env.setenv("PYTEST_XDIST_WORKER", "gw0")
    _configure_worker_runtime_environment()
    assert os.environ["PULLBOX_DATA_DIR"] == custom


def test_collected_test_names_are_bounded(request):
    # Inspect only lengths and truncated prefixes so a failure cannot itself dump
    # a multi-megabyte parameter into the runner log or JUnit artifact.
    oversized = [
        (item.nodeid[:120], len(item.nodeid))
        for item in request.session.items
        if len(item.nodeid) > 1024
    ]
    assert not oversized, f"Use short explicit parameter ids: {oversized[:5]}"


@pytest.mark.parametrize("payload", [b"x" * 10000, "x" * 10000, "\ud800" * 10000])
def test_large_parameter_ids_are_short_stable_and_distinct(payload):
    name = pytest_make_parametrize_id(payload, "body")
    assert name is not None and len(name) < 80
    assert name == pytest_make_parametrize_id(payload, "body")
    other = payload[:-1] + (b"y" if isinstance(payload, bytes) else "y")
    assert name != pytest_make_parametrize_id(other, "body")


@pytest.mark.parametrize("payload", ["normal", b"short", 123, {"a": "b"}])
def test_small_parameter_ids_keep_pytest_defaults(payload):
    assert pytest_make_parametrize_id(payload, "body") is None
