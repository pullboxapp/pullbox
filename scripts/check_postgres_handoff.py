"""Revalidate a stopped SQLite source and its reviewed PostgreSQL stage without starting either."""

import argparse
import asyncio
import getpass
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import warnings
from functools import partial
from pathlib import Path

from check_sqlite_source_offline import (
    MESSAGES,
    OfflineCheckError,
    docker,
    host_path,
    inspect_source,
)
from sqlite_source_snapshot import SourceCapture, compare_snapshot

from pullbox.config import get_settings
from pullbox.services.migration_approval import write_approval
from pullbox.services.migration_assets import check_assets
from pullbox.services.migration_first_start import BindingError, check_binding, read_token
from pullbox.services.migration_restore_config import check_restore, restored_key


async def verify(name, capture, scratch, raw, token):
    """Keep both sides fenced. A prior successful invocation is not a write authorization."""

    async def target():
        await check_binding(raw, token, capture.snapshot, source=capture.fingerprint)

    def offline():
        reasons, _, _, context = inspect_source(name, capture.checkpoint)
        return {k: MESSAGES[k] for k in reasons}, context

    try:
        # Avoid a potentially large second capture when the stage is already ineligible.
        await target()
        blockers, context = offline()
        if blockers:
            return blockers
        _, blockers = compare_snapshot(
            context,
            scratch,
            capture,
            partial(docker, endpoint=context.endpoint),
            lambda: offline()[0],
            partial(host_path, desktop=context.desktop),
        )
        if blockers:
            return blockers
        await target()
        return {}
    except BindingError as exc:
        return {"target_binding": str(exc)}
    except OfflineCheckError as exc:
        reason = exc.args[0]
        return {reason: MESSAGES[reason]}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return {"inspection_failed": MESSAGES["inspection_failed"]}


def _source_path(context, path):
    """Translate the recorded container path, not an operator-selected substitute."""
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise BindingError("Invalid original media path.")
    mounts = [(Path(m["Destination"]), m) for m in context.mounts]
    parents = [(p, m) for p, m in mounts if path.is_relative_to(p)]
    if not parents or any(p != path and p.is_relative_to(path) for p, _ in mounts):
        raise BindingError("Approval requires a host-readable source mount without nested mounts.")
    dest, mount = max(parents, key=lambda entry: len(entry[0].parts))
    if mount["Type"] != "bind":
        raise BindingError(
            "Approval currently requires host-readable bind mounts for source media."
        )
    base = Path(mount["Source"])
    if context.desktop:
        for prefix in ("/host_mnt", "/run/desktop/mnt/host"):
            if base.is_relative_to(prefix):
                base = Path("/") / base.relative_to(prefix)
                break
    base = base.resolve(strict=True)
    resolved = base / path.relative_to(dest)
    if resolved.resolve() != resolved:
        raise BindingError("The original media directory is not a direct path on its bind mount.")
    return resolved


async def approve(
    name,
    capture,
    scratch,
    raw,
    token,
    *,
    original_backup,
    restored_backup,
    original_settings,
    restored_settings,
    deployment_file,
    key_source,
    output,
    confirm,
    all_files=False,
):
    if (
        output.parent.resolve() != scratch.resolve()
        or output.suffix != ".json"
        or any(
            scratch.resolve().is_relative_to(d.resolve())
            for d in (original_settings, restored_settings)
        )
        or output.exists()
        or output.is_symlink()
    ):
        return {
            "approval_destination": "Use a new .json file in the handoff scratch directory, "
            "outside both settings copies."
        }
    reasons, _, _, context = inspect_source(name, capture.checkpoint)
    if reasons:
        return {reason: MESSAGES[reason] for reason in reasons}
    binding = await check_binding(raw, token, capture.snapshot, source=capture.fingerprint)
    if not confirm(capture.snapshot):
        return {"cancelled": "No approval issued. The stage remains closed."}
    template = (
        '{{range .Config.Env}}{{$k := index (split . "=") 0}}'
        '{{if or (eq $k "PULLBOX_DATA_DIR") (eq $k "PULLBOX_COVERS_DIR") '
        '(eq $k "PULLBOX_SECRET_KEY")}}'
        "{{println (json .)}}{{end}}{{end}}"
    )
    env = dict(
        json.loads(line).split("=", 1)
        for line in docker(
            "container", "inspect", "--format", template, context.cid, endpoint=context.endpoint
        ).splitlines()
    )
    covers = _source_path(context, Path(env.get("PULLBOX_COVERS_DIR", "/comics/.covers")))
    legacy = _source_path(context, Path(env.get("PULLBOX_DATA_DIR", "/data")) / "covers")
    if not legacy.exists() or legacy.samefile(covers):
        legacy = None
    protected = [host_path(m["Source"], context.desktop) for m in context.mounts if m.get("Source")]
    if original_backup.samefile(restored_backup) or any(
        path.lstat().st_nlink != 1
        or any(
            host_path(str(path.resolve()), context.desktop).is_relative_to(base)
            for base in protected
        )
        for path in (original_backup, restored_backup)
    ):
        return {
            "independent_backup": "Use two independent backup files outside the source mounts; "
            "do not use the source database or hard links."
        }
    configured = get_settings().db_url
    found = []
    restore = check_restore(
        restored_backup,
        capture.snapshot,
        original_settings,
        restored_settings,
        key_source,
        configured,
        deployment_file=deployment_file,
        on_verified_key=found.append,
    )
    if not restore.restore_verified:
        return {"restore": restore.next_step}
    (secret,) = found
    effective = env.pop("PULLBOX_SECRET_KEY", "").strip()
    if bool(effective) != (key_source == "secret-file"):
        return {"original_key": "Select the effective key source from the stopped deployment."}
    if not effective:
        path = _source_path(context, Path(env.get("PULLBOX_DATA_DIR", "/data")) / "config.xml")
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return {"original_key": "The stopped source configuration is not a regular file."}
            content = f.read(4 * 1024**2 + 1)
        if len(content) > 4 * 1024**2:
            return {"original_key": "The stopped source configuration is too large."}
        effective = restored_key(content, key_source)
    if not hmac.compare_digest(effective.encode(), secret.encode()):
        return {"original_key": "The restored key differs from the stopped source's effective key."}
    assets = check_assets(
        original_backup,
        capture.snapshot,
        None,
        compare=None,
        all_files=all_files,
        configured_url=configured,
        config_path=original_settings / "config.xml",
        covers_dir=covers,
        legacy_covers_dir=legacy,
        root_on_host=partial(_source_path, context),
        verified_key=secret,
    )
    if not assets.checks_passed or not assets.cover_cache_mapped or not assets.sample_sha256:
        return {
            "original_media": assets.next_step + " Blockers: " + ", ".join(sorted(assets.blockers))
        }
    blockers = await verify(name, capture, scratch, raw, token)
    if blockers:
        return blockers
    if binding != await check_binding(raw, token, capture.snapshot, source=capture.fingerprint):
        return {"target_binding": "The reviewed target changed before approval."}
    write_approval(
        output, binding, assets.sample_sha256, read_token(token), secret, all_files=all_files
    )
    return {}


def _confirm(digest):
    phrase = "approve " + digest[:12]
    print(
        "Pullbox will recheck the backup, settings, original media and stopped source. "
        "This does not start either installation. "
        f"Type '{phrase}' to issue approval only if all checks pass, or Enter to cancel:",
        file=sys.stderr,
        flush=True,
    )
    return input().strip() == phrase


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True, help="The original stopped SQLite container")
    parser.add_argument("--capture-report", type=Path, required=True)
    parser.add_argument("--scratch-dir", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument(
        "--write-approval", type=Path, help="Issue an approval after interactive confirmation"
    )
    for flag in (
        "original-backup",
        "restored-backup",
        "original-settings",
        "restored-settings",
    ):
        parser.add_argument("--" + flag, type=Path)
    parser.add_argument("--key-source", choices=("secret-file", "config-xml"))
    parser.add_argument("--deployment-file", default="compose.yaml")
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="Hash every registered comic in both approval checks (default: sample)",
    )
    parser.add_argument(
        "--url-stdin", action="store_true", help="Read the target URL from protected stdin"
    )
    args = parser.parse_args()
    if args.all_files and not args.write_approval:
        parser.error("--all-files requires --write-approval")
    if args.write_approval and (
        args.url_stdin
        or not sys.stdin.isatty()
        or not sys.stderr.isatty()
        or not all(
            (
                args.original_backup,
                args.restored_backup,
                args.original_settings,
                args.restored_settings,
                args.key_source,
            )
        )
    ):
        parser.error(
            "Approval requires a private terminal, both backups and settings, and --key-source."
        )
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", args.container):
        parser.error("Invalid Docker container name or ID")
    if args.url_stdin == sys.stdin.isatty():
        parser.error("Use the hidden terminal prompt or --url-stdin with a protected input stream.")
    try:
        capture = SourceCapture.read(args.capture_report)
    except ValueError as exc:
        blockers = {"source_capture": str(exc)}
    else:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                raw = (
                    sys.stdin.readline(4098).rstrip("\r\n")
                    if args.url_stdin
                    else getpass.getpass("Reviewed PostgreSQL target URL: ")
                )
            common = (args.container, capture, args.scratch_dir, raw, args.token_file)
            if args.write_approval:
                blockers = asyncio.run(
                    approve(
                        *common,
                        original_backup=args.original_backup,
                        restored_backup=args.restored_backup,
                        original_settings=args.original_settings,
                        restored_settings=args.restored_settings,
                        deployment_file=args.deployment_file,
                        key_source=args.key_source,
                        output=args.write_approval,
                        confirm=_confirm,
                        all_files=args.all_files,
                    )
                )
            else:
                blockers = asyncio.run(verify(*common))
        except BindingError as exc:
            blockers = {"approval_failed" if args.write_approval else "target_binding": str(exc)}
        except (KeyboardInterrupt, EOFError):
            blockers = {"cancelled": "Check cancelled. Leave both installations offline."}
        except Exception:
            # Driver and Docker errors may include credentials or private paths.
            blockers = {
                "handoff_unavailable": "Could not complete the handoff check. Do not switch."
            }
    print(
        json.dumps(
            {
                "source_and_target_verified": not blockers,
                "cutover_ready": False,
                "approval_issued": bool(args.write_approval) and not blockers,
                "blockers": blockers,
            },
            sort_keys=True,
        )
    )
    return int(bool(blockers))


if __name__ == "__main__":
    raise SystemExit(main())
