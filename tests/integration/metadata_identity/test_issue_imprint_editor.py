"""An issue imprint is not a new publisher, a series edit, or a reusable resource ID."""

from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from pullbox.models import Issue, Publisher, Series
from pullbox.services.archive_metadata_preservation import ArchiveMetadataRenderError
from pullbox.services.issue_file_metadata import file_metadata_error
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_issue_metadata_editor import edit
from tests.integration.metadata_identity.test_issue_tag_editor import preview, source


async def test_issue_imprint_keeps_publisher_and_does_not_transfer_imprint_id(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        binding = plan.target.binding.metadata
        issue = binding.issues[0].local_id
        async with factory.begin() as session:
            publisher = Publisher(name="Example Press")
            session.add(publisher)
            await session.flush()
            publisher_id = publisher.id
            series = await session.get(Series, binding.series.local_id)
            series.publisher_id = publisher_id
            sibling = Issue(series_id=series.id, issue_number=51, title="Sibling")
            session.add(sibling)
            await session.flush()
            sibling_id = sibling.id

        async def replace(ci, mi):
            await source(
                factory,
                plan,
                path,
                "",
                "",
                ci_extra=f"<Publisher>Example Press</Publisher><Imprint>{ci}</Imprint>",
                mi_extra='<Publisher id="8"><Name>Example Press</Name>'
                f'<Imprint id="9">{mi}</Imprint></Publisher>',
            )

        def check(label, imprint_id):
            with ZipFile(path) as archive:
                ci = ET.fromstring(archive.read("ComicInfo.xml"))
                mi = ET.fromstring(archive.read("MetronInfo.xml"))
                assert ci.findtext("Publisher") == mi.findtext("Publisher/Name") == "Example Press"
                assert mi.find("Publisher").attrib == {"id": "8"}
                assert ci.findtext("Imprint") == mi.findtext("Publisher/Imprint") == label
                node = mi.find("Publisher/Imprint")
                assert (node.get("id") if node is not None else None) == imprint_id
                assert archive.read("page.jpg") == b"page bytes"

        await replace("Old", "Legacy")
        before = path.read_bytes()
        initial = await preview(factory, issue)
        field = next(f for f in initial.editor_fields if f.label == "Imprint")
        assert field.editable and not initial.ready
        assert {o.source: o.value for o in field.options if o.value} == {
            "ComicInfo.xml": "Old",
            "MetronInfo.xml": "Legacy",
        }
        keep = {"imprint": {"source": "MetronInfo.xml"}}
        assert (await preview(factory, issue, keep)).ready and path.read_bytes() == before
        await edit(factory, issue, keep)
        check("Legacy", "9")
        await edit(factory, issue, {"imprint": {"source": "custom", "value": "New Line"}})
        check("New Line", None)
        stored = next(
            f for f in (await preview(factory, issue)).editor_fields if f.key == "issue.imprint"
        )
        assert stored.current == "New Line" and stored.overridden and not stored.differs
        await replace("External", "External")
        before = path.read_bytes()
        drift = await preview(factory, issue, {"title": {"source": "custom", "value": "Unrelated"}})
        assert not drift.ready and any(c.key == "xml.Imprint" for c in drift.conflicts)
        assert path.read_bytes() == before
        await edit(factory, issue, {"imprint": {"source": "clear"}})
        check(None, None)
        signature = path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns
        assert await edit(factory, issue, {"imprint": {"source": "reset"}}) == "override_released"
        assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == signature
        async with factory() as session:
            assert (await session.get(Series, binding.series.local_id)).publisher_id == publisher_id
            assert (await session.get(Issue, sibling_id)).title == "Sibling"


async def test_issue_without_publisher_can_clear_or_return_to_series_not_invent_parent(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        await source(factory, plan, path, "", "", ci_extra="<Imprint>Legacy</Imprint>")
        before = path.read_bytes()
        report = await preview(factory, issue)
        assert not report.ready
        note = next(c.note for c in report.conflicts if c.key == "xml.Imprint")
        assert "publisher" in note and "clear" in note.lower()
        field = next(f for f in report.editor_fields if f.key == "issue.imprint")
        assert field.differs and "publisher" in field.reason and "Clear value" in field.reason
        with pytest.raises(ArchiveMetadataRenderError) as error:
            await preview(factory, issue, {"imprint": {"source": "custom", "value": "Another"}})
        assert "publisher" in file_metadata_error(error.value)
        assert path.read_bytes() == before
        await edit(factory, issue, {"imprint": {"source": "clear"}})
        with ZipFile(path) as archive:
            for member in ("ComicInfo.xml", "MetronInfo.xml"):
                root = ET.fromstring(archive.read(member))
                assert root.find("Publisher") is None and root.find(".//Imprint") is None
