"""Known ComicInfo aliases remain evidence, not permission to replace metadata."""

from xml.etree import ElementTree as ET

import pytest

from pullbox.services.archive_metadata_reconciliation import reconcile_archive_metadata
from pullbox.services.archive_metadata_rendering import (
    ArchiveMetadataRenderError,
    render_archive_metadata,
)
from tests.unit.test_archive_metadata_rendering import files, snapshots


def test_known_aliases_are_read_and_rendered_as_canonical_fields():
    incoming = files(
        "<ComicInfo><!--keep--><Issue>50-X</Issue><writer>Local Writer</writer></ComicInfo>"
    )
    original = incoming.comicinfo.payload
    evidence = reconcile_archive_metadata(incoming)
    assert evidence.issue.issue_number_text == "50-X"
    assert evidence.issue.credits[0].name == "Local Writer"
    assert not evidence.diagnostics
    series, issue = snapshots(credits=({"name": "Local Writer", "role": "writer"},))
    rendered = render_archive_metadata(series, issue, incoming)
    ci = ET.fromstring(rendered.comicinfo)
    mi = ET.fromstring(rendered.metroninfo)
    assert ci.findtext("Number").upper() == mi.findtext("Number").upper() == "50-X"
    assert ci.findtext("Writer") == mi.findtext("Credits/Credit/Creator") == "Local Writer"
    assert ci.find("Issue") is None and ci.find("writer") is None
    assert b"<!--keep-->" in rendered.comicinfo
    assert incoming.comicinfo.payload == original


@pytest.mark.parametrize(
    "xml",
    [
        "<Number>50-X</Number><Issue>50-X</Issue>",
        "<Number>50-X</Number><Issue>3</Issue>",
        "<Issue>50-X</Issue><Issue>50-X</Issue>",
        "<Writer>One</Writer><writer>Two</writer>",
        '<Issue other="value">50-X</Issue>',
        "<writer><Name>One</Name></writer>",
        "<unknown>keep me</unknown>",
        '<other:Issue xmlns:other="urn:other">50-X</other:Issue>',
    ],
)
def test_aliases_do_not_allow_ambiguous_or_unsupported_content(xml):
    with pytest.raises(ArchiveMetadataRenderError):
        render_archive_metadata(*snapshots(), files(f"<ComicInfo>{xml}</ComicInfo>"))


def test_alias_issue_number_still_blocks_the_wrong_issue():
    with pytest.raises(ArchiveMetadataRenderError, match="issue_number_conflict"):
        render_archive_metadata(*snapshots(), files("<ComicInfo><Issue>3</Issue></ComicInfo>"))


def test_alias_writer_cannot_be_silently_replaced():
    with pytest.raises(ArchiveMetadataRenderError, match="unreconciled_field"):
        render_archive_metadata(
            *snapshots(credits=({"name": "Provider Writer", "role": "writer"},)),
            files("<ComicInfo><writer>Local Writer</writer></ComicInfo>"),
        )
