"""Pullbox CLI — management commands for Docker and production environments.

Provides commands that can be run via ``docker exec`` when the web UI is
inaccessible (e.g., locked out, forgot password).

Usage::

    printf '%s\n' 'NewPass1!' | docker exec -i pullbox \
        python -m pullbox.cli reset-password --user admin --password-stdin
"""

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullbox.config import get_settings
from pullbox.core.password_policy import validate_password
from pullbox.models.user import User
from pullbox.services.auth_service import AuthService
from pullbox.services.import_path_reconciliation import reconcile_saved_mylar_paths
from pullbox.services.import_review_recheck import prepare_import_recheck


async def _reset_password(username: str, candidate_secret: str) -> None:
    """Validate password, update the user's hash, and invalidate all sessions."""
    violations = validate_password(candidate_secret)
    if violations:
        # These are static policy requirement strings; candidate_secret is never printed.
        # codeql[py/clear-text-logging-sensitive-data]
        print("Password does not meet requirements:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        sys.exit(1)

    settings = get_settings()
    engine = create_async_engine(settings.db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as session:
            result = await session.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()

            if not user:
                print(f"Error: user '{username}' not found.", file=sys.stderr)
                sys.exit(1)

            user.password_hash = AuthService.hash_password(candidate_secret)
            user.session_version += 1
            await session.commit()

            print(f"Password reset for user '{username}'.")
            print(
                f"Session version bumped to {user.session_version}"
                " — all existing sessions invalidated."
            )
    finally:
        await engine.dispose()


def _read_password(*, password_stdin: bool) -> str:
    """Read a password without exposing it in process arguments."""
    if password_stdin:
        secret = sys.stdin.readline().rstrip("\r\n")
        if not secret:
            print("Error: no password was provided on stdin.", file=sys.stderr)
            sys.exit(1)
        return secret

    secret = getpass.getpass("New password: ")
    confirmation = getpass.getpass("Confirm new password: ")
    if secret != confirmation:
        print("Error: passwords do not match.", file=sys.stderr)
        sys.exit(1)
    return secret


async def _recheck_import(args: argparse.Namespace) -> None:
    """Run maintenance against a stopped app; default is a non-mutating preview."""
    engine = create_async_engine(get_settings().db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            options = dict(
                source_roots=[Path(root) for root in args.source_root],
                series_ids=args.series_id,
                apply=args.apply,
            )
            if args.command == "reconcile-import-paths":
                report = await reconcile_saved_mylar_paths(session, args.job, **options)
            else:
                report = await prepare_import_recheck(
                    session,
                    args.job,
                    **options,
                    accept_replaced_files=args.accept_replaced_files,
                )
            if args.apply:
                await session.commit()
            else:
                await session.rollback()
            print(json.dumps({"applied": args.apply, **report}, sort_keys=True))
            if args.apply:
                if report.get("series_prepared"):
                    print(
                        "Restart Pullbox to resume local matching of the saved review. "
                        "Sources were not modified."
                    )
                elif report.get("files_prepared"):
                    print(
                        "Restart Pullbox, open the completed import, and choose Retry failed. "
                        "Only rechecked failures will run again."
                    )
    finally:
        await engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    """Build the Pullbox management CLI parser."""
    parser = argparse.ArgumentParser(
        prog="pullbox",
        description="Pullbox management commands",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # reset-password
    rp = subparsers.add_parser(
        "reset-password",
        help="Reset a user's password (use when locked out of the web UI)",
    )
    rp.add_argument("--user", "-u", required=True, help="Username to reset")
    rp.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the new password from stdin instead of prompting",
    )
    recheck = subparsers.add_parser(
        "recheck-import", help="Recheck saved import evidence while Pullbox is stopped"
    )
    recheck.add_argument("--job", required=True, type=int, help="Saved REVIEW or COMPLETED job ID")
    recheck.add_argument(
        "--source-root",
        required=True,
        action="append",
        help="Permitted container-visible source directory; repeat as needed",
    )
    recheck.add_argument(
        "--series-id",
        type=int,
        action="append",
        help="Limit to these review series IDs; otherwise check automatic identity conflicts",
    )
    recheck.add_argument(
        "--offline",
        required=True,
        action="store_true",
        help="Acknowledge the Pullbox app is stopped and its database is backed up",
    )
    recheck.add_argument(
        "--apply", action="store_true", help="Persist changes; omitted means dry run"
    )
    recheck.add_argument(
        "--accept-replaced-files",
        action="store_true",
        help=(
            "Explicitly re-inspect changed or formerly missing files "
            "rather than retain a source-changed block"
        ),
    )
    reconcile = subparsers.add_parser(
        "reconcile-import-paths",
        help="Reconcile proven stale Mylar paths in an offline saved review",
    )
    reconcile.add_argument("--job", required=True, type=int)
    reconcile.add_argument("--source-root", required=True, action="append")
    reconcile.add_argument("--series-id", type=int, action="append")
    reconcile.add_argument(
        "--offline",
        required=True,
        action="store_true",
        help="Acknowledge Pullbox is stopped and its database is backed up",
    )
    reconcile.add_argument(
        "--apply", action="store_true", help="Persist repairs; default is preview only"
    )
    return parser


def main() -> None:
    """CLI entry point."""
    parser = build_parser()

    args = parser.parse_args()

    if args.command == "reset-password":
        candidate_secret = _read_password(password_stdin=args.password_stdin)
        asyncio.run(_reset_password(args.user, candidate_secret))
    elif args.command in {"recheck-import", "reconcile-import-paths"}:
        asyncio.run(_recheck_import(args))


if __name__ == "__main__":
    main()
