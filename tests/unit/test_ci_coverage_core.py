"""Coverage defaults and overrides must identify the real engine without relaxing CI."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
pytest_plugins = ["pytester"]


@pytest.mark.parametrize("core", ["auto", "ctrace", "sysmon"])
def test_workflow_launcher_loads_checkout_plugin_before_collection(
    tmp_path: Path, core: str
) -> None:
    """Run the real shell step without pytester's sys.path bootstrap."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(
        s for s in workflow["jobs"]["test"]["steps"] if s.get("name") == "Run tests with coverage"
    )
    package = tmp_path / "pullbox"
    package.mkdir()
    (package / "__init__.py").write_text("def double(value):\n    return value * 2\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    shutil.copyfile(ROOT / "tests/ci_coverage_core.py", tests / "ci_coverage_core.py")
    (tests / "test_probe.py").write_text(
        "import pytest\nfrom pullbox import double\n"
        "@pytest.mark.parametrize('value', range(6))\ndef test_double(value):\n"
        "    assert double(value) == value + value\n"
    )
    env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTEST_ADDOPTS", "COVERAGE_RCFILE", "COVERAGE_FILE"):
        env.pop(name, None)
    directory = tmp_path / "core-evidence"
    env.update(
        PATH=str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", ""),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTEST_PLUGINS="pytest_cov.plugin,xdist.plugin",
        PYTEST_WORKERS="2",
        COVERAGE_FAIL_UNDER="100",
        PULLBOX_CI_COVERAGE_EXPECTED_CORE=core,
        PULLBOX_CI_COVERAGE_REPORT_DIR=str(directory),
    )
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "6 passed" in result.stdout
    assert (tmp_path / "coverage.xml").is_file()
    assert (tmp_path / "test-results.xml").is_file()
    reports = [json.loads(path.read_text()) for path in directory.glob("*.json")]
    assert len(reports) == 3
    assert {item["worker"] for item in reports} == {"controller", "gw0", "gw1"}
    expected_core = (
        "SysMonitor"
        if core == "sysmon" or (core == "auto" and sys.version_info >= (3, 14))
        else "CTracer"
    )
    assert all(item["core"] == expected_core and item["expected_core"] == core for item in reports)


def _probe(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    *,
    actual: str | None = "sysmon",
    expected: str = "sysmon",
    workers: int = 0,
    enabled: bool = True,
    coverage: bool = True,
) -> tuple[pytest.RunResult, list[dict[str, object]]]:
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    if actual is None:
        monkeypatch.delenv("COVERAGE_CORE", raising=False)
    else:
        monkeypatch.setenv("COVERAGE_CORE", actual)
    monkeypatch.setenv("PULLBOX_CI_COVERAGE_EXPECTED_CORE", expected)
    directory = pytester.path / "core-evidence"
    if enabled:
        monkeypatch.setenv("PULLBOX_CI_COVERAGE_REPORT_DIR", str(directory))
    else:
        monkeypatch.delenv("PULLBOX_CI_COVERAGE_REPORT_DIR", raising=False)
    pytester.makeconftest(
        f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n"
        "pytest_plugins = ['tests.ci_coverage_core']\n"
    )
    pytester.makepyfile(measured="def double(value):\n    return value * 2\n")
    pytester.makepyfile(
        test_probe="import pytest\nfrom measured import double\n"
        "@pytest.mark.parametrize('value', range(6))\ndef test_double(value):\n"
        "    assert double(value) == value + value\n"
    )
    args = ["-p", "pytest_cov.plugin", "-p", "xdist.plugin", "-n", str(workers), "-q"]
    if coverage:
        args += ["--cov=measured", "--cov-report=xml", "--cov-fail-under=100"]
    result = pytester.runpytest_subprocess(*args, timeout=45)
    reports = [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]
    return result, reports


@pytest.mark.parametrize("workers", [0, 2])
@pytest.mark.parametrize(
    "core, expected_name",
    [
        ("ctrace", "CTracer"),
        ("sysmon", "SysMonitor"),
        ("auto", "SysMonitor" if sys.version_info >= (3, 14) else "CTracer"),
    ],
)
def test_records_each_real_worker_engine(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    workers: int,
    core: str,
    expected_name: str,
) -> None:
    result, reports = _probe(
        pytester,
        monkeypatch,
        actual=None if core == "auto" else core,
        expected=core,
        workers=workers,
    )
    result.assert_outcomes(passed=6)
    assert {item["worker"] for item in reports} == (
        {"controller", "gw0", "gw1"} if workers else {"controller"}
    )
    assert all(item["core"] == expected_name for item in reports)
    assert all(item["expected_core"] == core for item in reports)
    assert all(item["coverage_version"] and item["python_version"] for item in reports)
    assert all(item["branch"] is False for item in reports)


def test_rejects_an_unexpected_fallback_engine(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = _probe(pytester, monkeypatch, actual="ctrace", expected="sysmon")
    assert result.ret != 0, "A fallback must not be reported as a successful sysmon trial"
    assert "Expected SysMonitor, got CTracer" in result.stderr.str()


def test_rejects_missing_coverage_in_a_measurement(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _ = _probe(pytester, monkeypatch, coverage=False)
    assert result.ret != 0, "A measurement must fail when coverage is absent"
    assert "Coverage is not active" in result.stderr.str()


def test_probe_is_inert_outside_explicit_measurements(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, reports = _probe(pytester, monkeypatch, enabled=False, coverage=False)
    result.assert_outcomes(passed=6)
    assert reports == []


@pytest.mark.parametrize("job_name", ["test", "test-production"])
def test_workflow_defaults_compatibility_to_sysmon_and_records_workers(job_name: str) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    events = workflow.get("on", workflow.get(True))
    selection = events["workflow_dispatch"]["inputs"].get("compatibility_coverage_core", {})
    assert selection.get("default") == "sysmon"
    assert selection.get("type") == "choice"
    assert selection.get("options") == ["auto", "ctrace", "sysmon"]
    steps = workflow["jobs"][job_name]["steps"]
    run = next(s for s in steps if s.get("name") == "Run tests with coverage")
    assert run["env"]["PULLBOX_CI_COVERAGE_EXPECTED_CORE"] == (
        "${{ matrix.python-version != '3.14' && (github.event_name == 'workflow_dispatch' "
        "&& inputs.compatibility_coverage_core || 'sysmon') || 'auto' }}"
    )
    assert 'case "$PULLBOX_CI_COVERAGE_EXPECTED_CORE" in' in run["run"]
    assert "auto) unset COVERAGE_CORE" in run["run"]
    assert 'ctrace|sysmon) export COVERAGE_CORE="$PULLBOX_CI_COVERAGE_EXPECTED_CORE"' in run["run"]
    assert "-p tests.ci_coverage_core" in run["run"]
    assert (
        run["env"]["PULLBOX_CI_COVERAGE_REPORT_DIR"]
        == "${{ runner.temp }}/coverage-core-${{ github.run_id }}-"
        "${{ github.run_attempt }}-py${{ matrix.python-version }}"
    )
    upload = next(s for s in steps if s.get("name") == "Upload coverage core evidence")
    assert upload["if"] == "always()"
    assert upload["with"]["if-no-files-found"] == "error"
    assert not run.get("continue-on-error", False)


@pytest.mark.parametrize(
    "mode, expected, code",
    [("auto", "unset", 0), ("ctrace", "ctrace", 0), ("sysmon", "sysmon", 0), ("invalid", "", 2)],
)
def test_core_selection_shell_preserves_default_and_rejects_unknown_values(
    mode: str, expected: str, code: int
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(
        s for s in workflow["jobs"]["test"]["steps"] if s.get("name") == "Run tests with coverage"
    )
    selection = step["run"].split("# VM-wide", 1)[0]
    result = subprocess.run(
        ["bash", "-e", "-c", selection + '\nprintf "%s" "${COVERAGE_CORE-unset}"'],
        env=os.environ | {"COVERAGE_CORE": "inherited", "PULLBOX_CI_COVERAGE_EXPECTED_CORE": mode},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == code
    assert result.stdout == expected
