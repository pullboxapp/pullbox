"""Cross-document agreement is not identity verification or permission to write."""

from datetime import date
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from pullbox.core.archive import ArchiveReader
from pullbox.core.archive_metadata import ArchiveMetadataFiles, MetadataFile, MetadataReadDiagnostic
from pullbox.core.metadata_identity import MetadataEntityKind as Kind
from pullbox.services.archive_metadata_reconciliation import reconcile_archive_metadata


def files(comic: str | bytes | None = None, metron: str | bytes | None = None):
    def member(name, value):
        return MetadataFile(
            name, int(value is not None), value.encode() if isinstance(value, str) else value
        )

    return ArchiveMetadataFiles(member("ComicInfo.xml", comic), member("MetronInfo.xml", metron))


def ci(extra="", series="Harbor Lights", number="050-x"):
    return f"<ComicInfo><Series>{series}</Series><Number>{number}</Number>{extra}</ComicInfo>"


def mi(extra="", series="Harbor Lights", number="50-X", ids=""):
    return (
        f"<MetronInfo>{ids}<Series><Name>{series}</Name></Series>"
        f"<Number>{number}</Number>{extra}</MetronInfo>"
    )


def codes(result):
    return {(item.document, item.code, item.locator) for item in result.diagnostics}


def identities(result):
    return {
        (item.identity.namespace.value, item.identity.entity_kind.value, item.identity.external_id)
        for item in result.evidence
    }


def test_agreement_and_complementary_values_feed_one_shared_view():
    result = reconcile_archive_metadata(
        files(
            ci(
                "<Title>New Shores</Title><Summary>Reader summary</Summary>"
                "<Year>2020</Year><Month>6</Month><Writer>Alex Example</Writer>"
                "<PageCount>1200</PageCount><LanguageISO>en</LanguageISO>"
            ),
            mi(
                "<Stories><Story>New Shores</Story></Stories><CoverDate>2020-06-01</CoverDate>"
                "<StoreDate>2020-05-27</StoreDate><Publisher><Name>Example Press</Name></Publisher>"
                "<Credits><Credit><Creator>Alex Example</Creator>"
                "<Roles><Role>Writer</Role></Roles></Credit></Credits>"
            ),
        )
    )
    assert result.series.title == "Harbor Lights"
    assert result.series.publisher == "Example Press" and result.series.language == "en"
    assert result.issue.issue_number_text == "50-X"
    assert result.issue.title == "New Shores" and result.issue.description == "Reader summary"
    assert result.issue.cover_date == date(2020, 6, 1)
    assert result.issue.store_date == date(2020, 5, 27) and result.issue.page_count == 1200
    assert result.issue.credits and result.issue.credits[0].name == "Alex Example"
    assert result.issue.credits[0].role == "writer"
    assert result.comicinfo.publication_date_parts == (2020, 6, None)
    assert not result.requires_review


@pytest.mark.parametrize("count", [0, 1, 12, 1000000])
@pytest.mark.parametrize("with_metron", [False, True])
def test_comicinfo_issue_count_is_shared_metadata_not_unmapped_content(count, with_metron):
    metron = None
    if with_metron:
        # MetronInfo permits positive counts only; the writer omits a zero count.
        metron = mi().replace(
            "</Series>", f"<IssueCount>{count}</IssueCount></Series>" if count else "</Series>"
        )
    result = reconcile_archive_metadata(files(ci(f"<Count>{count}</Count>"), metron))
    assert result.comicinfo.series.issue_count == count
    assert result.series.issue_count == count
    assert not result.requires_review


def test_comicinfo_and_metron_issue_count_disagreement_remains_reviewable():
    metron = mi().replace("</Series>", "<IssueCount>13</IssueCount></Series>")
    result = reconcile_archive_metadata(files(ci("<Count>12</Count>"), metron))
    assert result.series.issue_count is None
    assert [
        (item.entity, item.field, item.comicinfo, item.metroninfo) for item in result.differences
    ] == [("series", "issue_count", 12, 13)]
    assert result.requires_review


@pytest.mark.parametrize("count", ["-1", "1000001", "12.5", "unknown", "9" * 11])
def test_invalid_comicinfo_issue_count_is_not_adopted(count):
    result = reconcile_archive_metadata(files(ci(f"<Count>{count}</Count>")))
    assert result.comicinfo.series.issue_count is None
    assert ("ComicInfo.xml", "invalid_field", "Count") in codes(result)


def test_duplicate_comicinfo_issue_counts_are_ambiguous():
    result = reconcile_archive_metadata(files(ci("<Count>12</Count><Count>13</Count>")))
    assert result.comicinfo.series.issue_count is None
    assert ("ComicInfo.xml", "ambiguous_field", "Count") in codes(result)


@pytest.mark.parametrize(
    "kind,notes,ids",
    [
        (Kind.ISSUE, "[cv_issue_id:101]", '<IDS><ID source="Comic Vine">202</ID></IDS>'),
        (
            Kind.SERIES,
            "[cv_vol_id:21]",
            '<IDS><ID source="Comic Vine" primary="true">101</ID></IDS>',
        ),
    ],
)
def test_exact_conflicts_are_retained_separately_from_descriptive_differences(kind, notes, ids):
    metron = mi(ids=ids)
    if kind is Kind.SERIES:
        metron = metron.replace("<Series>", '<Series id="22">')
    result = reconcile_archive_metadata(files(ci(f"<Notes>{notes}</Notes>"), metron))
    assert len(result.identity_conflicts) == 1
    assert result.identity_conflicts[0].entity_kind is kind
    assert result.requires_review and not result.differences
    assert len(result.identity_conflicts[0].evidence) == 2


def test_same_ids_agree_but_cooccurring_foreign_ids_do_not_become_verified_crosswalks():
    result = reconcile_archive_metadata(
        files(
            ci("<Notes>[cv_issue_id:00101] [cv_vol_id:21]</Notes>"),
            mi(ids='<IDS><ID source="Comic Vine">101</ID><ID source="Metron">77</ID></IDS>'),
        )
    )
    assert identities(result) == {
        ("comicvine", "issue", "101"),
        ("comicvine", "series", "21"),
        ("metron", "issue", "77"),
    }
    assert len(result.evidence) == 4  # Keep each source observation, not an attachment.
    assert not result.identity_conflicts and not result.requires_review


@pytest.mark.parametrize("number", ["13a", "13b", "13c", "50-x", "50-o", "-1", "0.5"])
def test_exact_designations_survive_reconciliation(number):
    result = reconcile_archive_metadata(files(ci(number=number), mi(number=number.upper())))
    assert result.issue.issue_number_text == number.upper()
    assert not result.requires_review


@pytest.mark.parametrize("number", ["50-O", "50X", "50", "51-X"])
def test_different_issue_designations_never_collapse_to_numeric_part(number):
    result = reconcile_archive_metadata(files(ci(), mi(number=number)))
    assert result.issue.issue_number_text is None
    assert {(item.entity, item.field) for item in result.differences} == {
        ("issue", "issue_number_text")
    }
    assert not result.identity_conflicts and result.requires_review


def test_disagreements_do_not_pick_a_document_and_input_is_retained():
    source = files(
        ci("<Summary>Local edit</Summary>", series="One"),
        mi("<Summary>Other edit</Summary>", series="Two"),
    )
    result = reconcile_archive_metadata(source)
    assert result.series.title is None and result.issue.description is None
    assert {
        (item.entity, item.field, item.comicinfo, item.metroninfo) for item in result.differences
    } == {("series", "title", "One", "Two"), ("issue", "description", "Local edit", "Other edit")}
    assert result.files is source and result.comicinfo.issue.description == "Local edit"
    assert result.metroninfo.issue.description == "Other edit" and result.requires_review


def test_credit_order_is_not_a_difference_but_an_explicit_empty_list_is():
    credits = (
        "<Credits><Credit><Creator>B</Creator><Roles><Role>Writer</Role></Roles></Credit>"
        "<Credit><Creator>A</Creator><Roles><Role>Writer</Role></Roles></Credit></Credits>"
    )
    result = reconcile_archive_metadata(files(ci("<Writer>A, B</Writer>"), mi(credits)))
    assert result.issue.credits and [credit.name for credit in result.issue.credits] == ["A", "B"]
    assert not result.requires_review
    cleared = reconcile_archive_metadata(files(ci("<Writer>A</Writer>"), mi("<Credits/>")))
    assert cleared.issue.credits is None
    assert [item.field for item in cleared.differences] == ["credits"]


@pytest.mark.parametrize(
    "names", ["Mike Deodato, Jr., Another Artist", "Another Artist, Mike Deodato, Jr."]
)
def test_structured_credits_disambiguate_only_the_matching_comicinfo_role(names):
    incoming = files(
        ci(f"<Penciller>{names}</Penciller>"),
        mi(
            "<Credits><Credit><Creator>Mike Deodato, Jr.</Creator><Roles><Role>Penciller</Role>"
            "<Role>Artist</Role></Roles></Credit><Credit><Creator>Another Artist</Creator>"
            "<Roles><Role>Penciller</Role></Roles></Credit></Credits>"
        ),
    )
    result = reconcile_archive_metadata(incoming)
    assert [c.name for c in result.comicinfo.issue.credits] == [
        "Another Artist",
        "Mike Deodato, Jr.",
    ]
    assert result.issue.credits == result.metroninfo.issue.credits
    assert not result.requires_review
    assert result.files is incoming


@pytest.mark.parametrize(
    "names,role",
    [
        ("Mike Deodato, Jr., Other Artist", "Penciller"),
        ("Mike Deodato, Jr.", "Writer"),
        ("Mike Deodato, Jr., Jr.", "Penciller"),
        ("Jr., Mike Deodato", "Penciller"),
    ],
)
def test_comma_names_do_not_hide_changed_people_roles_or_extra_entries(names, role):
    result = reconcile_archive_metadata(
        files(
            ci(f"<{role}>{names}</{role}>"),
            mi(
                "<Credits><Credit><Creator>Mike Deodato, Jr.</Creator>"
                "<Roles><Role>Penciller</Role></Roles></Credit></Credits>"
            ),
        )
    )
    assert result.requires_review
    assert "credits" in [item.field for item in result.differences]


def test_comicinfo_alone_does_not_guess_creator_grouping():
    result = reconcile_archive_metadata(files(ci("<Penciller>Mike Deodato, Jr.</Penciller>")))
    assert [c.name for c in result.issue.credits] == ["Jr.", "Mike Deodato"]


def test_missing_xml_is_optional_and_invalid_xml_does_not_hide_valid_evidence():
    empty = reconcile_archive_metadata(files())
    assert not empty.requires_review
    result = reconcile_archive_metadata(
        files(ci("<Notes>[cv_issue_id:101]</Notes>"), "<MetronInfo>")
    )
    assert result.series.title == "Harbor Lights"
    assert identities(result) == {("comicvine", "issue", "101")}
    assert ("MetronInfo.xml", "invalid_xml", "MetronInfo") in codes(result)
    assert result.requires_review


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://comicvine.gamespot.com/harbor/4050-21/", {("comicvine", "series", "21")}),
        ("http://comicvine.com/issue/4000-101/", {("comicvine", "issue", "101")}),
        ("https://comicvine.gamespot.com.evil.invalid/4050-21/", set()),
        ("https://evil.invalid/?comicvine.gamespot.com/4050-21/", set()),
        ("https://comicvine.gamespot.com@evil.invalid/4050-21/", set()),
        ("https://user:secret@comicvine.gamespot.com/4050-21/", set()),
        ("https://comicvine.gamespot.com:9999/4050-21/", set()),
        ("https://comicvine.gamespot.com/4050-21/4000-101/", set()),
        ("https://comicvine.gamespot.com/volume/?id=4050-21", set()),
    ],
)
def test_comicvine_urls_require_exact_allowed_host_and_resource_path(url, expected):
    result = reconcile_archive_metadata(files(ci(f"<Web>{url}</Web>")))
    assert identities(result) == expected


def test_all_duplicate_identity_assertions_are_compared_not_first_or_last_wins():
    result = reconcile_archive_metadata(
        files(
            ci(
                "<Notes>[cv_issue_id:101] [cv_issue_id:102]</Notes>"
                "<Notes>[cv_issue_id:103] [cv_vol_id:21]</Notes>"
            )
        )
    )
    assert identities(result) == {
        ("comicvine", "issue", "101"),
        ("comicvine", "issue", "102"),
        ("comicvine", "issue", "103"),
        ("comicvine", "series", "21"),
    }
    assert len(result.identity_conflicts) == 1
    assert ("ComicInfo.xml", "ambiguous_field", "Notes") in codes(result)


def test_loose_cvdb_and_volume_fields_never_establish_identity_or_series_year():
    result = reconcile_archive_metadata(
        files(ci("<Notes>[CVDB1132072]</Notes><Volume>209077</Volume>"))
    )
    assert not result.evidence
    assert result.series.year_start is None


@pytest.mark.parametrize("year,month,day", [(2021, 6, 1), (2020, 7, 1), (2020, 6, 2)])
def test_partial_calendar_conflicts_remain_visible(year, month, day):
    result = reconcile_archive_metadata(
        files(
            ci(f"<Year>{year}</Year><Month>{month}</Month><Day>{day}</Day>"),
            mi("<CoverDate>2020-06-01</CoverDate>"),
        )
    )
    assert result.issue.cover_date is None
    assert any(item.field == "cover_date" for item in result.differences)


def test_date_parts_do_not_invent_a_release_day_or_a_series_start_year():
    result = reconcile_archive_metadata(files(ci("<Year>2020</Year><Month>6</Month>")))
    assert result.comicinfo.publication_date_parts == (2020, 6, None)
    assert result.issue.cover_date is None and result.series.year_start is None


@pytest.mark.parametrize(
    "xml,code,locator",
    [
        ("<Other><Series>A</Series></Other>", "invalid_root", "ComicInfo"),
        ("<!DOCTYPE ComicInfo><ComicInfo/>", "unsafe_xml", "ComicInfo"),
        (
            "<ComicInfo><Series>A</Series><Series>B</Series></ComicInfo>",
            "ambiguous_field",
            "Series",
        ),
        (ci("<PageCount>-1</PageCount>"), "invalid_field", "PageCount"),
        (ci("<Year>2020</Year><Month>2</Month><Day>30</Day>"), "invalid_field", "CoverDate"),
        (ci("<Summary>" + "x" * 200001 + "</Summary>"), "invalid_field", "Summary"),
        (ci("<Summary><Nested>A</Nested></Summary>"), "invalid_field", "Summary"),
        (ci("<Unknown>preserve</Unknown>"), "unmapped_content", "ComicInfo"),
        (ci('<Writer extra="preserve">A</Writer>'), "unmapped_content", "ComicInfo"),
    ],
)
def test_comicinfo_review_diagnostics_are_typed_and_do_not_leak_payload(xml, code, locator):
    result = reconcile_archive_metadata(files(xml))
    assert ("ComicInfo.xml", code, locator) in codes(result)
    assert result.requires_review
    assert "x" * 100 not in repr(result.diagnostics)


@pytest.mark.parametrize(
    "xml,code",
    [
        (b"<ComicInfo>" + b" " * (2 * 1024 * 1024) + b"</ComicInfo>", "too_large"),
        ("<ComicInfo>" + "<Tag>" * 33 + "</Tag>" * 33 + "</ComicInfo>", "complexity_limit"),
        ("<ComicInfo>" + "<Tag/>" * 4096 + "</ComicInfo>", "complexity_limit"),
    ],
)
def test_comicinfo_uses_the_same_xml_resource_limits_as_metroninfo(xml, code):
    assert ("ComicInfo.xml", code, "ComicInfo") in codes(reconcile_archive_metadata(files(xml)))


@pytest.mark.parametrize("diagnostic", list(MetadataReadDiagnostic))
def test_archive_probe_failures_cannot_disappear_in_reconciliation(diagnostic):
    source = ArchiveMetadataFiles(
        MetadataFile("ComicInfo.xml", 1, diagnostics=(diagnostic,)), MetadataFile("MetronInfo.xml")
    )
    result = reconcile_archive_metadata(source)
    assert ("ComicInfo.xml", diagnostic.value, "ComicInfo.xml") in codes(result)
    assert result.requires_review


def test_rich_metron_resources_survive_without_becoming_canonical_identity():
    payload = (Path(__file__).parents[1] / "fixtures/metroninfo/descriptive.xml").read_bytes()
    result = reconcile_archive_metadata(files(metron=payload))
    assert result.metroninfo.metron and result.metroninfo.metron.credits
    assert result.metroninfo.metron.credits[0].creator.resource_id == "creator:17"
    assert result.issue.credits and result.issue.credits[0].role == "artist, writer"
    assert result.series.sort_title == "Harbor Lights, The" and result.series.language == "fr"
    assert result.issue.title is None  # Multiple story titles are not one issue title.


def test_real_archive_reconciliation_is_one_read_pass_and_never_writes(tmp_path):
    path = tmp_path / "issue.cbz"
    with ZipFile(path, "w") as archive:
        archive.writestr("ComicInfo.xml", ci("<Summary>Keep this</Summary>"))
        archive.writestr("MetronInfo.xml", mi())
    original = path.read_bytes()
    with patch("zipfile.ZipFile", wraps=ZipFile) as opened:
        result = reconcile_archive_metadata(ArchiveReader(path).read_metadata_files())
    assert result.issue.description == "Keep this" and not result.requires_review
    assert opened.call_count == 1 and path.read_bytes() == original


def test_reconciliation_cannot_call_providers_or_filesystem(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Reconciliation attempted external I/O")

    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    result = reconcile_archive_metadata(files(ci(), mi()))
    assert result.series.title == "Harbor Lights"


def test_story_arc_ids_do_not_conflict_with_each_other_or_canonical_issue_ids():
    result = reconcile_archive_metadata(
        files(
            ci("<Notes>[cv_issue_id:101]</Notes>"),
            mi(
                '<Arcs><Arc id="101"><Name>First Arc</Name><Number>1</Number></Arc>'
                '<Arc id="202"><Name>Second Arc</Name><Number>2</Number></Arc></Arcs>',
                ids='<IDS><ID source="Comic Vine" primary="true">101</ID></IDS>',
            ),
        )
    )
    assert len(result.metroninfo.metron.arcs) == 2
    assert identities(result) == {("comicvine", "issue", "101")}
    assert not result.requires_review


def test_unsupported_content_and_ambiguous_members_remain_visible_with_valid_values():
    source = files(ci("<Summary>Preserved</Summary>"), mi("<Extension>Preserve too</Extension>"))
    source = ArchiveMetadataFiles(
        MetadataFile("ComicInfo.xml", 2, source.comicinfo.payload), source.metroninfo
    )
    result = reconcile_archive_metadata(source)
    assert result.series.title == "Harbor Lights" and result.issue.description == "Preserved"
    assert ("ComicInfo.xml", "duplicate_entries", "ComicInfo.xml") in codes(result)
    assert ("MetronInfo.xml", "unmapped_content", "MetronInfo") in codes(result)
    assert result.requires_review and result.files is source


def test_invalid_credit_list_does_not_become_an_explicit_clear():
    result = reconcile_archive_metadata(
        files(
            ci("<Writer>A</Writer>"),
            mi("<Credits><Credit><Creator>B</Creator><Roles><Role/></Roles></Credit></Credits>"),
        )
    )
    assert result.metroninfo.issue.credits is None
    assert result.issue.credits and result.issue.credits[0].name == "A"
    assert not result.differences and result.requires_review
