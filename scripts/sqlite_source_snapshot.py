"""Publish a checked SQLite backup without starting the source container."""

import errno
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pullbox.services.database_migration_preflight import inspect_migration_backup  # noqa: E402

DESTINATION = {
    "snapshot_destination": "Choose a new output file in a private local directory outside "
    "the source mounts, with no existing SQLite file or sidecars."
}
CAPTURE = {
    "snapshot_capture": "The read-only helper could not capture a stable SQLite database. "
    "Check local space, mount layout, source permissions and shutdown before retrying. "
    "Do not remove SQLite journals."
}
DRIFT = {"source_files_changed": "SQLite files changed during capture. Do not use this snapshot."}
SPACE = {"snapshot_space": "Not enough local scratch space. Use a larger output filesystem."}


def unsafe_output(path):
    journals = ("-wal", "-shm", "-journal")
    return path.name.casefold().endswith(journals) or any(
        os.path.lexists(str(path) + suffix) for suffix in ("", *journals)
    )


def run_worker(context, run, work, mode):
    m = context.data_mount
    base = Path(m["Destination"])
    relative = context.database.relative_to(base)
    specs = (
        json.loads(
            run("container", "inspect", "--format", "{{json .HostConfig.Mounts}}", context.cid)
        )
        or []
    )
    if any(
        s.get("Target") == str(base)
        and any(v for k, v in (s.get("VolumeOptions") or {}).items() if k.lower() == "subpath")
        for s in specs
    ) or m.get("SubPath"):
        raise ValueError
    if m["Type"] == "bind":
        value = m["Source"]
        option = "bind-propagation=rprivate,bind-recursive=readonly"
    else:
        value = m["Name"]
        option = "volume-nocopy"
    if any(c in s for s in (value, str(work)) for c in ',"\n\r'):
        raise ValueError
    image = run("container", "inspect", "--format", "{{.Image}}", context.cid)
    if not re.fullmatch("sha256:[a-f0-9]{64}", image):
        raise ValueError
    user = run("container", "inspect", "--format", "{{.Config.User}}", context.cid)
    groups = (
        json.loads(
            run("container", "inspect", "--format", "{{json .HostConfig.GroupAdd}}", context.cid)
        )
        or []
    )
    script = Path(__file__).with_name("sqlite_source_snapshot_worker.py").read_text()
    cid = run(
        "create",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--log-driver=none",
        "--label=pullbox.offline-source=snapshot",
        "--workdir=/",
        "--mount",
        f"type={m['Type']},src={value},dst=/data,readonly,{option}",
        "--mount",
        f"type=bind,src={work},dst=/pullbox-snapshot-output"
        + (",readonly" if mode == "check" else ""),
        *(["--user", user] if user else []),
        *(flag for group in groups for flag in ("--group-add", group)),
        "--entrypoint=python",
        image,
        "-ISB",
        "-c",
        script,
        str(Path("/data") / relative),
        mode,
    )
    if not re.fullmatch("[a-f0-9]{64}", cid):
        raise ValueError
    try:
        mounts = json.loads(run("container", "inspect", "--format", "{{json .Mounts}}", cid))
        actual = next((entry for entry in mounts if entry["Destination"] == "/data"), {})
        # Prove the daemon mounted the original storage, not a fresh image volume.
        if actual.get("RW") is not False or actual.get("Source") != m["Source"]:
            raise ValueError
        evidence = run("start", "--attach", cid, timeout=3600)
        state = run(
            "container", "inspect", "--format", "{{.State.Status}}:{{.State.ExitCode}}", cid
        )
        if state == "exited:0" and evidence == "snapshot_space":
            raise OSError(errno.ENOSPC, "snapshot_space")
        if state != "exited:0" or not re.fullmatch("[a-f0-9]{64}", evidence):
            raise ValueError
        return evidence
    finally:
        run("rm", "--force", "--volumes", cid)


def capture_snapshot(context, output, docker, recheck, host_path):
    failed = {"snapshot_verified": False, "snapshot_sha256": None}
    try:
        parent = output.parent.resolve(strict=True)
        output = parent / output.name
        info = parent.stat()
        if (
            not parent.is_dir()
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
            or unsafe_output(output)
            or any(
                m.get("Source") and host_path(str(parent)).is_relative_to(host_path(m["Source"]))
                for m in context.mounts
            )
        ):
            return failed, DESTINATION
        with tempfile.TemporaryDirectory(dir=parent, prefix=".pullbox-snapshot-") as tmp:
            # Only this empty child is mounted. Its host parent stays private (0700);
            # the container's UID may differ from the operator's UID.
            work = Path(tmp) / "work"
            work.mkdir()
            work.chmod(0o777)
            staged = work / "snapshot"
            staged.touch(mode=0o600)
            staged.chmod(0o666)
            with staged.open("rb") as file:
                before = run_worker(context, docker, work, "capture")
                if not os.path.samestat(os.fstat(file.fileno()), staged.lstat()):
                    return failed, CAPTURE
                staged.chmod(0o600)
                os.fsync(file.fileno())
                checks = inspect_migration_backup(staged, configured_url="sqlite:///:memory:")
                if not checks.source_checks_passed:
                    return failed, checks.blockers
                digest = hashlib.file_digest(file, "sha256").hexdigest()
                if run_worker(context, docker, work, "check") != before:
                    return failed, DRIFT
                blockers = recheck()
                if blockers:
                    return failed, blockers
                if unsafe_output(output):
                    return failed, DESTINATION
                fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.link(staged, output, follow_symlinks=False)
                    try:
                        os.fsync(fd)
                    except OSError:
                        if os.path.samestat(os.fstat(file.fileno()), output.lstat()):
                            output.unlink()
                        raise
                except FileExistsError:
                    return failed, DESTINATION
                finally:
                    os.close(fd)
            return {
                "snapshot_verified": True,
                "snapshot_sha256": digest,
                "snapshot_bytes": checks.database_bytes,
                "source_revision": checks.source_revision,
                "source_file_state_sha256": before,
            }, {}
    except OSError as exc:
        return failed, SPACE if exc.errno == errno.ENOSPC else CAPTURE
    except (ValueError, subprocess.SubprocessError):
        return failed, CAPTURE
