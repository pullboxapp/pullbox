"""Provider-neutral Add Series persistence with SQLite/PostgreSQL parity."""

import asyncio
from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import delete, func, select, update

from pullbox.core.metadata_identity import ExternalIdentityRef
from pullbox.core.metadata_identity import IdentityNamespace as Namespace
from pullbox.core.metadata_identity import MetadataEntityKind as Kind
from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.core.metadata_identity_events import prepare_identity_event
from pullbox.core.metadata_identity_state import IdentityVerificationState
from pullbox.models import Issue, Series
from pullbox.models.issue import IssueStatus
from pullbox.models.metadata_identity import (
    IssueExternalIdentity,
    IssueIdentityEvent,
    SeriesExternalIdentity,
    SeriesIdentityEvent,
)
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.models.publisher import Publisher
from pullbox.models.series import IssueCatalogState
from pullbox.schemas.metadata_snapshot import MetadataSnapshot
from pullbox.services.metadata_series_adoption import (
    SeriesAdoptionError,
    SourceSeriesBundle,
    adopt_source_series_bundle,
)
from tests.integration.metadata_identity.test_attachment_service import _request
from tests.unit.test_metadata_discovery import row
from tests.unit.test_metadata_series_adoption_fetch import CatalogAdapter, fetch
from tests.unit.test_metadata_source_reads import issue_row


@pytest.fixture(autouse=True)
async def configured_sources(identity_probe_db):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        for priority, source in enumerate(Source):
            session.add(
                MetadataSourceConfig(
                    source=source.value, enabled=True, priority=priority, revision=1
                )
            )


def bundle(*, crosswalk=False, numbers=("13a", "13b", "50-x", "50-o", "0.5", "-1")):
    series = row(Source.METRON_API, title="Source-only series").model_copy(
        update={"issue_count": len(numbers)}
    )
    if crosswalk:
        series.cross_identities = [ExternalIdentityRef(Namespace.COMICVINE, Kind.SERIES, "500")]
    return SourceSeriesBundle(
        series,
        tuple(
            issue_row(
                external_id=str(100 + i), issue_number_text=number, issue_number_key=number.upper()
            )
            for i, number in enumerate(numbers)
        ),
        1,
        len(numbers),
    )


async def test_new_series_without_comicvine_id_has_verified_native_identity_and_full_catalog(
    identity_probe_db,
):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, bundle(), monitored=True)
        assert result.created and result.series.comicvine_id is None
        assert result.series.monitored
        assert result.series.issue_catalog_state is IssueCatalogState.COMPLETE
        assert result.series.issue_count == 6
        issues = list(await session.scalars(select(Issue).order_by(Issue.id)))
        assert [issue.issue_number_text for issue in issues] == [
            "13A",
            "13B",
            "50-X",
            "50-O",
            "0.5",
            "-1",
        ]
        assert all(
            issue.comicvine_id is None and issue.status is IssueStatus.WANTED for issue in issues
        )
        assert await session.scalar(select(func.count()).select_from(SeriesExternalIdentity)) == 1
        assert await session.scalar(select(func.count()).select_from(IssueExternalIdentity)) == 6
        assert await session.scalar(select(func.count()).select_from(IssueIdentityEvent)) == 6


async def test_new_add_returns_canonical_snapshots_matching_persisted_metadata(identity_probe_db):
    _, factory, _ = identity_probe_db
    data = bundle()
    data.series.description = "Canonical description"
    data.issues[0].cover_date = date(2026, 9, 28)
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, data)
        snapshot = getattr(result, "snapshot", None)
        assert isinstance(snapshot, MetadataSnapshot)
        assert snapshot.values.title == result.series.title
        assert snapshot.values.description == result.series.description
        assert snapshot.identities == (ExternalIdentityRef(Namespace.METRON, Kind.SERIES, "42"),)
        snapshots = result.issue_snapshots
        issues = list(await session.scalars(select(Issue).order_by(Issue.id)))
        assert len(snapshots) == len(issues) == 6
        for item, issue in zip(snapshots, issues, strict=True):
            assert item.values.title == issue.title
            assert item.values.cover_date == issue.release_date
            assert item.values.issue_number_text == issue.issue_number_text


@pytest.mark.parametrize("summary_count", [1, -1, 1000001, 1000000000])
async def test_stale_summary_adopts_complete_catalog_and_derives_saved_count(
    identity_probe_db, summary_count
):
    _, factory, _ = identity_probe_db
    data = bundle(numbers=("1", "2"))
    data.series.issue_count = summary_count
    async with factory.begin() as session:
        try:
            result = await adopt_source_series_bundle(session, data)
        except SeriesAdoptionError as exc:
            pytest.fail(f"A stale summary cannot veto the complete catalog: {exc}")
        assert result.series.issue_count == result.snapshot.values.issue_count == 2
        origin = next(item for item in result.snapshot.origins if item.field == "issue_count")
        assert origin.derivation == "catalog"
        assert await session.scalar(select(func.count()).select_from(Issue)) == 2
        assert data.series.issue_count == summary_count


async def test_representative_metron_cover_survives_add_and_session_reload(identity_probe_db):
    _, factory, _ = identity_probe_db
    adapter = CatalogAdapter(1)
    page = await adapter.issues("42")
    cover = "https://static.metron.cloud/media/issue/first.jpg"
    page.data.results[0].image_url = cover
    adapter.pages[1] = page
    data = replace(await fetch(adapter), source_revision=1)
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, data)
        assert result.snapshot.values.image_url == cover
        series_id = result.series.id
    async with factory() as session:
        assert (await session.get(Series, series_id)).cover_url == cover
        assert (await session.scalar(select(Issue))).cover_url == cover


async def test_add_filters_unsafe_artwork_before_database_write(identity_probe_db):
    _, factory, _ = identity_probe_db
    data = bundle()
    data.series.image_url = "https://127.0.0.1/private"
    data.issues[0].image_url = "file:///private"
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, data)
        assert result.series.cover_url is None
        issue = await session.scalar(select(Issue).order_by(Issue.id))
        assert issue.cover_url is None


async def test_caller_rollback_removes_whole_catalog_and_evidence(identity_probe_db):
    _, factory, _ = identity_probe_db
    async with factory() as session:
        await adopt_source_series_bundle(session, bundle())
        await session.rollback()
    async with factory() as session:
        for model in (
            Series,
            Issue,
            SeriesExternalIdentity,
            IssueExternalIdentity,
            SeriesIdentityEvent,
            IssueIdentityEvent,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_repeated_add_preserves_existing_user_state_and_issues(identity_probe_db):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        first = await adopt_source_series_bundle(session, bundle(), monitored=False)
        first_id = first.series.id
        first.series.title = "User title"
        first.series.path = "/read-only/original"
        first.series.issue_catalog_state = IssueCatalogState.PARTIAL
        issue = await session.scalar(select(Issue).order_by(Issue.id))
        issue.status = IssueStatus.OWNED
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, bundle(), monitored=True)
        assert result.series.id == first_id and not result.created
        assert result.series.title == "User title" and not result.series.monitored
        assert result.series.path == "/read-only/original"
        assert result.series.issue_catalog_state is IssueCatalogState.PARTIAL
        assert (await session.scalar(select(Issue).order_by(Issue.id))).status is IssueStatus.OWNED
        assert await session.scalar(select(func.count()).select_from(Series)) == 1
        assert await session.scalar(select(func.count()).select_from(Issue)) == 6
        assert await session.scalar(select(func.count()).select_from(SeriesIdentityEvent)) == 1


async def test_proven_series_crosswalk_reuses_legacy_owner_without_replacing_metadata(
    identity_probe_db,
):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        old = Series(
            title="Already here", sort_title="Already here", comicvine_id=500, monitored=False
        )
        session.add(old)
        await session.flush()
        old_id = old.id
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, bundle(crosswalk=True), monitored=True)
        assert not result.created and result.series.id == old_id
        assert result.series.title == "Already here" and not result.series.monitored
        assert await session.scalar(select(func.count()).select_from(SeriesExternalIdentity)) == 2
        assert await session.scalar(select(func.count()).select_from(Issue)) == 0


async def test_same_title_is_not_identity_evidence(identity_probe_db):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        session.add(
            Series(title="Source-only series", sort_title="Source-only series", comicvine_id=999)
        )
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, bundle())
        assert result.created
        assert await session.scalar(select(func.count()).select_from(Series)) == 2


@pytest.mark.parametrize(
    "problem",
    [
        "duplicate_number",
        "duplicate_id",
        "unknown_designation",
        "wrong_parent",
        "wrong_source",
        "incomplete",
        "unknown_profile_count",
    ],
)
async def test_bad_bundle_never_leaves_partial_rows_even_when_caller_commits(
    identity_probe_db, problem
):
    _, factory, _ = identity_probe_db
    data = bundle()
    if problem == "duplicate_number":
        data.issues[1].issue_number_text = "13A"
    elif problem == "duplicate_id":
        data.issues[1].external_id = data.issues[0].external_id
    elif problem == "unknown_designation":
        data.issues[1].issue_number_text = "SPECIAL"
    elif problem == "wrong_parent":
        data.issues[1].series_external_id = "999"
    elif problem == "wrong_source":
        data.issues[1].source = Source.COMICVINE_API
    else:
        if problem == "unknown_profile_count":
            data.series.issue_count = None
        data = replace(data, issues=data.issues[:1])
    async with factory.begin() as session:
        with pytest.raises(SeriesAdoptionError):
            await adopt_source_series_bundle(session, data)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Series)) == 0
        assert await session.scalar(select(func.count()).select_from(SeriesIdentityEvent)) == 0


async def test_issue_crosswalk_without_foreign_parent_proof_is_observation_not_ownership(
    identity_probe_db,
):
    _, factory, _ = identity_probe_db
    data = bundle(crosswalk=True, numbers=("1",))
    data.issues[0].cross_identities = [ExternalIdentityRef(Namespace.COMICVINE, Kind.ISSUE, "700")]
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, data)
        assert result.series.comicvine_id == 500
        issue = await session.scalar(select(Issue))
        assert issue.comicvine_id is None
        claims = list(
            await session.scalars(select(IssueIdentityEvent).order_by(IssueIdentityEvent.id))
        )
        assert {
            (claim.identity_namespace.value, claim.verification_state.value) for claim in claims
        } == {("metron", "verified"), ("comicvine", "observed")}


async def test_concurrent_adds_never_leave_duplicate_series_or_orphans(identity_probe_db):
    _, factory, _ = identity_probe_db

    async def add():
        try:
            async with factory.begin() as session:
                result = await adopt_source_series_bundle(session, bundle())
                return result.series.id
        except SeriesAdoptionError:
            return None

    results = await asyncio.gather(add(), add())
    assert any(value is not None for value in results)
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, bundle())
        assert not result.created
        assert {value for value in results if value is not None} == {result.series.id}
        assert await session.scalar(select(func.count()).select_from(Series)) == 1
        assert await session.scalar(select(func.count()).select_from(Issue)) == 6
        assert await session.scalar(select(func.count()).select_from(SeriesExternalIdentity)) == 1
        assert await session.scalar(select(func.count()).select_from(IssueExternalIdentity)) == 6


async def test_competing_series_crosswalk_owners_are_not_merged(identity_probe_db):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        native = await adopt_source_series_bundle(session, bundle())
        native_id = native.series.id
        session.add(Series(title="Other owner", sort_title="Other owner", comicvine_id=500))
    async with factory.begin() as session:
        with pytest.raises(SeriesAdoptionError, match="different library series"):
            await adopt_source_series_bundle(session, bundle(crosswalk=True))
    async with factory() as session:
        assert (await session.get(Series, native_id)).comicvine_id is None
        assert await session.scalar(select(func.count()).select_from(Series)) == 2


@pytest.mark.parametrize(
    "state", [IdentityVerificationState.REJECTED, IdentityVerificationState.CONFLICTED]
)
async def test_existing_review_decisions_block_crosswalk_adoption(identity_probe_db, state):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        old = Series(title="Existing", sort_title="Existing", comicvine_id=500)
        session.add(old)
        await session.flush()
        saved = prepare_identity_event(_request(old.id, "500"))
        session.add(
            SeriesIdentityEvent(
                series_id=old.id,
                identity_namespace=Namespace.COMICVINE,
                external_id="500",
                verification_state=state,
                evidence_kind="comicinfo_xml",
                event_key=saved.event_key,
                request_fingerprint=saved.request_fingerprint,
                request_json=saved.request_json,
            )
        )
    async with factory.begin() as session:
        with pytest.raises(SeriesAdoptionError, match="Review"):
            await adopt_source_series_bundle(session, bundle(crosswalk=True))
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(SeriesExternalIdentity)) == 0
        assert await session.scalar(select(func.count()).select_from(SeriesIdentityEvent)) == 1
        assert await session.scalar(select(func.count()).select_from(Issue)) == 0


async def test_owned_issue_crosswalk_does_not_create_a_second_issue(identity_probe_db):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        old = Series(title="Existing", sort_title="Existing", comicvine_id=999)
        session.add(old)
        await session.flush()
        session.add(
            Issue(series_id=old.id, comicvine_id=700, issue_number=1, status=IssueStatus.OWNED)
        )
    data = bundle(numbers=("1",))
    data.issues[0].cross_identities = [ExternalIdentityRef(Namespace.COMICVINE, Kind.ISSUE, "700")]
    async with factory.begin() as session:
        with pytest.raises(SeriesAdoptionError, match="another library series"):
            await adopt_source_series_bundle(session, data)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Series)) == 1
        assert await session.scalar(select(func.count()).select_from(Issue)) == 1


async def test_late_failure_rolls_back_publisher_catalog_and_all_identity_events(
    identity_probe_db, monkeypatch
):
    _, factory, _ = identity_probe_db
    data = bundle(numbers=("1",))
    data.series.publisher = "New publisher"
    data.issues[0].cross_identities = [ExternalIdentityRef(Namespace.COMICVINE, Kind.ISSUE, "700")]

    async def fail(*args, **kwargs):
        raise SeriesAdoptionError("synthetic late failure")

    monkeypatch.setattr(
        "pullbox.services.metadata_series_adoption.record_identity_observation", fail
    )
    async with factory.begin() as session:
        with pytest.raises(SeriesAdoptionError, match="late failure"):
            await adopt_source_series_bundle(session, data)
    async with factory() as session:
        for model in (
            Publisher,
            Series,
            Issue,
            SeriesExternalIdentity,
            IssueExternalIdentity,
            SeriesIdentityEvent,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("source", [Source.COMICVINE_API, Source.COMICVINE_LOCAL])
async def test_comicvine_native_add_preserves_compatibility_ids(identity_probe_db, source):
    _, factory, _ = identity_probe_db
    data = bundle(numbers=("50-x",))
    data.series.source = source
    data.series.identity_namespace = Namespace.COMICVINE
    data.issues[0].source = source
    data.issues[0].identity_namespace = Namespace.COMICVINE
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, data)
        assert result.series.comicvine_id == 42
        expected_source = "comicvine" if source is Source.COMICVINE_API else "pullbox_catalog"
        assert result.series.metadata_source == expected_source
    async with factory() as session:
        issue = await session.scalar(select(Issue))
        assert issue.comicvine_id == 100 and issue.issue_number_text == "50-X"
        assert issue.status is IssueStatus.SKIPPED
        assert issue.metadata_source == expected_source


@pytest.mark.parametrize("change", ["revision", "disabled", "removed"])
async def test_source_changed_after_fetch_cannot_be_adopted(identity_probe_db, change):
    _, factory, _ = identity_probe_db
    async with factory.begin() as session:
        condition = MetadataSourceConfig.source == Source.METRON_API.value
        if change == "removed":
            await session.execute(delete(MetadataSourceConfig).where(condition))
        else:
            values = {"revision": 2} if change == "revision" else {"enabled": False}
            await session.execute(update(MetadataSourceConfig).where(condition).values(**values))
    async with factory.begin() as session:
        with pytest.raises(SeriesAdoptionError, match="settings changed"):
            await adopt_source_series_bundle(session, bundle())
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Series)) == 0


@pytest.mark.parametrize(
    "series_type,issue_type",
    [
        ("annual", "annual"),
        ("tpb", "tpb"),
        ("hardcover", "hc"),
        ("graphic_novel", "gn"),
        ("omnibus", "omnibus"),
    ],
)
async def test_provider_format_is_inherited_by_new_issues(
    identity_probe_db, series_type, issue_type
):
    _, factory, _ = identity_probe_db
    data = bundle(numbers=("1",))
    data.series.series_type = series_type
    async with factory.begin() as session:
        await adopt_source_series_bundle(session, data)
        assert (await session.scalar(select(Issue))).issue_type.value == issue_type


async def test_legacy_title_classification_and_date_inference_still_apply(identity_probe_db):
    _, factory, _ = identity_probe_db
    data = bundle(numbers=("1",))
    data.series.title = "Batman Annual"
    data.series.year_start = 2020
    data.issues[0].cover_date = date(2020, 1, 1)
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, data)
        assert result.series.series_type.value == "annual"
        assert result.series.status.value == "ended" and result.series.year_end == 2020
        assert (await session.scalar(select(Issue))).issue_type.value == "annual"


async def test_explicit_provider_lifecycle_is_not_replaced_by_age_inference(identity_probe_db):
    _, factory, _ = identity_probe_db
    data = bundle(numbers=("1",))
    data.series.status = "continuing"
    data.issues[0].cover_date = date(2000, 1, 1)
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, data)
        assert result.series.status.value == "continuing"


async def test_add_persists_canonical_baselines_after_lifecycle_and_format_inference(
    identity_probe_db,
):
    from pullbox.services.metadata_baselines import load_metadata_baseline

    _, factory, _ = identity_probe_db
    data = bundle(numbers=("1",))
    data.series.title = "Batman Annual"
    data.series.sort_title = None
    data.series.year_start = 2020
    data.issues[0].cover_date = date(2020, 1, 1)
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, data)
        series_id = result.series.id
        issue_id = (await session.scalar(select(Issue))).id
    async with factory() as session:
        saved = await load_metadata_baseline(session, Kind.SERIES, series_id)
        assert saved is not None
        series = await session.get(Series, series_id)
        assert saved.snapshot.values.series_type == series.series_type.value == "annual"
        assert saved.snapshot.values.status == series.status.value == "ended"
        assert saved.snapshot.values.year_end == series.year_end == 2020
        assert saved.snapshot.values.sort_title == series.sort_title
        origins = {item.field: item for item in saved.snapshot.origins}
        assert origins["status"].derivation == "lifecycle"
        assert not origins["status"].user_override and origins["status"].source is None
        assert origins["title"].source is Source.METRON_API
        issue = await load_metadata_baseline(session, Kind.ISSUE, issue_id)
        assert issue is not None and issue.snapshot.values.issue_number_text == "1"
