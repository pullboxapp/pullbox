"""Real local arc reads feed the existing atomic saver on both app databases."""

import sqlite3

import pytest
from sqlalchemy import func, select

from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.models import Issue, Series, StoryArc, StoryArcExternalIdentity
from pullbox.models.metadata_identity import IssueExternalIdentity, SeriesExternalIdentity
from pullbox.models.metadata_source import MetadataSourceConfig
from pullbox.models.story_arc import IssueStoryArc
from pullbox.services.metadata_arc_catalog import fetch_source_arc_catalog
from pullbox.services.metadata_arc_commands import catalog_writer
from tests.unit.test_gcd_local_arcs import arc_dump, registry
from tests.unit.test_story_arc_catalog import _root


async def preview(path):
    return await fetch_source_arc_catalog(
        await registry(path), Source.GCD_LOCAL, "4", source_revision=1
    )


@pytest.mark.parametrize("rollback", [False, True])
async def test_gcd_arc_add_is_atomic_native_graph_not_comicvine_adoption(
    identity_probe_db, tmp_path, rollback
):
    _, factory, _ = identity_probe_db
    path = arc_dump(tmp_path / "gcd.db")
    library = tmp_path / "comics"
    library.mkdir()
    async with factory.begin() as session:
        root = await _root(session, library)
        root_id = root.id
        session.add(
            MetadataSourceConfig(
                source=Source.GCD_LOCAL.value, enabled=True, priority=4, revision=1
            )
        )
        other = Series(title="Unrelated CV series", sort_title="Other", comicvine_id=2999)
        session.add(other)
        await session.flush()
        session.add(Issue(series_id=other.id, issue_number=1, comicvine_id=10))
    snapshot = await preview(path)
    service = catalog_writer(snapshot)
    async with factory() as session:
        arc = await service.add(
            session,
            snapshot,
            ordered_issue_provider_ids=["11", "10", "15"],
            library_root_id=root_id,
        )
        arc_id = arc.id
        if rollback:
            await session.rollback()
        else:
            await session.commit()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Series)) == (
            1 if rollback else 3
        )
        assert await session.scalar(select(func.count()).select_from(Issue)) == (
            1 if rollback else 4
        )
        assert await session.scalar(select(func.count()).select_from(StoryArc)) == (
            0 if rollback else 1
        )
        for model, count in (
            (SeriesExternalIdentity, 2),
            (IssueExternalIdentity, 3),
            (StoryArcExternalIdentity, 1),
        ):
            records = list(await session.scalars(select(model)))
            assert len(records) == (0 if rollback else count)
            assert all(
                (row.source if model is StoryArcExternalIdentity else row.identity_namespace)
                == "gcd"
                for row in records
            )
        if not rollback:
            assert (await session.get(StoryArc, arc_id)).comicvine_id is None
            assert await service.find_existing(session, ["4"]) == {"4": arc_id}
            members = list(
                await session.scalars(select(IssueStoryArc).order_by(IssueStoryArc.sequence_number))
            )
            assert [row.source_issue_id for row in members] == ["11", "10", "15"]
            for member in members:
                assert (await session.get(Issue, member.issue_id)).comicvine_id is None
    assert not list(library.iterdir())


async def test_gcd_arc_refresh_requires_review_and_preserves_removed_members(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    path = arc_dump(tmp_path / "gcd.db")
    library = tmp_path / "comics"
    library.mkdir()
    async with factory.begin() as session:
        root = await _root(session, library)
        root_id = root.id
        session.add(
            MetadataSourceConfig(
                source=Source.GCD_LOCAL.value, enabled=True, priority=4, revision=1
            )
        )
    snapshot = await preview(path)
    service = catalog_writer(snapshot)
    async with factory.begin() as session:
        arc = await service.add(
            session,
            snapshot,
            ordered_issue_provider_ids=["11", "10", "15"],
            library_root_id=root_id,
        )
        arc_id, revision = arc.id, arc.revision
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM gcd_story_story_arc WHERE story_id=205")
        db.execute("INSERT INTO gcd_story VALUES (208,12,19,0)")
        db.execute("INSERT INTO gcd_story_story_arc VALUES (7,208,4)")
    updated = await preview(path)
    async with factory.begin() as session:
        delta = await service.preview_refresh(session, arc_id, updated)
        assert delta.added_issue_provider_ids == ("12",)
        assert delta.removed_issue_provider_ids == ("11",)
        result = await service.refresh(session, arc_id, updated, expected_revision=revision)
        assert len(result.added_membership_ids) == 1
        rows = list(
            await session.scalars(select(IssueStoryArc).order_by(IssueStoryArc.sequence_number))
        )
        assert [row.source_issue_id for row in rows] == ["11", "10", "15", "12"]
        assert (
            rows[-1].sync_eligible is False and rows[-1].evidence["catalog_review_required"] is True
        )
    assert not list(library.iterdir())
