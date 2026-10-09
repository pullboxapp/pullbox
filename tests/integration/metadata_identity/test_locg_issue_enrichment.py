"""Fresh release dates enrich proven existing issues, never rematch or create them."""

import asyncio
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from pullbox.core.metadata_identity import IdentityNamespace, MetadataEntityKind
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models import Issue, LibraryFile, Series, User
from pullbox.models.issue import IssueStatus, IssueType
from pullbox.models.library import LibraryRoot
from pullbox.models.metadata_baseline import IssueMetadataBaseline
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.models.reader import IssueReaderState
from pullbox.models.whats_new import WhatsNewReleaseCache
from pullbox.schemas.metadata_snapshot import FieldOrigin, MetadataSnapshot
from pullbox.schemas.metadata_sources import MetadataDomain
from pullbox.services.metadata_baselines import (
    MetadataBaselineConflictError,
    MetadataBaselineWrite,
    load_metadata_baseline,
    save_metadata_baselines,
)
from pullbox.services.metadata_issue_refresh import refresh_issue_from_sources
from pullbox.services.metadata_series_refresh import refresh_series_from_sources
from tests.integration.metadata_identity.test_issue_refresh import IssueAdapter, reader
from tests.integration.metadata_identity.test_locg_series_enrichment import (
    configured_sources,  # noqa: F401
    prepare,
    release,
)
from tests.integration.metadata_identity.test_series_refresh import RefreshAdapter, refresh_registry

DAY = date(2026, 9, 30)


@pytest.fixture(params=["series", "issue"])
def refresh_command(request):
    async def refresh(session, series_id, issue_id, data, *, wait=None, started=None):
        adapter = (
            RefreshAdapter(data, session=session, wait=wait)
            if request.param == "series"
            else IssueAdapter(data.issues[0], session=session, wait=wait)
        )
        if started is not None:
            adapter.started = started
        if request.param == "series":
            return await refresh_series_from_sources(
                session,
                series_id,
                registry=refresh_registry(adapter),
            )
        return await refresh_issue_from_sources(
            session,
            issue_id,
            gcd_api_enabled=False,
            registry=reader(adapter),
        )

    return refresh


def issue_release(identifier=1001, **updates):
    return {
        **release(identifier),
        "issue_number": "1",
        "store_date": DAY.isoformat(),
        **updates,
    }


async def setup(factory, *, exact=True, rows=None, age=timedelta()):
    series_id, issue_id, data, cache_id = await prepare(factory, age=age)
    data.issues[0].store_date = None
    data.issues[0].cover_date = None if exact else DAY
    async with factory.begin() as session:
        issue = await session.get(Issue, issue_id)
        issue.store_date = None
        issue.release_date = data.issues[0].cover_date
        row = await session.get(IssueMetadataBaseline, issue_id)
        previous = MetadataSnapshot.model_validate_json(row.snapshot_json)
        row.snapshot_json = MetadataSnapshot.model_validate(
            {
                **previous.model_dump(),
                "values": {
                    **previous.values.model_dump(),
                    "store_date": None,
                    "cover_date": issue.release_date,
                },
                "origins": tuple(item for item in previous.origins if item.field != "store_date"),
            }
        ).model_dump_json()
        cache = await session.get(WhatsNewReleaseCache, cache_id)
        cache.payload = {
            "weeks": [
                {
                    "issues": rows
                    if rows is not None
                    else [
                        issue_release(**({"metron_issue_id": 100} if exact else {})),
                        issue_release(1002, **({"metron_issue_id": 100} if exact else {})),
                    ]
                }
            ]
        }
    return series_id, issue_id, data, cache_id


@pytest.mark.parametrize("exact", [True, False])
async def test_actual_refresh_fills_only_proven_store_date_and_preserves_issue_state(
    identity_probe_db, exact, refresh_command, tmp_path
):
    _, factory, _ = identity_probe_db
    series_id, issue_id, data, _ = await setup(factory, exact=exact)
    archive = tmp_path / "kept.cbz"
    archive.write_bytes(b"unchanged reference archive")
    async with factory.begin() as session:
        root = LibraryRoot(name="Reference", path=str(tmp_path))
        user = User(username="reader", password_hash="not-a-live-account")
        session.add_all([root, user])
        await session.flush()
        session.add(
            LibraryFile(
                issue_id=issue_id,
                library_root_id=root.id,
                file_path=str(archive),
                file_name=archive.name,
                file_format="cbz",
                file_size=archive.stat().st_size,
                file_modified_at=datetime.now(UTC),
                storage_mode="referenced",
            )
        )
        session.add(
            IssueReaderState(user_id=user.id, issue_id=issue_id, last_page_index=17, page_count=24)
        )
    async with factory() as session:
        before_claims = list(await session.scalars(select(IssueExternalIdentity.external_id)))
        await session.rollback()
        await refresh_command(session, series_id, issue_id, data)
        issue = await session.get(Issue, issue_id)
        assert issue.store_date == DAY, "proven cached release date never reaches issue refresh"
        assert issue.release_date == (None if exact else DAY), "store date is not a cover date"
        assert issue.title == "Original issue title"
        assert issue.status is IssueStatus.OWNED and issue.manual_skip
        assert await session.scalar(select(func.count()).select_from(Issue)) == 1
        assert (
            list(await session.scalars(select(IssueExternalIdentity.external_id))) == before_claims
        )
        saved = await load_metadata_baseline(session, MetadataEntityKind.ISSUE, issue_id)
        origin = next(item for item in saved.snapshot.origins if item.field == "store_date")
        assert origin.source is None and origin.passive_release is not None
        assert origin.passive_release.locg_series_id == "77"
        assert origin.passive_release.release_ids == ("1001", "1002")
        assert origin.passive_release.issue_identity in saved.snapshot.identities
        assert origin.passive_release.match_kind == ("exact_issue" if exact else "number_date")
        assert all(ref.namespace is not IdentityNamespace.LOCG for ref in saved.snapshot.identities)
        file = await session.scalar(select(LibraryFile))
        assert file.file_path == str(archive) and file.issue_id == issue_id
        assert file.storage_mode == "referenced" and file.file_size == archive.stat().st_size
        assert (await session.scalar(select(IssueReaderState))).last_page_index == 17
        await session.commit()
    async with factory() as session:
        await refresh_command(session, series_id, issue_id, data)
        assert (await session.get(Issue, issue_id)).store_date == DAY
        await session.commit()
    assert archive.read_bytes() == b"unchanged reference archive"


@pytest.mark.parametrize(
    "rows",
    [
        [issue_release(metron_issue_id=999)],
        [issue_release(metron_issue_id="100")],
        [issue_release(metron_issue_id=100, issue_number="2")],
        [issue_release(store_date="not-a-date")],
        [issue_release(metron_issue_id=100), issue_release(1002, store_date="2026-10-07")],
        [issue_release(metron_issue_id=100), issue_release(1002, metron_issue_id=999)],
        [
            issue_release(metron_issue_id=100),
            issue_release(1002, metron_issue_id=100, issue_number="2"),
        ],
        [
            issue_release(metron_issue_id=100),
            issue_release(metron_issue_id=100, store_date="2026-10-07"),
        ],
    ],
)
async def test_unknown_or_disagreeing_release_group_never_fills_the_gap(
    identity_probe_db, rows, refresh_command
):
    _, factory, _ = identity_probe_db
    series_id, issue_id, data, _ = await setup(factory, exact=False, rows=rows)
    async with factory() as session:
        await refresh_command(session, series_id, issue_id, data)
        assert (await session.get(Issue, issue_id)).store_date is None
        await session.commit()


@pytest.mark.parametrize(
    "case",
    [
        "stale",
        "future",
        "no_date_proof",
        "annual",
        "user_clear",
        "archive",
        "disagreement",
        "assembled_date_changed",
    ],
)
async def test_unproven_or_protected_date_stays_blank(identity_probe_db, case, refresh_command):
    _, factory, _ = identity_probe_db
    series_id, issue_id, data, _ = await setup(
        factory,
        exact=False,
        age=timedelta(hours=7)
        if case == "stale"
        else (timedelta(hours=-1) if case == "future" else timedelta()),
    )
    async with factory.begin() as session:
        issue = await session.get(Issue, issue_id)
        if case == "annual":
            issue.issue_type = IssueType.ANNUAL
        if case == "no_date_proof":
            issue.release_date = None
            data.issues[0].cover_date = None
        if case == "assembled_date_changed":
            data.issues[0].cover_date = date(2026, 10, 1)
            row = await session.get(IssueMetadataBaseline, issue_id)
            previous = MetadataSnapshot.model_validate_json(row.snapshot_json)
            row.snapshot_json = previous.model_copy(
                update={
                    "origins": (
                        *previous.origins,
                        FieldOrigin(
                            field="cover_date",
                            domain=MetadataDomain.CORE,
                            source=data.issues[0].source,
                            observed_at=datetime.now(UTC),
                        ),
                    )
                }
            ).model_dump_json()
        if case in {"user_clear", "archive", "disagreement"}:
            row = await session.get(IssueMetadataBaseline, issue_id)
            previous = MetadataSnapshot.model_validate_json(row.snapshot_json)
            row.snapshot_json = MetadataSnapshot.model_validate(
                {
                    **previous.model_dump(),
                    "origins": (
                        *previous.origins,
                        FieldOrigin(
                            field="store_date",
                            domain=MetadataDomain.CORE,
                            observed_at=datetime.now(UTC),
                            user_override=case == "user_clear",
                            embedded_documents=("ComicInfo.xml",) if case == "archive" else (),
                        ),
                    ),
                    "diagnostics": ("archive:issue:store_date:disagreement",)
                    if case == "disagreement"
                    else (),
                }
            ).model_dump_json()
    async with factory() as session:
        await refresh_command(session, series_id, issue_id, data)
        assert (await session.get(Issue, issue_id)).store_date is None
        await session.commit()


async def test_provider_value_wins_and_caller_rollback_keeps_original_date(
    identity_probe_db, refresh_command
):
    _, factory, _ = identity_probe_db
    series_id, issue_id, data, _ = await setup(factory)
    data.issues[0].store_date = date(2026, 10, 1)
    async with factory() as session:
        await refresh_command(session, series_id, issue_id, data)
        assert (await session.get(Issue, issue_id)).store_date == date(2026, 10, 1)
        await session.rollback()
    async with factory() as session:
        assert (await session.get(Issue, issue_id)).store_date is None
        assert await session.scalar(select(func.count()).select_from(Series)) == 1


@pytest.mark.parametrize(
    "change", ["cached_date", "issue_type", "cache_expired", "duplicate_number"]
)
async def test_date_or_match_change_during_fetch_aborts_before_any_metadata_write(
    identity_probe_db,
    change,
    refresh_command,
):
    _, factory, _ = identity_probe_db
    series_id, issue_id, data, cache_id = await setup(factory, exact=False)
    data.issues[0].title = "Must not save stale metadata"
    wait = asyncio.Event()
    started = asyncio.Event()
    async with factory() as session:
        task = asyncio.create_task(
            refresh_command(session, series_id, issue_id, data, wait=wait, started=started)
        )
        await asyncio.wait_for(started.wait(), 5)
        async with factory.begin() as other:
            if change == "cached_date":
                cache = await other.get(WhatsNewReleaseCache, cache_id)
                payload = deepcopy(cache.payload)
                payload["weeks"][0]["issues"][0]["store_date"] = "2026-10-07"
                cache.payload = payload
            elif change == "issue_type":
                (await other.get(Issue, issue_id)).issue_type = IssueType.ANNUAL
            elif change == "cache_expired":
                (await other.get(WhatsNewReleaseCache, cache_id)).fetched_at -= timedelta(hours=7)
            else:
                other.add(
                    Issue(
                        series_id=series_id,
                        issue_number=1,
                        issue_number_text=None,
                        release_date=DAY,
                    )
                )
        wait.set()
        with pytest.raises(ValueError, match="changed"):
            await task
        await session.commit()
    async with factory() as session:
        issue = await session.get(Issue, issue_id)
        assert issue.store_date is None and issue.title == "Original issue title"
        assert (
            await load_metadata_baseline(session, MetadataEntityKind.ISSUE, issue_id)
        ).revision == 1


async def test_a_user_clear_relative_to_baseline_is_not_refilled(
    identity_probe_db, refresh_command
):
    _, factory, _ = identity_probe_db
    series_id, issue_id, data, _ = await setup(factory)
    async with factory.begin() as session:
        row = await session.get(IssueMetadataBaseline, issue_id)
        previous = MetadataSnapshot.model_validate_json(row.snapshot_json)
        row.snapshot_json = previous.model_copy(
            update={"values": previous.values.model_copy(update={"store_date": DAY})}
        ).model_dump_json()
    async with factory() as session:
        await refresh_command(session, series_id, issue_id, data)
        assert (await session.get(Issue, issue_id)).store_date is None
        saved = await load_metadata_baseline(session, MetadataEntityKind.ISSUE, issue_id)
        assert next(
            item for item in saved.snapshot.origins if item.field == "store_date"
        ).user_override
        await session.commit()


@pytest.mark.parametrize("case", ["wrong_parent", "disputed_parent"])
async def test_issue_provenance_requires_its_current_verified_series_link(
    identity_probe_db, case, refresh_command
):
    _, factory, _ = identity_probe_db
    series_id, issue_id, data, _ = await setup(factory)
    async with factory() as session:
        await refresh_command(session, series_id, issue_id, data)
        await session.commit()
    async with factory() as session:
        saved = await load_metadata_baseline(session, MetadataEntityKind.ISSUE, issue_id)
        payload = saved.snapshot.model_dump(mode="json")
        if case == "wrong_parent":
            next(item for item in payload["origins"] if item["field"] == "store_date")[
                "passive_release"
            ]["locg_series_id"] = "88"
        else:
            parent = await session.scalar(
                select(SeriesExternalIdentity).where(
                    SeriesExternalIdentity.series_id == series_id,
                    SeriesExternalIdentity.identity_namespace == IdentityNamespace.LOCG,
                )
            )
            parent.verification_state = IdentityVerificationState.CONFLICTED
            await session.flush()
        with pytest.raises(MetadataBaselineConflictError, match=r"series.*release"):
            await save_metadata_baselines(
                session,
                [
                    MetadataBaselineWrite(
                        issue_id, MetadataSnapshot.model_validate(payload), saved.revision
                    )
                ],
            )
        await session.rollback()


@pytest.mark.parametrize("disagreement", [False, True])
async def test_many_variants_use_bounded_resolver_batches_and_share_one_issue(
    identity_probe_db, monkeypatch, disagreement, refresh_command
):
    from pullbox.services import metadata_locg_enrichment as enrichment

    _, factory, _ = identity_probe_db
    rows = [issue_release(1001 + index, metron_issue_id=100) for index in range(205)]
    if disagreement:
        rows[-1]["metron_issue_id"] = 999
    series_id, issue_id, data, _ = await setup(factory, rows=rows)
    real = enrichment.local_release_issues
    batches = []

    async def resolver(session, releases, owners):
        batches.append(len(releases))
        return await real(session, releases, owners)

    monkeypatch.setattr(enrichment, "local_release_issues", resolver)
    async with factory() as session:
        await refresh_command(session, series_id, issue_id, data)
        assert (await session.get(Issue, issue_id)).store_date == (None if disagreement else DAY)
        assert batches == [200, 5, 200, 5]
        assert await session.scalar(select(func.count()).select_from(Issue)) == 1
        await session.commit()
