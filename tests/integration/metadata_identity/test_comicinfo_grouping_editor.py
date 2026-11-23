"""ComicInfo grouping describes this issue, not ownership, character IDs or tracked series."""

import pytest
from sqlalchemy import select

from pullbox.models import Issue, IssueExternalIdentity, LibraryFile, Series, SeriesExternalIdentity
from pullbox.services.issue_metadata_editor import IssueMetadataEditError
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_comicinfo_text_editor import documents
from tests.integration.metadata_identity.test_issue_metadata_editor import edit
from tests.integration.metadata_identity.test_issue_tag_editor import preview, source


async def test_grouping_is_issue_only_and_keeps_series_and_resource_ids(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra="<MainCharacterOrTeam>Robin, the Boy Wonder</MainCharacterOrTeam>"
            "<SeriesGroup>Gotham, Elseworlds</SeriesGroup>"
            "<Characters>Alfred</Characters><Teams>Outsiders</Teams>",
            mi_extra='<Characters><Character id="8">Alfred</Character></Characters>'
            '<Teams><Team id="9">Outsiders</Team></Teams>'
            "<CollectionTitle>Collected edition</CollectionTitle>",
        )

        async def bindings():
            async with factory() as session:
                return [
                    list((await session.execute(query)).all())
                    for query in (
                        select(Issue.id, Issue.series_id, Issue.issue_number_text),
                        select(Series.id, Series.title),
                        select(
                            SeriesExternalIdentity.series_id, SeriesExternalIdentity.external_id
                        ),
                        select(IssueExternalIdentity.issue_id, IssueExternalIdentity.external_id),
                        select(LibraryFile.id, LibraryFile.file_path, LibraryFile.issue_id),
                    )
                ]

        before = path.read_bytes(), await bindings()
        report = await preview(factory, issue)
        rows = {r.key: r for r in report.editor_fields}
        assert {"issue.main_character_or_team", "issue.series_groups"} <= rows.keys()
        for field, value, widget in (
            ("issue.main_character_or_team", "Robin, the Boy Wonder", "text"),
            ("issue.series_groups", "Gotham\nElseworlds", "textarea"),
        ):
            row = rows[field]
            assert row.editable and row.differs and row.input_type == widget
            assert {o.source: o.value for o in row.options} == {
                "library": "",
                "ComicInfo.xml": value,
            }
        changes = {
            "main_character_or_team": {"source": "ComicInfo.xml"},
            "series_groups": {"source": "custom", "value": "Elseworlds\nGotham & Beyond"},
        }
        assert (await preview(factory, issue, changes)).ready
        assert (path.read_bytes(), await bindings()) == before
        await edit(factory, issue, changes)
        ci, mi = documents(path)
        assert ci.findtext("MainCharacterOrTeam") == "Robin, the Boy Wonder"
        assert ci.findtext("SeriesGroup") == "Elseworlds, Gotham & Beyond"
        assert (ci.findtext("Characters"), ci.findtext("Teams")) == ("Alfred", "Outsiders")
        assert mi.findtext("CollectionTitle") == "Collected edition"
        for tag, name, ident in (
            ("Characters/Character", "Alfred", "8"),
            ("Teams/Team", "Outsiders", "9"),
        ):
            assert mi.findtext(tag) == name and mi.find(tag).get("id") == ident
        assert mi.find("SeriesGroup") is mi.find("MainCharacterOrTeam") is None
        assert await bindings() == before[1]
        saved = await preview(factory, issue)
        assert saved.ready and all(
            r.overridden and not r.differs
            for r in saved.editor_fields
            if r.key in {"issue.main_character_or_team", "issue.series_groups"}
        )
        await edit(factory, issue, {"main_character_or_team": {"source": "clear"}})
        assert documents(path)[0].find("MainCharacterOrTeam") is None
        assert documents(path)[0].findtext("SeriesGroup") == "Elseworlds, Gotham & Beyond"
        sig = path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino
        assert (
            await edit(factory, issue, {"series_groups": {"source": "reset"}})
            == "override_released"
        )
        assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) == sig


async def test_bad_group_labels_are_preserved_without_approving_them(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await source(factory, plan, path, "", "", ci_extra="<SeriesGroup>A,,a</SeriesGroup>")
        original = path.read_bytes()
        report = await preview(factory, issue)
        rows = {r.key: r for r in report.editor_fields}
        assert report.ready and "issue.series_groups" in rows
        assert any(
            o.source == "ComicInfo.xml" and not o.selectable
            for o in rows["issue.series_groups"].options
        )
        for field, action in (
            ("series_groups", {"source": "ComicInfo.xml"}),
            ("main_character_or_team", {"source": "MetronInfo.xml"}),
            ("main_character_or_team", {"source": "custom", "value": "One\nTwo"}),
            ("series_groups", {"source": "custom", "value": "A\na"}),
            ("series_groups", {"source": "custom", "value": "A,B"}),
            ("series_groups", {"source": "custom", "value": "Group\n[CVDB123]"}),
        ):
            with pytest.raises(IssueMetadataEditError):
                await preview(factory, issue, {field: action})
        assert path.read_bytes() == original
        await edit(
            factory,
            issue,
            {"main_character_or_team": {"source": "custom", "value": "The Outsiders"}},
        )
        assert documents(path)[0].findtext("SeriesGroup") == "A,,a"
        await edit(factory, issue, {"series_groups": {"source": "custom", "value": "Gotham"}})
        await source(
            factory,
            plan,
            path,
            "",
            "",
            ci_extra="<MainCharacterOrTeam>The Outsiders</MainCharacterOrTeam>"
            "<SeriesGroup>Elsewhere</SeriesGroup>",
        )
        pending = await preview(factory, issue)
        assert not pending.ready and any(c.key == "xml.SeriesGroup" for c in pending.conflicts)
        await edit(factory, issue, {"series_groups": {"source": "ComicInfo.xml"}})
        assert (await preview(factory, issue)).ready
