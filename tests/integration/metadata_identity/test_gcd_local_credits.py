"""GCD credits reach the existing canonical graph and paired XML on both databases."""

from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import delete, select

from pullbox.core.archive_metadata import ArchiveMetadataFiles, MetadataFile
from pullbox.core.metadata_identity import MetadataEntityKind as Kind
from pullbox.core.metadata_identity import MetadataSource as Source
from pullbox.core.metroninfo import parse_metroninfo
from pullbox.core.metroninfo_schema import validate_metroninfo_xml
from pullbox.models import Issue
from pullbox.models.creator import Creator, IssueCreator
from pullbox.providers.metadata.gcd_local import GcdLocalSource
from pullbox.providers.metadata.gcd_local_database import validate_candidate
from pullbox.schemas.metadata_credits import parse_credits
from pullbox.schemas.metadata_sources import SourceCapability
from pullbox.services.archive_metadata_rendering import render_archive_metadata
from pullbox.services.metadata_baselines import load_metadata_baseline
from pullbox.services.metadata_discovery import MetadataSourceRegistry, SourceRegistration
from pullbox.services.metadata_series_adoption import (
    SourceSeriesBundle,
    adopt_source_series_bundle,
)
from pullbox.services.metadata_series_refresh import refresh_series_from_sources
from pullbox.utilities.comicinfo_creators import load_comicinfo_creator_fields
from tests.integration.metadata_identity.test_series_adoption import (
    configured_sources,  # noqa: F401
)
from tests.unit.test_gcd_local_credits import EXPECTED, credited_dump
from tests.unit.test_metadata_discovery import runtime


async def source_bundle(path):
    adapter = GcdLocalSource(await validate_candidate(str(path)))
    series = (await adapter.series("2999")).data
    issues = (await adapter.issues("2999")).data
    return adapter, SourceSeriesBundle(series, tuple(issues.results), 1, issues.total)


async def test_gcd_add_populates_descriptive_relations_and_reconciled_xml(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    _, bundle = await source_bundle(credited_dump(tmp_path / "gcd.db"))
    async with factory.begin() as session:
        added = await adopt_source_series_bundle(session, bundle)
        issue_id = await session.scalar(select(Issue.id).order_by(Issue.id))
        assert (await load_comicinfo_creator_fields(session, issue_id))[
            "Penciller"
        ] == "Dave Gibbons"
        series = await load_metadata_baseline(session, Kind.SERIES, added.series.id)
        issue = await load_metadata_baseline(session, Kind.ISSUE, issue_id)
        assert issue.snapshot.values.model_dump(mode="json")["credits"] == EXPECTED
        assert all(item.comicvine_id is None for item in await session.scalars(select(Creator)))
        before_render = {item.name for item in tmp_path.iterdir()}
        rendered = render_archive_metadata(
            series.snapshot,
            issue.snapshot,
            ArchiveMetadataFiles(
                MetadataFile("ComicInfo.xml", 0), MetadataFile("MetronInfo.xml", 0)
            ),
        )
        ci = ET.fromstring(rendered.comicinfo)
        mi = parse_metroninfo(rendered.metroninfo)
        validate_metroninfo_xml(rendered.metroninfo)
        assert ci.findtext("Writer") == "Alan Moore"
        assert ci.findtext("Penciller") == "Dave Gibbons"
        assert ci.findtext("CoverArtist") == "Dave Gibbons, John Higgins"
        assert ci.findtext("Editor") == "Len Wein"
        assert (
            parse_credits(
                tuple(
                    {
                        "name": credit.creator.value,
                        "role": ", ".join(role.value for role in credit.roles),
                    }
                    for credit in mi.credits
                )
            )
            == issue.snapshot.values.credits
        )
        assert {item.name for item in tmp_path.iterdir()} == before_render


@pytest.mark.parametrize("edit", ["none", "rename", "clear"])
async def test_gcd_refresh_preserves_local_credit_edits_and_clears(
    identity_probe_db, tmp_path, edit
):
    import sqlite3

    _, factory, _ = identity_probe_db
    path = credited_dump(tmp_path / "gcd.db")
    _, bundle = await source_bundle(path)
    async with factory.begin() as session:
        result = await adopt_source_series_bundle(session, bundle)
        series_id = result.series.id
        issue_id = await session.scalar(select(Issue.id).order_by(Issue.id))
    async with factory.begin() as session:
        if edit == "rename":
            creator = await session.scalar(select(Creator).where(Creator.name == "Alan Moore"))
            assert creator is not None, "The real GCD credit must be persisted before editing it"
            creator.name = "Local Author"
        elif edit == "clear":
            await session.execute(delete(IssueCreator).where(IssueCreator.issue_id == issue_id))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE gcd_creator_name_detail SET name='Replacement' WHERE id=101")
    adapter, _ = await source_bundle(path)
    source_registry = MetadataSourceRegistry(
        [runtime(Source.GCD_LOCAL, revision=1)],
        factories={
            Source.GCD_LOCAL: SourceRegistration(
                frozenset({SourceCapability.SERIES_DETAILS, SourceCapability.ISSUE_LIST}),
                lambda _: adapter,
            )
        },
    )
    async with factory() as session:
        await refresh_series_from_sources(session, series_id, registry=source_registry)
        await session.commit()
    async with factory() as session:
        fields = await load_comicinfo_creator_fields(session, issue_id)
        assert fields.get("Writer") == (
            None if edit == "clear" else "Local Author" if edit == "rename" else "Replacement"
        )
        baseline = await load_metadata_baseline(session, Kind.ISSUE, issue_id)
        origin = next(item for item in baseline.snapshot.origins if item.field == "credits")
        assert origin.user_override == (edit != "none")
