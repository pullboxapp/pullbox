"""Shared archive fields must be actionable before any paired write."""

from xml.etree import ElementTree as ET

import pytest

from pullbox.services.archive_metadata_preservation import review_preserved_fields
from pullbox.services.archive_metadata_rendering import (
    ArchiveMetadataRenderError,
    render_archive_metadata,
)
from tests.unit.test_archive_metadata_rendering import files, metron, snapshots


@pytest.mark.parametrize("left,right", [("Teen", "Unknown"), ("Unknown", "Teen")])
def test_unspecified_rating_does_not_conflict_with_known_rating(left, right):
    incoming = files(
        f"<ComicInfo><AgeRating>{left}</AgeRating></ComicInfo>",
        metron(f"<AgeRating>{right}</AgeRating>"),
    )
    try:
        pair = render_archive_metadata(*snapshots(), incoming)
    except ArchiveMetadataRenderError as exc:
        pytest.fail(f"An unspecified rating blocked the known rating: {exc.code}/{exc.field}")
    assert ET.fromstring(pair.comicinfo).findtext("AgeRating") == "Teen"
    assert ET.fromstring(pair.metroninfo).findtext("AgeRating") == "Teen"


SHARED_CASES = [
    ("AgeRating", "<AgeRating>Teen</AgeRating>", "<AgeRating>Mature</AgeRating>"),
    ("Genre", "<Genre>Action</Genre>", "<Genres><Genre>Adventure</Genre></Genres>"),
    ("Tags", "<Tags>One</Tags>", "<Tags><Tag>Two</Tag></Tags>"),
    (
        "Characters",
        "<Characters>One</Characters>",
        "<Characters><Character>Two</Character></Characters>",
    ),
    ("Teams", "<Teams>One</Teams>", "<Teams><Team>Two</Team></Teams>"),
    ("Locations", "<Locations>One</Locations>", "<Locations><Location>Two</Location></Locations>"),
    (
        "Imprint",
        "<Imprint>One</Imprint>",
        "<Publisher><Name>Publisher</Name><Imprint>Two</Imprint></Publisher>",
    ),
    (
        "StoryArc",
        "<StoryArc>One</StoryArc><StoryArcNumber>2</StoryArcNumber>",
        "<Arcs><Arc><Name>Two</Name><Number>3</Number></Arc></Arcs>",
    ),
    (
        "StoryArc",
        "<StoryArc>One</StoryArc><StoryArcNumber>2</StoryArcNumber>",
        "<Arcs><Arc><Name>One</Name><Number>3</Number></Arc></Arcs>",
    ),
]


@pytest.mark.parametrize("field,ci,mi", SHARED_CASES)
def test_every_shared_disagreement_is_offered_for_explicit_review(field, ci, mi):
    incoming = files(f"<ComicInfo>{ci}</ComicInfo>", metron(mi))
    conflicts = review_preserved_fields(incoming, {})
    assert len(conflicts) == 1, f"Hidden shared XML conflict: {field}"
    conflict = conflicts[0]
    assert conflict.key == f"xml.{field}"
    assert conflict.selected is None
    assert {o.source for o in conflict.options} == {"ComicInfo.xml", "MetronInfo.xml"}
    assert all(o.selectable for o in conflict.options)


def test_shared_conflict_error_names_the_actual_xml_field():
    from pullbox.services.issue_file_metadata import file_metadata_error

    message = file_metadata_error(ArchiveMetadataRenderError("unreconciled_field", "AgeRating"))
    assert "age rating" in message.lower()
    assert "ComicInfo.xml" in message and "MetronInfo.xml" in message


@pytest.mark.parametrize("code", ["unsupported_content", "resource_metadata_change"])
def test_credit_render_error_explains_the_field_and_preserved_file(code):
    from pullbox.services.issue_file_metadata import file_metadata_error

    message = file_metadata_error(ArchiveMetadataRenderError(code, "credits"))
    assert "creator credits" in message.lower()
    assert "unchanged" in message.lower()
    assert "preview" in message.lower()
    assert "provider matches" not in message


def test_final_verification_error_does_not_blame_provider_matches():
    from pullbox.services.archive_metadata_publication import ArchivePublicationError
    from pullbox.services.issue_file_metadata import file_metadata_error

    message = file_metadata_error(ArchivePublicationError("snapshot_disagrees"))
    assert "final verification" in message.lower()
    assert "unchanged" in message.lower()
    assert "download" in message.lower()
    assert "provider matches" not in message


@pytest.mark.parametrize("field,ci,mi", SHARED_CASES)
@pytest.mark.parametrize("source", ["ComicInfo.xml", "MetronInfo.xml"])
def test_each_choice_renders_an_agreeing_pair_and_round_trips(field, ci, mi, source):
    incoming = files(f"<ComicInfo>{ci}</ComicInfo>", metron(mi))
    choices = {f"xml.{field}": source}
    reviewed = review_preserved_fields(incoming, choices)
    assert reviewed[0].selected == source
    output = render_archive_metadata(*snapshots(), incoming, shared_choices=choices)
    rendered = files(output.comicinfo.decode(), output.metroninfo.decode())
    assert review_preserved_fields(rendered, {}) == []
    assert render_archive_metadata(*snapshots(), rendered) == output
    chosen = ET.fromstring(
        incoming.comicinfo.payload if source == "ComicInfo.xml" else incoming.metroninfo.payload
    )
    actual = ET.fromstring(output.comicinfo if source == "ComicInfo.xml" else output.metroninfo)
    path = (
        {"Genre": "Genres", "StoryArc": "Arcs", "Imprint": "Publisher/Imprint"}.get(field, field)
        if source == "MetronInfo.xml"
        else field
    )
    assert ET.tostring(actual.find(path)) == ET.tostring(chosen.find(path))


@pytest.mark.parametrize("choices", [{"xml.Tags": "ComicInfo.xml"}, {"xml.AgeRating": "library"}])
def test_stale_or_unoffered_choices_are_rejected(choices):
    incoming = files(
        "<ComicInfo><AgeRating>Teen</AgeRating></ComicInfo>",
        metron("<AgeRating>Mature</AgeRating>"),
    )
    with pytest.raises(ArchiveMetadataRenderError, match=r"choices_changed|invalid_choice"):
        render_archive_metadata(*snapshots(), incoming, shared_choices=choices)


def test_xml_choice_cannot_override_issue_identity():
    incoming = files(
        "<ComicInfo><Number>99</Number><AgeRating>Teen</AgeRating></ComicInfo>",
        metron("<AgeRating>Mature</AgeRating>"),
    )
    with pytest.raises(ArchiveMetadataRenderError, match="issue_number_conflict"):
        render_archive_metadata(
            *snapshots(), incoming, shared_choices={"xml.AgeRating": "ComicInfo.xml"}
        )


def test_selected_names_never_inherit_an_unrelated_resource_id():
    incoming = files(
        "<ComicInfo><Characters>Retained, New</Characters><StoryArc>New arc</StoryArc>"
        "<StoryArcNumber>5</StoryArcNumber><Imprint>New imprint</Imprint></ComicInfo>",
        metron(
            '<IDS><ID source="Comic Vine" primary="true">7</ID></IDS>'
            '<Characters><Character id="1">Retained</Character>'
            '<Character id="2">Old</Character></Characters>'
            '<Arcs><Arc id="3"><Name>Old arc</Name><Number>2</Number></Arc></Arcs>'
            '<Publisher><Name>Publisher</Name><Imprint id="4">Old imprint</Imprint></Publisher>'
        ),
    )
    choices = {key: "ComicInfo.xml" for key in ("xml.Characters", "xml.StoryArc", "xml.Imprint")}
    output = render_archive_metadata(*snapshots(), incoming, shared_choices=choices)
    mi = ET.fromstring(output.metroninfo)
    chars = mi.findall("Characters/Character")
    assert [(n.text, n.get("id")) for n in chars] == [("Retained", "1"), ("New", None)]
    assert mi.find("Arcs/Arc").get("id") is None
    assert mi.findtext("Arcs/Arc/Number") == "5"
    assert mi.find("Publisher/Imprint").get("id") is None


def test_multiple_arc_names_and_positions_are_chosen_as_one_value():
    incoming = files(
        "<ComicInfo><StoryArc>First, Second</StoryArc>"
        "<StoryArcNumber>2, 5</StoryArcNumber></ComicInfo>",
        metron("<Arcs><Arc><Name>Other</Name><Number>8</Number></Arc></Arcs>"),
    )
    for source, expected in (
        ("ComicInfo.xml", [("First", "2"), ("Second", "5")]),
        ("MetronInfo.xml", [("Other", "8")]),
    ):
        pair = render_archive_metadata(
            *snapshots(), incoming, shared_choices={"xml.StoryArc": source}
        )
        ci, mi = ET.fromstring(pair.comicinfo), ET.fromstring(pair.metroninfo)
        assert ci.findtext("StoryArc").split(", ") == [n for n, _ in expected]
        assert ci.findtext("StoryArcNumber").split(", ") == [p for _, p in expected]
        assert [
            (a.findtext("Name"), a.findtext("Number")) for a in mi.findall("Arcs/Arc")
        ] == expected


def test_rating_option_must_be_supported_by_both_output_formats():
    incoming = files(
        "<ComicInfo><AgeRating>Adults Only 18+</AgeRating></ComicInfo>",
        metron("<AgeRating>Unknown</AgeRating>"),
    )
    conflicts = review_preserved_fields(incoming, {})
    assert len(conflicts) == 1
    assert [(o.source, o.selectable) for o in conflicts[0].options] == [
        ("ComicInfo.xml", False),
        ("MetronInfo.xml", True),
    ]
    pair = render_archive_metadata(
        *snapshots(), incoming, shared_choices={"xml.AgeRating": "MetronInfo.xml"}
    )
    assert ET.fromstring(pair.comicinfo).findtext("AgeRating") == "Unknown"
    with pytest.raises(ArchiveMetadataRenderError, match="invalid_choice"):
        render_archive_metadata(
            *snapshots(), incoming, shared_choices={"xml.AgeRating": "ComicInfo.xml"}
        )


def test_imprint_without_publisher_has_actionable_error_not_a_hidden_choice():
    from pullbox.services.issue_file_metadata import file_metadata_error

    series, issue = snapshots()
    series = series.model_copy(
        update={"values": series.values.model_copy(update={"publisher": None})}
    )
    with pytest.raises(ArchiveMetadataRenderError) as failure:
        render_archive_metadata(
            series, issue, files("<ComicInfo><Imprint>Imprint</Imprint></ComicInfo>")
        )
    assert failure.value.code == "publisher_required"
    assert "publisher" in file_metadata_error(failure.value).lower()


def test_equivalent_arc_positions_do_not_create_a_review_decision():
    incoming = files(
        "<ComicInfo><StoryArc>First</StoryArc><StoryArcNumber>2</StoryArcNumber></ComicInfo>",
        metron("<Arcs><Arc><Name>First</Name><Number>02</Number></Arc></Arcs>"),
    )
    assert review_preserved_fields(incoming, {}) == []
