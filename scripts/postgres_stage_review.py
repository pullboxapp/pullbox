"""Read-only operator review while the verified staging connection still exists."""

import getpass
import json
import sys
import warnings

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from pullbox.models.import_job import ImportedFile, ImportedSeries
from pullbox.models.issue import Issue
from pullbox.models.library import LibraryFile
from pullbox.models.series import Series
from pullbox.models.story_arc import IssueStoryArc, StoryArc
from pullbox.services.auth_service import AuthService
from pullbox.services.metadata_sources import read_source_policies
from pullbox.ui.formatters import format_issue_number
from pullbox.ui.import_follow_up import load_import_follow_up_context

PAGE_SIZE = 25
PAGED_COUNTS = ("series", "issues", "memberships", "story_arcs", "follow_up_jobs")


def _credentials() -> tuple[str, str]:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return (
            getpass.getpass("Pullbox username (hidden): "),
            getpass.getpass("Pullbox password (hidden): "),
        )


def _display(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _answer() -> str:
    return input().strip().lower()


async def _job_statuses(session, model, ids):
    by_job = {job_id: {} for job_id in ids}
    if ids:
        for job_id, status, total in await session.execute(
            select(model.import_job_id, model.status, func.count())
            .where(model.import_job_id.in_(ids))
            .group_by(model.import_job_id, model.status)
        ):
            by_job[job_id][status.value] = total
    return by_job


async def _page(session, page):
    def window(query):
        return query.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)

    series = await session.execute(window(select(Series.id, Series.title).order_by(Series.id)))
    issues = await session.execute(
        window(
            select(
                Issue.id, Series.title, Issue.issue_number, Issue.issue_number_text, Issue.status
            )
            .join(Series, Series.id == Issue.series_id)
            .order_by(Issue.id)
        )
    )
    arcs = await session.execute(window(select(StoryArc.id, StoryArc.name).order_by(StoryArc.id)))
    members = await session.execute(
        window(
            select(
                StoryArc.name,
                IssueStoryArc.sequence_number,
                IssueStoryArc.issue_id,
                Series.title,
                Issue.issue_number,
                Issue.issue_number_text,
                IssueStoryArc.source_series_name,
                IssueStoryArc.source_issue_number_text,
            )
            .join(IssueStoryArc, IssueStoryArc.story_arc_id == StoryArc.id)
            .outerjoin(Issue, Issue.id == IssueStoryArc.issue_id)
            .outerjoin(Series, Series.id == Issue.series_id)
            .order_by(
                StoryArc.id,
                IssueStoryArc.sequence_number,
                IssueStoryArc.source_ordinal,
                IssueStoryArc.id,
            )
        )
    )
    follow = await load_import_follow_up_context(
        session, view="all", requested_page=page, job_id=None
    )
    jobs = follow["follow_up_jobs"] if follow["page"] == page else []
    ids = [job.id for job in jobs]
    series_status = await _job_statuses(session, ImportedSeries, ids)
    file_status = await _job_statuses(session, ImportedFile, ids)
    return {
        "series": [dict(row) for row in series.mappings()],
        "issues": [
            {
                "id": id_,
                "series": title,
                "number": format_issue_number(number, exact),
                "status": status.value,
            }
            for id_, title, number, exact, status in issues
        ],
        "story_arcs": [dict(row) for row in arcs.mappings()],
        "reading_order": [
            {
                "arc": name,
                "position": pos,
                "issue_id": issue_id,
                "series": title or source_title,
                "issue_number": format_issue_number(number, exact)
                if number is not None
                else source_no,
            }
            for name, pos, issue_id, title, number, exact, source_title, source_no in members
        ],
        "follow_up_jobs": [
            {
                "id": job.id,
                "source": job.source_type.value,
                "status": job.status.value,
                "series_statuses": series_status[job.id],
                "file_statuses": file_status[job.id],
            }
            for job in jobs
        ],
    }, follow["total"]


class ReviewBlockedError(Exception):
    """Only fixed blocker codes may appear in the shareable report."""


async def _inspect(conn, digest):
    # Row transfer uses a raw text JSON codec. Restore the app dialect's decoder
    # before reading provider policies or hydrated import jobs on this connection.
    raw = await conn.get_raw_connection()
    await conn.dialect.setup_asyncpg_json_codec(raw.dbapi_connection)
    async with conn.begin():
        await conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        await conn.exec_driver_sql("SET LOCAL search_path TO pg_catalog, public")
        await conn.exec_driver_sql("SET LOCAL idle_in_transaction_session_timeout TO '10min'")
        sealed = await conn.scalar(
            text(
                "SELECT NOT datallowconn AND shobj_description(oid,'pg_database') = "
                "'pullbox:offline-stage:rows-verified' AND "
                "NOT EXISTS(SELECT FROM pg_stat_activity "
                "WHERE datid=d.oid AND pid<>pg_backend_pid()) "
                "AND current_setting('transaction_read_only')='on' AND "
                "(SELECT snapshot_sha256 FROM pullbox_stage.verification)=:digest "
                "FROM pg_database d WHERE datname=current_database()"
            ),
            {"digest": digest},
        )
        if not sealed:
            raise ReviewBlockedError("stage_not_verified")
        async with AsyncSession(bind=conn, autoflush=False) as session:
            _display(
                "Rows verified. Sign in with an existing Pullbox account for read-only review."
            )
            for _ in range(3):
                if await AuthService.check_credentials(session, *_credentials()):
                    break
                _display("Sign-in did not match an active account.")
            else:
                raise ReviewBlockedError("login_failed")
            counts = {
                key: await session.scalar(select(func.count()).select_from(model))
                for key, model in (
                    ("series", Series),
                    ("issues", Issue),
                    ("library_files", LibraryFile),
                    ("story_arcs", StoryArc),
                    ("memberships", IssueStoryArc),
                )
            }
            policies = await read_source_policies(session)
            display_policies = [
                {
                    "source": p.source.value,
                    "enabled": p.enabled,
                    "priority": p.priority,
                    "domain_priorities": p.domain_priorities,
                    "configuration_status": p.configuration_status,
                }
                for p in policies
            ]
            page = 1
            while True:
                rows, follow = await _page(session, page)
                counts.update(follow_up_jobs=follow, metadata_sources=len(policies))
                largest = max(counts[k] for k in PAGED_COUNTS)
                pages = max(1, (largest + PAGE_SIZE - 1) // PAGE_SIZE)
                # Escape control characters in imported titles, including terminal escapes.
                _display(f"Read-only library review | Page {page} of {pages}")
                _display("Totals: " + json.dumps(counts, sort_keys=True))
                for label, entries in {"metadata_providers": display_policies, **rows}.items():
                    _display(label + ":")
                    for entry in entries:
                        _display("  " + json.dumps(entry, ensure_ascii=True))
                    if not entries:
                        _display("  No entries on this page.")
                if any(p.configuration_status for p in policies):
                    raise ReviewBlockedError("provider_configuration_invalid")
                _display(
                    "Enter next, back, a page number, confirm (the snapshot looks right), or quit:"
                )
                action = _answer()
                if action == "confirm":
                    return {"status": "confirmed", **counts}
                if action == "quit":
                    raise ReviewBlockedError("cancelled")
                if action == "next":
                    page = min(pages, page + 1)
                elif action == "back":
                    page = max(1, page - 1)
                elif action.isascii() and action.isdecimal() and len(action) <= 12:
                    page = min(pages, max(1, int(action)))


async def review_library(conn, digest, report):
    try:
        report["maintenance_review"] = await _inspect(conn, digest)
        return
    except ReviewBlockedError as exc:
        status = str(exc)
    except (EOFError, KeyboardInterrupt):
        status = "cancelled"
    except Exception:
        # Driver, validation and terminal errors can include private strings.
        status = "review_unavailable"
    report["maintenance_review"] = {"status": status}
    report["blockers"] = {status: "Library review did not pass. Keep the verified copy offline."}
