"""Explicit community rating corrections never invent, round, or reattribute votes."""

from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from pullbox.schemas.issue_metadata_editor import IssueMetadataEditorReview
from pullbox.services.archive_metadata_publication import ArchivePublicationError
from pullbox.services.issue_file_metadata import write_file_metadata
from pullbox.services.issue_metadata_editor import IssueMetadataEditError
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_issue_metadata_editor import (
    edit,
    no_control,
    no_progress,
)
from tests.integration.metadata_identity.test_issue_tag_editor import preview, source


async def rated(factory, plan, path, ci="3", mi="4.500", extra="", count=12):
    await source(
        factory,
        plan,
        path,
        "",
        "",
        ci_extra=f"<CommunityRating>{ci}</CommunityRating>",
        mi_extra=f"<CommunityRating><AverageRating>{mi}</AverageRating>"
        f"<RatingCount>{count}</RatingCount>{extra}</CommunityRating>",
    )


def values(path):
    with ZipFile(path) as archive:
        assert archive.read("page.jpg") == b"page bytes"
        ci = ET.fromstring(archive.read("ComicInfo.xml"))
        mi = ET.fromstring(archive.read("MetronInfo.xml"))
        return (
            ci.findtext("CommunityRating"),
            mi.findtext("CommunityRating/AverageRating"),
            mi.findtext("CommunityRating/RatingCount"),
        )


def pick(source, value=None):
    return {
        "community_rating": {"source": source, **({"value": value} if value is not None else {})}
    }


async def test_zero_clear_and_vote_preservation(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await rated(factory, plan, path)
        before = path.read_bytes(), path.stat().st_mtime_ns
        report = await preview(factory, issue)
        field = next((f for f in report.editor_fields if f.key == "issue.community_rating"), None)
        assert field and field.editable and field.differs and "12" in field.reason
        assert report.ready and field.current == ""
        selected = await preview(factory, issue, pick("MetronInfo.xml"))
        assert selected.ready and any(
            "keeps the count of 12 votes" in f.reason for f in selected.editor_fields
        )
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        await edit(factory, issue, pick("MetronInfo.xml"))
        assert values(path) == ("4.5", "4.500", "12")
        field = next(
            f
            for f in (await preview(factory, issue)).editor_fields
            if f.key == "issue.community_rating"
        )
        assert field.current == "4.5" and field.overridden and not field.differs
        changed = await preview(factory, issue, pick("custom", "0"))
        assert any("removes the count of 12 votes" in f.reason for f in changed.editor_fields)
        assert changed.ready and any(
            "RatingCount" in c.field and c.before == "12" and not c.after for c in changed.changes
        )
        await edit(factory, issue, pick("custom", "0"))
        assert values(path) == ("0", "0", None)
        await edit(factory, issue, pick("clear"))
        assert values(path) == (None, None, None)
        before = path.read_bytes(), path.stat().st_mtime_ns
        assert await edit(factory, issue, pick("reset")) == "override_released"
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before


async def test_precise_score_is_preserved_until_explicit_correction(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await rated(factory, plan, path, mi="4.555")
        original = path.read_bytes()
        report = await preview(factory, issue)
        row = next(f for f in report.editor_fields if f.key == "issue.community_rating")
        assert report.ready and any(o.value == "4.555" and not o.selectable for o in row.options)
        for action in (
            pick("MetronInfo.xml"),
            *(
                pick("custom", v)
                for v in (
                    "5.01",
                    "4.555",
                    "4e0",
                    "NaN",
                    "-1",
                    "4,5",
                    "4.00000000000000000000000000001",
                )
            ),
        ):
            with pytest.raises(IssueMetadataEditError, match="0 to 5"):
                await preview(factory, issue, action)
        assert path.read_bytes() == original
        await edit(factory, issue, {"title": {"source": "custom", "value": "Reviewed"}})
        assert values(path) == ("3", "4.555", "12")
        await edit(factory, issue, pick("ComicInfo.xml"))
        assert values(path) == ("3", "3", None)
        await rated(factory, plan, path, ci="3", mi="4.555")
        drift = await preview(factory, issue)
        assert not drift.ready and any(c.key == "xml.CommunityRating" for c in drift.conflicts)


async def test_extended_vote_data_blocks_only_rating_edits(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await rated(factory, plan, path, extra="<!--source annotation-->")
        report = await preview(factory, issue)
        field = next(f for f in report.editor_fields if f.key == "issue.community_rating")
        assert report.ready and not field.editable
        with pytest.raises(IssueMetadataEditError, match="rating"):
            await preview(factory, issue, pick("clear"))
        await edit(factory, issue, {"title": {"source": "custom", "value": "Reviewed"}})
        with ZipFile(path) as archive:
            assert b"<!--source annotation-->" in archive.read("MetronInfo.xml")


async def test_changed_vote_count_invalidates_approval(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        # The first 128 characters look representable; the whole source value is not.
        await rated(factory, plan, path, ci="4." + "0" * 140 + "1")
        with pytest.raises(IssueMetadataEditError, match="0 to 5"):
            await preview(factory, issue, pick("ComicInfo.xml"))
        choice = pick("custom", "+.7500")
        before = await preview(factory, issue, choice)
        assert before.ready
        await rated(factory, plan, path, ci="4." + "0" * 140 + "1", count=13)
        changed = path.read_bytes()
        with pytest.raises(ArchivePublicationError, match="approval_changed"):
            await write_file_metadata(
                factory,
                issue,
                before.review_key,
                uuid4(),
                limit=10**6,
                check_control=no_control,
                progress=no_progress,
                editor=IssueMetadataEditorReview.model_validate({"edits": choice}),
            )
        assert path.read_bytes() == changed
        await edit(factory, issue, choice)
        assert values(path) == ("0.75", "0.75", None)
