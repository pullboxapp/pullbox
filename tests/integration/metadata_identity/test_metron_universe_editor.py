"""Metron universe IDs travel with exact name/designation pairs, never row positions."""

from zipfile import ZipFile

import pytest
from pydantic import ValidationError

from pullbox.schemas.issue_metadata_editor import IssueMetadataEditorReview
from pullbox.services.archive_metadata_preservation import ArchiveMetadataRenderError
from pullbox.services.issue_metadata_editor import IssueMetadataEditError
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_comicinfo_text_editor import documents
from tests.integration.metadata_identity.test_issue_metadata_editor import edit
from tests.integration.metadata_identity.test_issue_tag_editor import preview, source


def pairs(path):
    ci, mi = documents(path)
    assert ci.find("Universes") is None and ci.findtext("Number") == mi.findtext("Number") == "50-X"
    assert mi.findtext("IDS/ID") == "42" and mi.find("Series").get("id") == "12"
    return [
        (n.findtext("Name"), n.findtext("Designation"), n.get("id"))
        for n in mi.findall("Universes/Universe")
    ]


def change(*rows):
    return {
        "universes": {"source": "custom", "value": [{"name": n, "designation": d} for n, d in rows]}
    }


def test_name_only_rows_serialize_in_their_own_field_schema():
    row = {"source": "custom", "value": [{"name": "Earth"}]}
    parsed = IssueMetadataEditorReview.model_validate(
        {"edits": {"story_arcs": row, "universes": row}}
    )
    actions = parsed.model_dump()["edits"]
    assert list(actions["universes"]["value"]) == [{"name": "Earth", "designation": None}]
    assert list(actions["story_arcs"]["value"]) == [{"name": "Earth", "position": None}]
    for field, rows in (
        ("universes", [{"name": "Earth", "position": 1}]),
        ("story_arcs", [{"name": "Earth", "designation": "One"}]),
        ("title", [{"name": "Earth", "designation": "One"}]),
        ("universes", [{"name": "Earth"}, {"name": "earth"}]),
        ("universes", [{"name": "Earth", "designation": "Line\nbreak"}]),
        ("universes", [{"name": "Earth", "designation": "a" * 4097}]),
        ("universes", [{"name": str(i)} for i in range(129)]),
    ):
        with pytest.raises(ValidationError):
            IssueMetadataEditorReview.model_validate(
                {"edits": {field: {"source": "custom", "value": rows}}}
            )


async def test_universes_keep_exact_pairs_not_just_names(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        xml = (
            '<Universes><Universe id="a"><Name>Marvel</Name>'
            "<Designation>Earth-616</Designation></Universe>"
            '<Universe id="b"><Name>Marvel</Name><Designation>Earth-1610</Designation></Universe>'
            '<Universe id="c"><Name>Old, Earth</Name></Universe></Universes>'
        )
        await source(factory, plan, path, "", "", mi_extra=xml)
        before = path.read_bytes()
        result = await preview(factory, issue)
        rows = {r.key: r for r in result.editor_fields}
        assert result.ready and "issue.universes" in rows
        row = rows["issue.universes"]
        assert row.editable and row.differs and row.input_type == "universes"
        assert [(e.name, e.designation) for e in row.universe_draft] == [
            ("Marvel", "Earth-616"),
            ("Marvel", "Earth-1610"),
            ("Old, Earth", None),
        ]
        assert {o.source for o in row.options} == {"library", "MetronInfo.xml"}
        assert (await preview(factory, issue, {"universes": {"source": "MetronInfo.xml"}})).ready
        assert path.read_bytes() == before
        await edit(factory, issue, {"universes": {"source": "MetronInfo.xml"}})
        changes = change(
            ("Old, Earth", None), ("Marvel", "Earth-1610"), ("Marvel", "Earth-65"), ("New", None)
        )
        await edit(factory, issue, changes)
        assert pairs(path) == [
            ("Old, Earth", None, "c"),
            ("Marvel", "Earth-1610", "b"),
            ("Marvel", "Earth-65", None),
            ("New", None, None),
        ]
        saved = await preview(factory, issue)
        row = next(r for r in saved.editor_fields if r.key == "issue.universes")
        assert saved.ready and row.overridden and not row.differs
        await source(factory, plan, path, "", "", mi_extra=xml)
        drift = await preview(factory, issue)
        assert not drift.ready and any(c.key == "xml.Universes" for c in drift.conflicts)
        await edit(factory, issue, {"universes": {"source": "clear"}})
        assert documents(path)[1].find("Universes") is None
        sig = path.read_bytes(), path.stat().st_ino
        assert await edit(factory, issue, {"universes": {"source": "reset"}}) == "override_released"
        assert (path.read_bytes(), path.stat().st_ino) == sig


async def test_universe_ambiguity_and_annotations_preserve_other_edits(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        xml = (
            '<Universes><Universe id="a"><Name>Earth</Name></Universe>'
            '<Universe id="b"><Name>Earth</Name></Universe></Universes>'
        )
        await source(factory, plan, path, "", "", mi_extra=xml)
        before = path.read_bytes()
        report = await preview(factory, issue)
        rows = {r.key: r for r in report.editor_fields}
        assert report.ready and "issue.universes" in rows
        assert not next(
            o for o in rows["issue.universes"].options if o.source == "MetronInfo.xml"
        ).selectable
        with pytest.raises(ArchiveMetadataRenderError, match="ambiguous_list_id"):
            await preview(factory, issue, change(("Earth", None)))
        with pytest.raises(IssueMetadataEditError):
            await preview(factory, issue, {"universes": {"source": "ComicInfo.xml"}})
        with pytest.raises(ValidationError):
            IssueMetadataEditorReview.model_validate(
                {
                    "edits": {
                        "universes": {
                            "source": "custom",
                            "value": [{"name": "Earth", "designation": None, "id": "a"}],
                        }
                    }
                }
            )
        assert path.read_bytes() == before
        await edit(factory, issue, change(("Earth", "New")))
        assert pairs(path) == [("Earth", "New", None)]
        await edit(factory, issue, {"universes": {"source": "reset"}})
        xml = (
            '<Universes><Universe id="a"><Name>Earth</Name>'
            "<Designation>A<!--note-->B</Designation></Universe></Universes>"
        )
        await source(factory, plan, path, "", "", mi_extra=xml)
        report = await preview(factory, issue)
        row = next(r for r in report.editor_fields if r.key == "issue.universes")
        assert report.ready and not row.editable and "annotation" in row.reason
        await edit(factory, issue, {"collection_title": {"source": "custom", "value": "Edition"}})
        with ZipFile(path) as archive:
            assert xml.encode() in archive.read("MetronInfo.xml")
