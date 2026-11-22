"""Operator-facing checks for a stopped Docker source, without changing its lifecycle."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check_sqlite_source_offline.py"


@pytest.mark.parametrize("layout", ["layer", "file", "shadow", "external", "postgres"])
def test_unqualified_source_is_never_offline_evidence(layout):
    module = runpy.run_path(str(SCRIPT))
    config = {"PULLBOX_DB_URL": "sqlite:////data/db", "PULLBOX_LIBRARY_ROOT": "/data/library"}
    mounts = [{"Destination": "/data", "Type": "bind", "RW": True}]
    if layout == "layer":
        mounts = []
    elif layout == "file":
        mounts.append({"Destination": "/data/db-wal", "Type": "bind", "RW": True})
    elif layout == "shadow":
        mounts.append({"Destination": "/data/library", "Type": "tmpfs", "RW": True})
    elif layout == "external":
        config["PULLBOX_LIBRARY_ROOT"] = "/different"
    else:
        config["PULLBOX_DB_URL"] = "postgresql://not-a-sqlite-source"
    with pytest.raises(module["OfflineCheckError"], match="unsupported_layout"):
        module["check_source_paths"](config, mounts)


def test_restart_during_inventory_invalidates_report_even_with_same_container_id(monkeypatch):
    module = runpy.run_path(str(SCRIPT))
    inspect = module["inspect_source"]
    cid = "a" * 64
    observations = []

    def replay(*args, endpoint=None):
        if args[0] == "info":
            return "engine|linux|Linux"
        if args[:2] == ("container", "ls"):
            return cid
        template = args[3]
        if template == "{{.Id}}":
            return cid
        if template == module["SOURCE_CONFIG"]:
            return '"PULLBOX_DB_URL=sqlite:////data/db"\n"PULLBOX_LIBRARY_ROOT=/data/library"'
        assert template == module["FIELDS"]
        observations.append(True)
        mounts = [{"Destination": "/data", "Source": "/private-source", "Type": "bind", "RW": True}]
        return json.dumps(
            [
                cid,
                "exited",
                "first" if len(observations) == 1 else "second",
                "stopped",
                "no",
                mounts,
                False,
                False,
            ]
        )

    monkeypatch.setenv("DOCKER_HOST", "unix:///test-socket")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setitem(inspect.__globals__, "docker", replay)
    with pytest.raises(module["OfflineCheckError"], match="inventory_changed"):
        inspect("source", None)


def check(container, *args):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--container", container, *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert not result.stderr, result.stderr
    assert "private-source" not in result.stdout
    report = json.loads(result.stdout)
    assert report["cutover_ready"] is False
    assert (result.returncode == 0) is report["offline_checks_passed"]
    return report


def docker(*args):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=30, check=True
    ).stdout.strip()


def test_docker_source_offline_checkpoint_survives_only_an_uninterrupted_stop(tmp_path):
    if os.environ.get("PULLBOX_TEST_MIGRATION_TARGET_DOCKER") != "1":
        pytest.skip("opt in to disposable local Docker containers")
    tag = uuid4().hex
    source = "pullbox-offline-source-" + tag
    peer = "pullbox-offline-peer-" + tag
    root = tmp_path / "private-source"
    root.mkdir()
    (root / "comics").mkdir()
    image = "postgres:16-bookworm"
    source_args = [
        "--network=none",
        "--pull=never",
        "--entrypoint=/bin/sh",
        "--stop-signal=SIGTERM",
        "--mount",
        f"type=bind,src={root},dst=/data",
        "-e",
        "PULLBOX_DB_URL=sqlite+aiosqlite:////data/pullbox.db",
        "-e",
        "PULLBOX_LIBRARY_ROOT=/data/comics",
        image,
        "-c",
        "trap 'exit 0' TERM; while :; do sleep 1; done",
    ]
    try:
        docker("run", "-d", "--name", source, *source_args)
        assert "source_not_stopped" in check(source)["blockers"]
        docker("stop", "-t", "2", source)
        docker("update", "--restart=always", source)
        assert "source_restart_enabled" in check(source)["blockers"]
        docker("update", "--restart=no", source)
        first = check(source)
        assert first["offline_checks_passed"], first
        previous = ["--compare-checkpoint", first["checkpoint_sha256"]]
        assert check(source, *previous)["checkpoint_matched"]

        def start_peer(mode):
            docker(
                "run",
                "-d",
                "--network=none",
                "--pull=never",
                "--name",
                peer,
                "--entrypoint=/bin/sleep",
                "-v",
                f"{root / 'comics'}:/different:{mode}",
                image,
                "600",
            )

        start_peer("ro")
        assert check(source, *previous)["checkpoint_matched"]
        docker("rm", "-f", "-v", peer)
        start_peer("rw")
        blocked = check(source)
        assert "other_writable_container" in blocked["blockers"]
        assert blocked["other_writer_ids"] == [docker("inspect", "--format", "{{.Id}}", peer)]
        docker("stop", "-t", "2", peer)
        assert "checkpoint_changed" in check(source, *previous)["blockers"]
        docker("rm", "-v", peer)

        docker("start", source)
        docker("stop", "-t", "2", source)
        after = check(source, *previous)
        assert "checkpoint_changed" in after["blockers"]
        assert not after["checkpoint_matched"]
        assert docker("inspect", "--format", "{{.State.Status}}", source) == "exited"
        docker("start", source)
        docker("kill", source)
        assert "source_unclean_exit" in check(source)["blockers"]
    finally:
        for name in (peer, source):
            if docker("ps", "-aq", "--filter", "name=^/" + name + "$"):
                docker("rm", "-f", "-v", name)
