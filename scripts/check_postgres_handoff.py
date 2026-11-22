"""Revalidate a stopped SQLite source and its reviewed PostgreSQL stage without starting either."""

import argparse
import asyncio
import getpass
import json
import re
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

from pullbox.services.migration_first_start import BindingError, check_binding


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True, help="The original stopped SQLite container")
    parser.add_argument("--capture-report", type=Path, required=True)
    parser.add_argument("--scratch-dir", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument(
        "--url-stdin", action="store_true", help="Read the target URL from protected stdin"
    )
    args = parser.parse_args()
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
            blockers = asyncio.run(
                verify(args.container, capture, args.scratch_dir, raw, args.token_file)
            )
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
                "blockers": blockers,
            },
            sort_keys=True,
        )
    )
    return int(bool(blockers))


if __name__ == "__main__":
    raise SystemExit(main())
