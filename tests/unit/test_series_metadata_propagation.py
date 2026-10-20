"""Propagation changes selected parent fields, never issue-specific metadata."""

from xml.etree import ElementTree as ET

import pytest

from pullbox.models.series import SeriesType
from pullbox.services.archive_metadata_rendering import (
    ArchiveMetadataRenderError,
    render_archive_metadata,
)
from pullbox.services.series_metadata_propagation_rendering import render_series_propagation
from tests.unit.test_archive_metadata_rendering import files, snapshots


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
