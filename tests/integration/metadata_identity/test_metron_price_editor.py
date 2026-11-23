"""Metron cover prices are ordered country/decimal pairs, not currency conversions."""

from uuid import uuid4
from zipfile import ZipFile

import pytest
from pydantic import ValidationError

from pullbox.schemas.issue_metadata_editor import IssueMetadataEditorReview
from pullbox.services.archive_metadata_preservation import ArchiveMetadataRenderError
from pullbox.services.archive_metadata_publication import ArchivePublicationError
from pullbox.services.issue_file_metadata import (
    file_metadata_error,
    prepare_file_metadata,
    repairable_metadata_field_error,
    write_file_metadata,
)
from pullbox.services.issue_metadata_editor import IssueMetadataEditError
from tests.integration.metadata_identity.test_archive_metadata_publication import prepared
from tests.integration.metadata_identity.test_comicinfo_text_editor import documents
from tests.integration.metadata_identity.test_issue_metadata_editor import (
    edit,
    no_control,
    no_progress,
)
from tests.integration.metadata_identity.test_issue_tag_editor import preview, source


def prices(path):
    ci, mi = documents(path)
    assert ci.find("Prices") is None and mi.findtext("Number") == "50-X"
    assert mi.findtext("IDS/ID") == "42"
    with ZipFile(path) as archive:
        assert archive.read("page.jpg") == b"page bytes"
    return [(n.get("country"), n.text) for n in mi.findall("Prices/Price")]


async def test_edit_repeated_countries_exact_amounts_and_detect_drift(identity_probe_db, tmp_path):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        xml = (
            '<Prices><Price country="US">3.990</Price><Price country="CA">0</Price>'
            '<Price country="US"> +.50 </Price></Prices>'
        )
        await source(factory, plan, path, "", "", mi_extra=xml)
        before = path.read_bytes()
        result = await preview(factory, issue)
        assert result.ready and "issue.prices" in {r.key for r in result.editor_fields}
        row = next(r for r in result.editor_fields if r.key == "issue.prices")
        assert row.editable and [(e.country, e.amount) for e in row.price_draft] == [
            ("US", "3.990"),
            ("CA", "0"),
            ("US", "+.50"),
        ]
        assert [o.source for o in row.options] == ["library", "MetronInfo.xml"]
        custom = {
            "prices": {
                "source": "custom",
                "value": [
                    {"country": "US", "amount": "+.50"},
                    {"country": "CA", "amount": "0.000000000000000001"},
                    {"country": "GB", "amount": "0"},
                    {"country": "GB", "amount": "0"},
                ],
            }
        }
        assert (await preview(factory, issue, custom)).ready and path.read_bytes() == before
        await edit(factory, issue, custom)
        assert prices(path) == [
            ("US", " +.50 "),
            ("CA", "0.000000000000000001"),
            ("GB", "0"),
            ("GB", "0"),
        ]
        row = next(
            r for r in (await preview(factory, issue)).editor_fields if r.key == "issue.prices"
        )
        assert row.overridden and not row.differs
        await source(factory, plan, path, "", "", mi_extra=xml)
        drift = await preview(factory, issue)
        assert not drift.ready and {c.key for c in drift.conflicts} == {"xml.Prices"}
        await edit(factory, issue, {"prices": {"source": "MetronInfo.xml"}})
        assert prices(path) == [("US", "3.990"), ("CA", "0"), ("US", " +.50 ")]
        await edit(factory, issue, {"prices": {"source": "clear"}})
        assert prices(path) == [] and documents(path)[1].find("Prices") is None
        unchanged = path.read_bytes(), path.stat().st_mtime_ns
        assert await edit(factory, issue, {"prices": {"source": "reset"}}) == "override_released"
        assert (path.read_bytes(), path.stat().st_mtime_ns) == unchanged


async def test_annotated_price_is_read_only_without_blocking_other_fields(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        xml = '<Prices><Price country="US">3.99<!--first printing--></Price></Prices>'
        await source(factory, plan, path, "", "", mi_extra=xml)
        result = await preview(factory, issue)
        assert result.ready and "issue.prices" in {r.key for r in result.editor_fields}
        row = next(r for r in result.editor_fields if r.key == "issue.prices")
        assert not row.editable and "annotation" in row.reason
        with pytest.raises(IssueMetadataEditError, match="read-only"):
            await preview(factory, issue, {"prices": {"source": "clear"}})
        await edit(
            factory, issue, {"notes": {"source": "custom", "value": "Preserve the first printing"}}
        )
        with ZipFile(path) as archive:
            assert xml.encode() in archive.read("MetronInfo.xml")


def test_editor_accepts_only_explicit_price_pairs_without_currency_or_float_coercion():
    def review(value):
        return IssueMetadataEditorReview.model_validate(
            {"edits": {"prices": {"source": "custom", "value": value}}}
        )

    valid = {"country": "US", "amount": "003.990"}
    assert review([valid]).model_dump(mode="json")["edits"]["prices"]["value"] == [valid]
    for invalid in [
        "US: 3.99",
        [],
        [dict(valid, currency="USD")],
        [dict(valid, country="USD")],
        [dict(valid, country="us")],
        [dict(valid, amount=3.99)],
        [dict(valid, amount="1E2")],
        [dict(valid, amount="NaN")],
        [dict(valid, amount="3,99")],
        [dict(valid, amount="1\n")],
        [valid] * 129,
        [{"name": "US"}],
    ]:
        with pytest.raises(ValidationError):
            review(invalid)


async def test_old_clear_approval_cannot_remove_a_price_after_its_country_changes(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        xml = '<Prices><Price country="US">3.99</Price></Prices>'
        await source(factory, plan, path, "", "", mi_extra=xml)
        corrections = {"prices": {"source": "clear"}}
        approved = await preview(factory, issue, corrections)
        assert approved.ready
        await source(factory, plan, path, "", "", mi_extra=xml.replace('"US"', '"CA"'))
        unchanged = path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns
        with pytest.raises(ArchivePublicationError, match="approval_changed"):
            await write_file_metadata(
                factory,
                issue,
                approved.review_key,
                uuid4(),
                limit=1000000,
                check_control=no_control,
                progress=no_progress,
                editor=IssueMetadataEditorReview.model_validate({"edits": corrections}),
            )
        assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == unchanged
        assert prices(path) == [("CA", "3.99")]


async def test_invalid_price_can_be_corrected_without_approving_other_invalid_xml(
    identity_probe_db, tmp_path
):
    _, factory, _ = identity_probe_db
    async with prepared(factory, tmp_path) as (path, _, plan):
        issue = plan.target.binding.metadata.issues[0].local_id
        invalid = '<Prices><Price country="US">$4,99</Price></Prices>'
        await source(factory, plan, path, "", "", mi_extra=invalid)
        before = path.read_bytes()
        async with factory() as session:
            with pytest.raises(ArchiveMetadataRenderError) as error:
                await prepare_file_metadata(session, issue)
        assert repairable_metadata_field_error(error.value)
        assert "select Cover prices" in file_metadata_error(error.value)
        review = await preview(factory, issue)
        assert not review.ready and {c.key for c in review.conflicts} == {"xml.Prices"}
        row = next(r for r in review.editor_fields if r.key == "issue.prices")
        assert row.editable and not row.options[-1].selectable
        assert path.read_bytes() == before
        correction = {
            "prices": {"source": "custom", "value": [{"country": "US", "amount": "4.99"}]}
        }
        await edit(factory, issue, correction)
        assert prices(path) == [("US", "4.99")]
        await source(factory, plan, path, "", "", mi_extra=invalid + "<UnknownExtension/>")
        before = path.read_bytes()
        with pytest.raises(ArchiveMetadataRenderError, match="invalid_input"):
            await preview(factory, issue, correction)
        assert path.read_bytes() == before
