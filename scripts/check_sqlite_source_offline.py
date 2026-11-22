"""Read-only host preflight for an offline, standalone Docker SQLite source."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from functools import partial
from pathlib import Path as HostPath
from pathlib import PurePosixPath as Path

MESSAGES = {
    "local_docker_required": "Use the one trusted, local Docker Engine that owns the source.",
    "inspection_failed": "Could not inspect the selected container and local Docker inventory.",
    "inventory_changed": "Docker containers changed during inspection. Retry when stable.",
    "unsupported_layout": "Use a standalone SQLite container with persistent local data and "
    "library mounts. Other layouts need separate offline verification.",
    "source_not_stopped": "Stop the source cleanly. A running or paused app is not offline.",
    "source_unclean_exit": "The source did not exit cleanly. Resolve the failure in SQLite, "
    "settle any unfinished work and repeat the controlled shutdown.",
    "source_restart_enabled": "Disable automatic restart on the source and its deployment manager.",
    "other_writable_container": "Another running or restartable container shares writable "
    "storage with the source. Stop its writes and automatic restart before retrying.",
    "checkpoint_changed": "The offline evidence changed. Do not reuse the previous snapshot.",
    "candidate_not_waiting": "The named first-start container is not the sole waiting Pullbox "
    "process, or it can write source storage other than the original comics mount.",
}


class OfflineCheckError(Exception):
    pass


@dataclass
class SourceContext:
    endpoint: str
    cid: str
    database: Path
    data_mount: dict
    desktop: bool
    mounts: list


def docker(*args, endpoint=None, timeout=30):
    host = ["--host", endpoint] if endpoint else []
    env = os.environ.copy()
    if endpoint:
        env.pop("DOCKER_CONTEXT", None)
    return subprocess.run(
        ["docker", *host, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
        env=env,
    ).stdout.strip()


# Project only the fields we need. Never print or retain full inspect records
# (environment values and health-check logs can contain credentials).
FIELDS = (
    "[{{json .Id}},{{json .State.Status}},{{json .State.StartedAt}},{{json .State.FinishedAt}},"
    "{{json .HostConfig.RestartPolicy.Name}},{{json .Mounts}},"
    '{{if or (index .Config.Labels "com.docker.swarm.service.id") '
    '(index .Config.Labels "io.kubernetes.pod.uid")}}true{{else}}false{{end}},'
    '{{if or .State.OOMKilled (ne (json .State.ExitCode) "0")}}true{{else}}false{{end}}]'
)
SOURCE_CONFIG = (
    '{{range .Config.Env}}{{$k := index (split . "=") 0}}'
    '{{if or (eq $k "PULLBOX_DB_URL") (eq $k "PULLBOX_LIBRARY_ROOT")}}'
    "{{println (json .)}}{{end}}{{end}}"
)


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or value.startswith("//"):
        raise OfflineCheckError("unsupported_layout")
    return path


def host_path(value, desktop):
    path = absolute(value)
    if desktop:
        for prefix in ("/host_mnt", "/run/desktop/mnt/host"):
            if path.is_relative_to(prefix):
                path = Path("/") / path.relative_to(prefix)
                break
        # On macOS the aliases can name the same writable files. An extra
        # conservative match is preferable to overlooking another writer.
        if path.parts[1:2] in (("tmp",), ("var",), ("etc",)):
            path = Path("/private") / path.relative_to("/")
        path = Path(str(path).casefold())
    return path


def storage(mount, desktop):
    if mount["Type"] == "tmpfs":
        return None
    if mount["Type"] not in {"volume", "bind"} or (
        mount["Type"] == "volume" and mount["Driver"] != "local"
    ):
        raise OfflineCheckError("unsupported_layout")
    return host_path(mount["Source"], desktop)


def check_source_paths(config, mounts):
    match = re.fullmatch(r"sqlite(?:\+aiosqlite)?:///(/[^?#]+)", config.get("PULLBOX_DB_URL", ""))
    if not match:
        raise OfflineCheckError("unsupported_layout")
    db = absolute(match[1])
    library = absolute(config.get("PULLBOX_LIBRARY_ROOT", "/comics"))
    for directory in (db.parent, library):
        candidates = [m for m in mounts if directory.is_relative_to(absolute(m["Destination"]))]
        if directory == Path("/") or not candidates:
            raise OfflineCheckError("unsupported_layout")
        owning = max(candidates, key=lambda m: len(absolute(m["Destination"]).parts))
        if owning["Type"] not in {"volume", "bind"} or not owning["RW"]:
            raise OfflineCheckError("unsupported_layout")
    # A file-only DB mount may leave WAL or rollback journals on the container layer.
    db_files = {Path(str(db) + suffix) for suffix in ("", "-wal", "-shm", "-journal")}
    if any(absolute(m["Destination"]) in db_files for m in mounts):
        raise OfflineCheckError("unsupported_layout")
    return db, max(
        (m for m in mounts if db.parent.is_relative_to(absolute(m["Destination"]))),
        key=lambda m: len(absolute(m["Destination"]).parts),
    )


def inspect_rows(run, ids):
    rows = {}
    for i in range(0, len(ids), 64):
        data = run("container", "inspect", "--format", FIELDS, *ids[i : i + 64])
        for line in data.splitlines():
            cid, *row = json.loads(line)
            rows[cid] = row
    if sorted(rows) != ids:
        raise OfflineCheckError("inventory_changed")
    return rows


def _waiting_container(
    run, candidate, challenge, rows, source, config, data_mount, database, desktop
):
    """Only during live admission: prove the candidate is the single waiting CLI, not an app."""
    failure = OfflineCheckError("candidate_not_waiting")
    cid = run("container", "inspect", "--format", "{{.Id}}", candidate)
    if cid == source or cid not in rows or not re.fullmatch("[0-9a-f]{32}", challenge):
        raise failure
    status, _, _, restart, mounts, managed, _ = rows[cid]
    if status != "running" or restart not in ("no", "") or managed:
        raise failure
    command, args, pid, privileged = json.loads(
        run(
            "container",
            "inspect",
            "--format",
            "[{{json .Path}},{{json .Args}},{{json .State.Pid}},{{json .HostConfig.Privileged}}]",
            cid,
        )
    )
    if (
        command not in ("python", "/usr/local/bin/python")
        or privileged
        or args[:3]
        not in (["-m", "pullbox.cli", "migration-start"], ["-Bm", "pullbox.cli", "migration-start"])
        or len(args) != 11
        or set(args[3::2])
        != {"--token-file", "--snapshot-sha256", "--approval-file", "--restored-backup"}
        or run("top", cid, "-eo", "pid").split() != ["PID", str(pid)]
        or f"First-start challenge: {challenge}" not in run("logs", "--tail", "5", cid).splitlines()
    ):
        raise failure
    orig_mounts = rows[source][4]
    library = absolute(config.get("PULLBOX_LIBRARY_ROOT", "/comics"))
    media = [
        m for m in orig_mounts if absolute(m["Destination"]) == library and m["Type"] == "bind"
    ]
    if len(media) != 1:
        raise failure
    allowed = storage(media[0], desktop)
    protected = {storage(m, desktop) for m in orig_mounts if m["RW"]} - {None}
    db_path = storage(data_mount, desktop) / database.relative_to(
        absolute(data_mount["Destination"])
    )
    for m in mounts:
        if not m["RW"] or not (path := storage(m, desktop)):
            continue
        if any(path.is_relative_to(p) or p.is_relative_to(path) for p in protected) and (
            path != allowed or absolute(m["Destination"]) != library or db_path.is_relative_to(path)
        ):
            raise failure
    return cid


def inspect_source(name, compare, *, candidate=None, challenge=None):
    endpoint = os.environ.get("DOCKER_HOST") or docker(
        "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"
    )
    if not endpoint.startswith("unix://") or (
        os.environ.get("DOCKER_HOST") and os.environ.get("DOCKER_CONTEXT")
    ):
        raise OfflineCheckError("local_docker_required")
    run = partial(docker, endpoint=endpoint)
    engine = run("info", "--format", "{{.ID}}|{{.OSType}}|{{.OperatingSystem}}")
    ident, os_type, system = engine.split("|", 2)
    if not ident or os_type != "linux":
        raise OfflineCheckError("local_docker_required")
    desktop = "docker desktop" in system.lower()

    def resolve():
        return run("container", "inspect", "--format", "{{.Id}}", name)

    def inventory():
        return sorted(run("container", "ls", "-aq", "--no-trunc").splitlines())

    source = resolve()
    ids = inventory()
    if source not in ids:
        raise OfflineCheckError("inventory_changed")
    rows = inspect_rows(run, ids)
    state, _, _, policy, mounts, orchestrated, unclean = rows[source]
    blockers = set()
    if orchestrated:
        raise OfflineCheckError("unsupported_layout")
    if state != "exited":
        blockers.add("source_not_stopped")
    elif unclean:
        blockers.add("source_unclean_exit")
    if policy not in ("no", ""):
        blockers.add("source_restart_enabled")
    config = dict(
        json.loads(line).split("=", 1)
        for line in run("container", "inspect", "--format", SOURCE_CONFIG, source).splitlines()
    )
    database, data_mount = check_source_paths(config, mounts)
    waiting = (
        _waiting_container(
            run, candidate, challenge, rows, source, config, data_mount, database, desktop
        )
        if candidate and challenge and compare
        else None
    )
    protected = {storage(m, desktop) for m in mounts if m["RW"]} - {None}
    related = {source: rows[source][:4]}
    writers = []
    volume_names = set()
    for cid, (status, started, finished, restart, container_mounts, managed, _) in rows.items():
        overlaps = False
        for m in container_mounts:
            if not m["RW"]:
                continue
            path = storage(m, desktop)
            if m["Type"] == "volume":
                volume_names.add(m["Name"])
            if path and any(path.is_relative_to(p) or p.is_relative_to(path) for p in protected):
                overlaps = True
        if cid not in {source, waiting} and overlaps:
            related[cid] = [status, started, finished, restart]
            if status not in {"exited", "created"} or restart not in ("no", "") or managed:
                blockers.add("other_writable_container")
                writers.append(cid)
    # A volume configured with bind/NFS options can alias another mount under
    # an unrelated engine path; its mountpoint alone is not negative evidence.
    names = sorted(volume_names)
    for i in range(0, len(names), 64):
        flags = run(
            "volume",
            "inspect",
            "--format",
            '{{if or (ne .Driver "local") .Options}}unsupported{{else}}local{{end}}',
            *names[i : i + 64],
        ).splitlines()
        if flags != ["local"] * len(names[i : i + 64]):
            raise OfflineCheckError("unsupported_layout")
    if inspect_rows(run, ids) != rows or inventory() != ids or resolve() != source:
        raise OfflineCheckError("inventory_changed")
    payload = [1, ident, source, config, sorted(str(p) for p in protected), related]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    if compare and compare != digest:
        blockers.add("checkpoint_changed")
    return (
        blockers,
        digest,
        sorted(writers),
        SourceContext(endpoint, source, database, data_mount, desktop, mounts),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--snapshot", metavar="NEW_FILE")
    parser.add_argument("--compare-checkpoint", metavar="SHA256")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", args.container):
        parser.error("Invalid Docker container name or ID")
    if args.compare_checkpoint and not re.fullmatch("[a-f0-9]{64}", args.compare_checkpoint):
        parser.error("Expected a SHA-256 checkpoint")
    if args.snapshot and not args.compare_checkpoint:
        parser.error("--snapshot requires --compare-checkpoint from a prior successful check")
    digest = None
    writers = []
    snapshot_info = {"snapshot_verified": False, "snapshot_sha256": None} if args.snapshot else {}
    messages = dict(MESSAGES)
    try:
        reasons, digest, writers, context = inspect_source(args.container, args.compare_checkpoint)
        if args.snapshot and not reasons:
            from sqlite_source_snapshot import capture_snapshot

            def recheck():
                nonlocal writers
                after, _, writers, _ = inspect_source(args.container, digest)
                return {reason: MESSAGES[reason] for reason in after}

            snapshot_info, blocked = capture_snapshot(
                context,
                HostPath(args.snapshot),
                partial(docker, endpoint=context.endpoint),
                recheck,
                partial(host_path, desktop=context.desktop),
            )
            reasons.update(blocked)
            messages.update(blocked)
    except OfflineCheckError as exc:
        reasons = {exc.args[0]}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        reasons = {"inspection_failed"}
    print(
        json.dumps(
            {
                "offline_checks_passed": not reasons,
                "checkpoint_sha256": None if reasons else digest,
                "checkpoint_matched": not reasons and args.compare_checkpoint is not None,
                "cutover_ready": False,
                "other_writer_ids": writers,
                "blockers": {reason: messages[reason] for reason in sorted(reasons)},
                **snapshot_info,
            },
            sort_keys=True,
        )
    )
    return int(bool(reasons))


if __name__ == "__main__":
    raise SystemExit(main())
