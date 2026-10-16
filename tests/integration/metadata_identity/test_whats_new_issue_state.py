"""Read-only release state never invents identity, ownership or acquisition."""

from copy import deepcopy
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import event

from pullbox.core.issue_numbers import parse_issue_number_text
from pullbox.core.metadata_identity import IdentityEvidenceKind, IdentityNamespace
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models import (
    DownloadHistory,
    DownloadState,
    Issue,
    IssueStatus,
    LibraryFile,
    LibraryRoot,
    Series,
)
from pullbox.models.download import DownloadClientType
from pullbox.models.issue import IssueType
from pullbox.models.library import FileFormat
from pullbox.models.metadata_identity import IssueExternalIdentity
from pullbox.models.pending_match import PendingMatch, PendingMatchStatus
from pullbox.services.whats_new_issue_state import local_release_issues

DAY = date(2026, 9, 30)


def release(number="3", **fields):
    return {
        "locg_issue_id": 8204875,
        "locg_series_id": 210209,
        "series": {"title": "Badrock", "locg_series_id": 210209},
        "issue_number": number,
        "store_date": DAY,
        **fields,
    }


async def prepare(session, tmp_path, *, number="3", status=IssueStatus.WANTED):
    root = LibraryRoot(name="Release states", path=str(tmp_path))
    series = Series(title="Badrock", sort_title="Badrock", year_start=2026)
    session.add_all([root, series])
    await session.flush()
    issue = Issue(
        series_id=series.id,
        issue_number=parse_issue_number_text(number)[0],
        issue_number_text=number,
        store_date=DAY,
        status=status,
    )
    session.add(issue)
    await session.flush()
    return root, series, issue


async def add_file(session, root, issue):
    session.add(
        LibraryFile(
            issue_id=issue.id,
            library_root_id=root.id,
            file_path=f"{root.path}/{issue.id}.cbz",
            file_name=f"{issue.id}.cbz",
            file_size=10,
            file_format=FileFormat.CBZ,
            file_modified_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def add_download(session, issue, state, *, imported=False):
    download = DownloadHistory(
        issue_id=issue.id,
        title="Badrock #3",
        download_url="https://example.test/comic",
        download_client=DownloadClientType.DIRECT,
        state=state,
        imported_at=datetime.now(UTC) if imported else None,
    )
    session.add(download)
    await session.flush()


async def claim(session, issue, namespace, external_id, *, verified=True):
    session.add(
        IssueExternalIdentity(
            issue_id=issue.id,
            identity_namespace=namespace,
            external_id=str(external_id),
            verification_state=IdentityVerificationState.VERIFIED
            if verified
            else IdentityVerificationState.CONFLICTED,
            evidence_kind=IdentityEvidenceKind.USER_SELECTION,
        )
    )
    await session.flush()


@pytest.mark.parametrize(
    "status,owned,expected",
    [
        (IssueStatus.WANTED, False, "missing"),
        (IssueStatus.SKIPPED, False, "skipped"),
        (IssueStatus.OWNED, True, "owned"),
        (IssueStatus.OWNED, False, "needs_review"),
        (IssueStatus.DOWNLOADING, False, "needs_review"),
    ],
)
async def test_status_uses_registered_ownership_not_optimistic_flags(
    identity_probe_db, tmp_path, status, owned, expected
):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        root, series, issue = await prepare(session, tmp_path, status=status)
        if owned:
            await add_file(session, root, issue)
        source = release()
        snapshot = deepcopy(source)
        state = (await local_release_issues(session, [source], {"210209": series}))[0]
        assert state is not None, "confirmed release has no local issue state"
        assert state.state.value == expected
        assert state.issue_id == issue.id
        assert source == snapshot
        assert not session.new and not session.dirty and not session.deleted


@pytest.mark.parametrize(
    "download,expected",
    [
        (DownloadState.QUEUED, "queued"),
        (DownloadState.SENT, "queued"),
        (DownloadState.RETRY_PENDING, "queued"),
        (DownloadState.DOWNLOADING, "downloading"),
        (DownloadState.PAUSED, "paused"),
        (DownloadState.FINALIZING, "processing"),
        (DownloadState.POST_PROCESSING, "processing"),
        (DownloadState.COMPLETED, "processing"),
        (DownloadState.FAILED, "missing"),
        (DownloadState.IMPORTED, "missing"),
    ],
)
async def test_actual_download_history_drives_state(
    identity_probe_db, tmp_path, download, expected
):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        _, series, issue = await prepare(session, tmp_path)
        await add_download(session, issue, download)
        state = (await local_release_issues(session, [release()], {"210209": series}))[0]
        assert state is not None, "existing download is not reflected on its release"
        assert state.state.value == expected


async def test_active_replacement_wins_over_owned_and_old_failed_records(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        root, series, issue = await prepare(session, tmp_path, status=IssueStatus.OWNED)
        await add_file(session, root, issue)
        await add_download(session, issue, DownloadState.DOWNLOADING)
        await add_download(session, issue, DownloadState.FAILED)
        await add_download(session, issue, DownloadState.COMPLETED, imported=True)
        state = (await local_release_issues(session, [release()], {"210209": series}))[0]
        assert state is not None
        assert state.state.value == "downloading"


@pytest.mark.parametrize("number", ["13a", "13b", "13c", "50-x", "50-o"])
async def test_lettered_numbers_remain_distinct(identity_probe_db, tmp_path, number):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        _, series, issue = await prepare(session, tmp_path, number=number)
        state = (await local_release_issues(session, [release(number)], {"210209": series}))[0]
        assert state is not None
        assert state.issue_id == issue.id
        assert state.state.value == "missing"


@pytest.mark.parametrize(
    "case",
    [
        "unknown_number",
        "undated",
        "different_date",
        "annual",
        "unknown_cross_id",
        "disputed",
        "contradictory_series",
        "foreign_cross_id",
    ],
)
async def test_uncertain_evidence_never_selects_an_issue(identity_probe_db, tmp_path, case):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        _, series, issue = await prepare(session, tmp_path)
        source = release()
        if case == "unknown_number":
            source["issue_number"] = "Annual 3"
        elif case == "undated":
            issue.store_date = None
        elif case == "different_date":
            issue.store_date = date(2026, 10, 28)
        elif case == "annual":
            issue.issue_type = IssueType.ANNUAL
        elif case == "unknown_cross_id":
            source["metron_issue_id"] = 999
        elif case == "disputed":
            await claim(session, issue, IdentityNamespace.METRON, 123, verified=False)
        elif case == "contradictory_series":
            source["locg_series_id"] = 999
        elif case == "foreign_cross_id":
            other = Series(title="Other series", sort_title="Other series")
            session.add(other)
            await session.flush()
            foreign = Issue(series_id=other.id, issue_number=3, store_date=DAY)
            session.add(foreign)
            await session.flush()
            await claim(session, foreign, IdentityNamespace.METRON, 999)
            source["metron_issue_id"] = 999
        await session.flush()
        state = (await local_release_issues(session, [source], {"210209": series}))[0]
        assert state is not None, "uncertainty needs a visible, non-actionable state"
        assert state.issue_id is None
        assert state.state.value == "unresolved"


async def test_variant_crosswalks_must_converge_not_inherit_the_primary(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        _, series, issue = await prepare(session, tmp_path)
        await claim(session, issue, IdentityNamespace.METRON, 123)
        second = Issue(series_id=series.id, issue_number=4, store_date=DAY)
        session.add(second)
        await session.flush()
        await claim(session, second, IdentityNamespace.METRON, 456)
        source = release(metron_issue_id=123)
        source["_discovery_releases"] = [source.copy(), release(metron_issue_id=456)]
        state = (await local_release_issues(session, [source], {"210209": series}))[0]
        assert state is not None
        assert state.issue_id is None
        assert state.state.value == "unresolved"
        source["_discovery_releases"] = [release(metron_issue_id=123), release(metron_issue_id=123)]
        state = (await local_release_issues(session, [source], {"210209": series}))[0]
        assert state.issue_id == issue.id


async def test_no_confirmed_series_does_not_match_similar_titles(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        await prepare(session, tmp_path)
        assert await local_release_issues(session, [release()], {}) == [None]


@pytest.mark.parametrize(
    "case", ["different_variant_series", "duplicate_number", "malformed_variant"]
)
async def test_group_or_catalog_ambiguity_cannot_select_the_primary(
    identity_probe_db, tmp_path, case
):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        _, series, _ = await prepare(session, tmp_path)
        source = release()
        if case == "different_variant_series":
            variant = release(locg_series_id=999, series={"locg_series_id": 999})
            source["_discovery_releases"] = [release(), variant]
        elif case == "malformed_variant":
            source["_discovery_releases"] = [release(), None]
        else:
            duplicate = Issue(series_id=series.id, issue_number=3, store_date=DAY)
            duplicate.issue_number_text = None
            session.add(duplicate)
            await session.flush()
        state = (await local_release_issues(session, [source], {"210209": series}))[0]
        assert state is not None
        assert state.issue_id is None


async def test_pending_review_blocks_missing_without_mutating_pending_match(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        _, series, issue = await prepare(session, tmp_path)
        held = PendingMatch(
            issue_id=issue.id,
            release_title="Badrock #3",
            download_url="https://example.test/held",
            confidence="medium",
        )
        session.add(held)
        await session.flush()
        state = (await local_release_issues(session, [release()], {"210209": series}))[0]
        assert state is not None
        assert state.state.value == "needs_review"
        assert held.status == PendingMatchStatus.PENDING
        assert not session.dirty


async def test_exact_id_can_resolve_a_date_change_but_not_a_disagreeing_number(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        _, series, issue = await prepare(session, tmp_path)
        await claim(session, issue, IdentityNamespace.METRON, 123)
        source = release(metron_issue_id=123, store_date=date(2026, 11, 11))
        state = (await local_release_issues(session, [source], {"210209": series}))[0]
        assert state is not None and state.issue_id == issue.id
        source["issue_number"] = "4"
        state = (await local_release_issues(session, [source], {"210209": series}))[0]
        assert state is not None and state.issue_id is None


async def test_direct_series_id_remains_supported_in_sparse_old_summaries(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        _, series, issue = await prepare(session, tmp_path)
        source = release(series={"title": "Badrock"})
        state = (await local_release_issues(session, [source], {"210209": series}))[0]
        assert state is not None and state.issue_id == issue.id


@pytest.mark.parametrize("provider_id", [True, 0, -1, "123", 2**100])
async def test_invalid_cross_ids_never_fall_back_or_overflow_database(
    identity_probe_db, tmp_path, provider_id
):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        _, series, issue = await prepare(session, tmp_path)
        issue.comicvine_id = 123
        await session.flush()
        state = (
            await local_release_issues(
                session, [release(comicvine_issue_id=provider_id)], {"210209": series}
            )
        )[0]
        assert state is not None and state.issue_id is None


async def test_legacy_comicvine_projection_disagreement_is_not_verified_by_a_claim(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        _, series, issue = await prepare(session, tmp_path)
        issue.comicvine_id = 456
        await claim(session, issue, IdentityNamespace.COMICVINE, 123)
        state = (
            await local_release_issues(
                session, [release(comicvine_issue_id=123)], {"210209": series}
            )
        )[0]
        assert state is not None and state.issue_id is None


@pytest.mark.parametrize("count", [1, 25, 100])
async def test_visible_state_resolution_has_constant_queries_and_no_writes(
    identity_probe_db, tmp_path, count
):
    engine, factory, _ = identity_probe_db
    async with factory.begin() as session:
        _, series, issue = await prepare(session, tmp_path)
        sources = [release()]
        expected_ids = [issue.id]
        for number in range(4, count + 3):
            item = Issue(series_id=series.id, issue_number=number, store_date=DAY)
            session.add(item)
            await session.flush()
            sources.append(release(str(number), locg_issue_id=8204875 + number))
            expected_ids.append(item.id)
        queries = []

        def record(_conn, _cursor, statement, _params, _context, _many):
            queries.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            states = await local_release_issues(session, sources, {"210209": series})
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
        assert [state.issue_id if state else None for state in states] == expected_ids
        assert 1 <= len(queries) <= 3
        assert all(statement.lstrip().upper().startswith("SELECT") for statement in queries)
