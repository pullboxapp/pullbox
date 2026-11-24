"""Propagation changes selected parent fields, never issue-specific metadata."""

from xml.etree import ElementTree as ET

import pytest

from pullbox.models.series import SeriesType
from pullbox.schemas.series_publication_titles import SeriesPublicationTitle
from pullbox.services.archive_metadata_rendering import (
    ArchiveMetadataRenderError,
    render_archive_metadata,
)
from pullbox.services.series_metadata_propagation_rendering import render_series_propagation
from tests.unit.test_archive_metadata_rendering import files, origin, snapshots


def source_pair():
    series, issue = snapshots(title="Original issue", description="Keep this summary")
    pair = render_archive_metadata(series, issue, files())
    return series, issue, files(pair.comicinfo.decode(), pair.metroninfo.decode())


def test_selected_series_title_does_not_refresh_unselected_issue_values():
    series, issue, source = source_pair()
    series = series.model_copy(
        update={"values": series.values.model_copy(update={"title": "Corrected series"})}
    )
    issue = issue.model_copy(
        update={
            "values": issue.values.model_copy(update={"description": "Different catalog summary"})
        }
    )
    result = render_series_propagation(series, issue, source, frozenset({"title"}))
    ci, mi = ET.fromstring(result.comicinfo), ET.fromstring(result.metroninfo)
    assert ci.findtext("Series") == mi.findtext("Series/Name") == "Corrected series"
    assert ci.findtext("Summary") == mi.findtext("Summary") == "Keep this summary"
    assert ci.findtext("Title") == "Original issue"


@pytest.mark.parametrize(
    "field", ["description", "issue_count", "status", "year_end", "issue_number_text"]
)
def test_nonpropagatable_fields_are_rejected(field):
    series, issue, source = source_pair()
    with pytest.raises(ArchiveMetadataRenderError):
        render_series_propagation(series, issue, source, frozenset({field}))


def test_repeated_projection_is_stable_and_start_year_is_not_issue_date():
    series, issue, source = source_pair()
    series = series.model_copy(
        update={"values": series.values.model_copy(update={"year_start": 1999, "language": "en"})}
    )
    fields = frozenset({"year_start", "language"})
    result = render_series_propagation(series, issue, source, fields)
    assert ET.fromstring(result.comicinfo).find("Year") is None
    assert ET.fromstring(result.metroninfo).findtext("Series/StartYear") == "1999"
    assert (
        render_series_propagation(
            series, issue, files(result.comicinfo.decode(), result.metroninfo.decode()), fields
        )
        == result
    )


@pytest.mark.parametrize("format_value", [*SeriesType, "Limited Series", None])
def test_all_selected_series_fields_leave_issue_data_intact(format_value):
    from pullbox.services.issue_file_metadata import _fields

    series, issue, source = source_pair()
    series = series.model_copy(
        update={
            "values": series.values.model_copy(
                update={
                    "publisher": "Corrected publisher",
                    "language": "en",
                    "volume": "2",
                    "sort_title": "Sorting title",
                    "year_start": 2000,
                    "series_type": format_value,
                }
            )
        }
    )
    result = render_series_propagation(
        series,
        issue,
        source,
        frozenset({"publisher", "language", "volume", "sort_title", "year_start", "format"}),
    )
    old, new = _fields(source.comicinfo.payload), _fields(result.comicinfo)
    assert {
        k: v
        for k, v in old.items()
        if not any(x in k for x in ("/Publisher", "/LanguageISO", "/Format"))
    } == {
        k: v
        for k, v in new.items()
        if not any(x in k for x in ("/Publisher", "/LanguageISO", "/Format"))
    }
    mi = ET.fromstring(result.metroninfo)
    assert mi.findtext("Series/Volume") == "2"
    assert mi.findtext("Publisher/Name") == "Corrected publisher"


def test_selected_publisher_never_discards_resource_identity():
    series, issue, source = source_pair()
    mi = ET.fromstring(source.metroninfo.payload)
    mi.find("Publisher").set("id", "123")
    series = series.model_copy(
        update={"values": series.values.model_copy(update={"publisher": "Different publisher"})}
    )
    with pytest.raises(ArchiveMetadataRenderError) as caught:
        render_series_propagation(
            series,
            issue,
            files(source.comicinfo.payload.decode(), ET.tostring(mi).decode()),
            frozenset({"publisher"}),
        )
    assert caught.value.code == "resource_metadata_change"


def test_total_volumes_changes_only_selected_metron_series_value():
    series, issue, source = source_pair()
    mi = ET.fromstring(source.metroninfo.payload)
    parent = mi.find("Series")
    for tag, value in (("Volume", "2"), ("IssueCount", "12"), ("VolumeCount", "4")):
        if parent.find(tag) is None:
            ET.SubElement(parent, tag)
        parent.find(tag).text = value
    source = files(source.comicinfo.payload.decode(), ET.tostring(mi).decode())
    from pullbox.services.issue_file_metadata import file_metadata_error

    with pytest.raises(ArchiveMetadataRenderError) as caught:
        render_series_propagation(series, issue, source, frozenset({"volume_count"}))
    assert "Clear value" in file_metadata_error(caught.value)
    for count in (7, None):
        series = series.model_copy(
            update={
                "values": series.values.model_copy(update={"volume_count": count}),
                "origins": (origin("volume_count", user=True),),
            }
        )
        unchanged = render_series_propagation(series, issue, source, frozenset({"title"}))
        assert ET.fromstring(unchanged.metroninfo).findtext("Series/VolumeCount") == "4"
        result = render_series_propagation(series, issue, source, frozenset({"volume_count"}))
        out = ET.fromstring(result.metroninfo)
        assert out.findtext("Series/VolumeCount") == (str(count) if count else None)
        assert out.findtext("Series/Volume") == "2" and out.findtext("Series/IssueCount") == "12"
        assert result.comicinfo == unchanged.comicinfo


def test_identity_and_issue_number_conflicts_are_not_overridden():
    series, issue, source = source_pair()
    ci = ET.fromstring(source.comicinfo.payload)
    ci.find("Number").text = "999"
    with pytest.raises(ArchiveMetadataRenderError):
        render_series_propagation(
            series,
            issue,
            files(ET.tostring(ci).decode(), source.metroninfo.payload.decode()),
            frozenset({"title"}),
        )


def _title_projection(existing, selected, *, clear=False):
    series, issue, source = source_pair()
    mi = ET.fromstring(source.metroninfo.payload)
    mi.find("Series").append(
        ET.fromstring(existing, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    )
    # Formatting of the unselected ComicInfo member must survive the actual write.
    source = files(source.comicinfo.payload.decode() + "\n", ET.tostring(mi).decode())
    titles = (
        tuple(SeriesPublicationTitle(name=name, language=lang) for name, lang in selected) or None
    )
    series = series.model_copy(
        update={
            "values": series.values.model_copy(update={"publication_titles": titles}),
            "origins": (origin("publication_titles", user=True),) if clear or titles else (),
        }
    )
    return series, issue, source


def test_publication_titles_preserve_localized_ids_and_never_copy_them_to_new_names():
    args = _title_projection(
        '<AlternativeNames><AlternativeName id="a">Port</AlternativeName>'
        '<AlternativeName lang="fr" id="b">Port</AlternativeName>'
        '<AlternativeName lang="de">Old</AlternativeName></AlternativeNames>',
        [("Port", "fr"), ("Port", "en"), ("Der Hafen", "de")],
    )
    chosen = frozenset({"publication_titles"})
    result = render_series_propagation(*args, chosen)
    nodes = ET.fromstring(result.metroninfo).findall("Series/AlternativeNames/AlternativeName")
    assert [(n.text, n.attrib) for n in nodes] == [
        ("Port", {"lang": "fr", "id": "b"}),
        ("Port", {"id": "a"}),
        ("Der Hafen", {"lang": "de"}),
    ]
    assert result.comicinfo == args[2].comicinfo.payload
    assert (
        render_series_propagation(
            args[0], args[1], files(result.comicinfo.decode(), result.metroninfo.decode()), chosen
        )
        == result
    )


@pytest.mark.parametrize(
    "inner",
    [
        '<AlternativeName id="a">Port</AlternativeName>'
        '<AlternativeName id="b" lang="en">Port</AlternativeName>',
        "<!--proof--><AlternativeName>Port</AlternativeName>",
    ],
)
def test_ambiguous_or_annotated_publication_title_lists_are_not_replaced(inner):
    args = _title_projection(f"<AlternativeNames>{inner}</AlternativeNames>", [("Port", "en")])
    with pytest.raises(ArchiveMetadataRenderError) as caught:
        render_series_propagation(*args, frozenset({"publication_titles"}))
    assert caught.value.code in {"propagation_titles_ambiguous", "propagation_titles_annotated"}


def test_titles_only_do_not_normalize_unselected_metron_fields():
    series, issue, source = _title_projection("<AlternativeNames/>", [("Port", "en")])
    mi = ET.fromstring(source.metroninfo.payload)
    attr = "{http://www.w3.org/2001/XMLSchema-instance}noNamespaceSchemaLocation"
    mi.set(attr, "local-copy.xsd")

    def project():
        return render_series_propagation(
            series,
            issue,
            files(source.comicinfo.payload.decode(), ET.tostring(mi).decode()),
            frozenset({"publication_titles"}),
        )

    assert ET.fromstring(project().metroninfo).get(attr) == "local-copy.xsd"
    mi.set("version", "1.0")  # Requires unrelated normalization before it can validate as 1.1.
    with pytest.raises(ArchiveMetadataRenderError) as caught:
        project()
    assert caught.value.code == "propagation_titles_legacy_xml"


@pytest.mark.parametrize(
    ("selected", "explicit_clear", "expected"),
    [
        ([], False, "Clear value"),
        ([], True, "identifier"),
        ([("Port", "fr")], False, "identifier"),
        ([("PORT", "en")], False, "identifier"),
    ],
)
def test_missing_or_conflicting_title_identity_never_silently_rewrites(
    selected, explicit_clear, expected
):
    from pullbox.services.issue_file_metadata import file_metadata_error

    args = _title_projection(
        '<AlternativeNames><AlternativeName id="a">Port</AlternativeName></AlternativeNames>',
        selected,
        clear=explicit_clear,
    )
    with pytest.raises(ArchiveMetadataRenderError) as caught:
        render_series_propagation(*args, frozenset({"publication_titles"}))
    assert expected in file_metadata_error(caught.value)


def test_explicit_publication_title_clear_only_removes_unidentified_names():
    args = _title_projection(
        "<AlternativeNames><AlternativeName>Port</AlternativeName></AlternativeNames>",
        [],
        clear=True,
    )
    assert (
        ET.fromstring(render_series_propagation(*args, frozenset({"title"})).metroninfo).findtext(
            "Series/AlternativeNames/AlternativeName"
        )
        == "Port"
    )
    result = render_series_propagation(*args, frozenset({"publication_titles"}))
    assert ET.fromstring(result.metroninfo).find("Series/AlternativeNames") is None
    assert result.comicinfo == args[2].comicinfo.payload
