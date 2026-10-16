"""Paired output must preserve evidence, edits, and supported archive metadata."""

from datetime import UTC, date, datetime
from xml.etree import ElementTree as ET

import pytest

from pullbox.core.archive_metadata import ArchiveMetadataFiles, MetadataFile
from pullbox.core.metadata_identity import (
    ExternalIdentityRef,
    MetadataSource,
)
from pullbox.core.metadata_identity import (
    IdentityNamespace as Namespace,
)
from pullbox.core.metadata_identity import (
    MetadataEntityKind as Kind,
)
from pullbox.core.metroninfo import parse_metroninfo
from pullbox.core.metroninfo_schema import validate_metroninfo_xml
from pullbox.schemas.metadata_snapshot import (
    FieldOrigin,
    MetadataSnapshot,
    MetadataValues,
    PassiveReleaseOrigin,
)
from pullbox.schemas.metadata_sources import MetadataDomain
from pullbox.services.archive_metadata_reconciliation import reconcile_archive_metadata
from pullbox.services.archive_metadata_rendering import (
    ArchiveMetadataRenderError,
    render_archive_metadata,
)

CV_SERIES = ExternalIdentityRef(Namespace.COMICVINE, Kind.SERIES, "42")
CV_ISSUE = ExternalIdentityRef(Namespace.COMICVINE, Kind.ISSUE, "7")
METRON_SERIES = ExternalIdentityRef(Namespace.METRON, Kind.SERIES, "88")
METRON_ISSUE = ExternalIdentityRef(Namespace.METRON, Kind.ISSUE, "9")


def snapshots(**issue_values):
    return (
        MetadataSnapshot(
            entity_kind=Kind.SERIES,
            identities=(CV_SERIES,),
            values=MetadataValues(title="Example & Friends", publisher="Publisher"),
        ),
        MetadataSnapshot(
            entity_kind=Kind.ISSUE,
            identities=(CV_ISSUE,),
            values=MetadataValues(issue_number_text="50-x", **issue_values),
        ),
    )


def files(ci="", mi=""):
    return ArchiveMetadataFiles(
        MetadataFile("ComicInfo.xml", int(bool(ci)), ci.encode() if ci else None),
        MetadataFile("MetronInfo.xml", int(bool(mi)), mi.encode() if mi else None),
    )


def metron(content="", series="<Name>Example &amp; Friends</Name>", attrs=""):
    return f"<MetronInfo><Series {attrs}>{series}</Series>{content}</MetronInfo>"


def origin(field, *, user=False):
    return FieldOrigin(
        field=field,
        domain=MetadataDomain.CORE,
        observed_at=datetime(2026, 9, 28, tzinfo=UTC),
        source=None if user else MetadataSource.COMICVINE_API,
        user_override=user,
    )


def test_paired_core_output_uses_one_snapshot_and_validates_offline():
    series, issue = snapshots(
        title="Part <One>",
        description="A & B",
        page_count=0,
        cover_date=date(2026, 9, 28),
        store_date=date(2026, 9, 25),
        credits=({"name": "A Writer", "role": "writer, penciller"},),
    )
    result = render_archive_metadata(series, issue, files())
    assert result.comicinfo and result.metroninfo
    ci = ET.fromstring(result.comicinfo)
    mi = parse_metroninfo(result.metroninfo)
    validate_metroninfo_xml(result.metroninfo)
    assert ci.findtext("Series") == mi.series == series.values.title
    assert ci.findtext("Title") == mi.stories[0] == issue.values.title
    assert ci.findtext("Summary") == mi.summary == issue.values.description
    assert ci.findtext("Number") == mi.number == "50-x"
    assert ci.findtext("Publisher") == mi.publisher == "Publisher"
    assert ci.findtext("PageCount") == "0" and mi.page_count == 0
    assert (ci.findtext("Year"), ci.findtext("Month"), ci.findtext("Day")) == ("2026", "9", "28")
    assert mi.cover_date == issue.values.cover_date and mi.store_date == issue.values.store_date
    assert ci.findtext("Writer") == ci.findtext("Penciller") == "A Writer"
    assert {r.value for r in mi.credits[0].roles} == {"Writer", "Penciller"}
    assert {e.identity for e in mi.evidence} == {CV_SERIES, CV_ISSUE}
    assert "[cv_issue_id:7]" in ci.findtext("Notes")
    assert "[cv_vol_id:42]" in ci.findtext("Notes")


def test_passive_release_store_date_writes_metron_date_without_inventing_comicinfo_cover_date():
    series, issue = snapshots(store_date=date(2026, 9, 25))
    issue = MetadataSnapshot.model_validate(
        {
            **issue.model_dump(),
            "values": {**issue.values.model_dump(), "issue_number_text": "50-X"},
            "origins": (
                FieldOrigin(
                    field="store_date",
                    domain=MetadataDomain.CORE,
                    observed_at=datetime(2026, 9, 28, tzinfo=UTC),
                    passive_release=PassiveReleaseOrigin(
                        locg_series_id="77",
                        release_ids=("1001", "1002"),
                        fetched_at=datetime(2026, 9, 28, tzinfo=UTC),
                        issue_identity=CV_ISSUE,
                        issue_number_text="50-X",
                        matched_store_date=date(2026, 9, 25),
                        match_kind="exact_issue",
                    ),
                ),
            ),
        }
    )
    result = render_archive_metadata(series, issue, files())
    parsed = parse_metroninfo(result.metroninfo)
    assert parsed.store_date == date(2026, 9, 25)
    assert parsed.cover_date is None
    ci = ET.fromstring(result.comicinfo)
    assert ci.find("Year") is None and ci.find("Month") is None and ci.find("Day") is None
    assert {item.identity for item in parsed.evidence} == {CV_SERIES, CV_ISSUE}
    assert b"1001" not in result.metroninfo and b"1002" not in result.metroninfo
    validate_metroninfo_xml(result.metroninfo)


def test_explicit_credit_clear_cannot_discard_resource_ids():
    series, issue = snapshots()
    issue = issue.model_copy(update={"origins": (origin("credits", user=True),)})
    incoming = files(
        mi=metron(
            '<IDS><ID source="Comic Vine" primary="true">7</ID></IDS>'
            '<Credits><Credit><Creator id="88">Creator</Creator>'
            "<Roles><Role>Writer</Role></Roles></Credit></Credits>"
        )
    )
    with pytest.raises(ArchiveMetadataRenderError, match="resource_metadata_change"):
        render_archive_metadata(series, issue, incoming)


def test_multiple_verified_ids_require_explicit_or_existing_primary():
    series, issue = snapshots()
    series = series.model_copy(update={"identities": (CV_SERIES, METRON_SERIES)})
    issue = issue.model_copy(update={"identities": (CV_ISSUE, METRON_ISSUE)})
    with pytest.raises(ArchiveMetadataRenderError, match="primary_identity_required"):
        render_archive_metadata(series, issue, files())
    rendered = render_archive_metadata(series, issue, files(), primary_identity=METRON_ISSUE)
    mi = parse_metroninfo(rendered.metroninfo)
    assert mi.primary_source == Namespace.METRON
    assert {e.identity for e in mi.evidence} == {METRON_SERIES, CV_ISSUE, METRON_ISSUE}
    assert ET.fromstring(rendered.metroninfo).find("Series").get("id") == "88"
    assert (
        render_archive_metadata(series, issue, files(mi=rendered.metroninfo.decode())) == rendered
    )


def test_added_verified_identity_preserves_prior_baseline_and_local_clear():
    old_series, old_issue = snapshots(description="Old summary")
    series = old_series.model_copy(update={"identities": (CV_SERIES, METRON_SERIES)})
    issue = old_issue.model_copy(
        update={
            "identities": (CV_ISSUE, METRON_ISSUE),
            "values": old_issue.values.model_copy(update={"description": None}),
            "origins": (origin("description", user=True),),
        }
    )
    pair = render_archive_metadata(
        series,
        issue,
        files("<ComicInfo><Summary>Old summary</Summary></ComicInfo>"),
        primary_identity=CV_ISSUE,
        previous_series=old_series,
        previous_issue=old_issue,
    )
    assert ET.fromstring(pair.comicinfo).findtext("Summary") is None
    assert parse_metroninfo(pair.metroninfo).summary is None
    assert {e.identity for e in parse_metroninfo(pair.metroninfo).evidence} == {
        CV_SERIES,
        CV_ISSUE,
        METRON_ISSUE,
    }


@pytest.mark.parametrize("primary", [CV_SERIES, METRON_ISSUE])
def test_primary_identity_must_be_a_verified_issue(primary):
    with pytest.raises(ArchiveMetadataRenderError, match="invalid_primary_identity"):
        render_archive_metadata(*snapshots(), files(), primary_identity=primary)


@pytest.mark.parametrize(
    "ci,mi",
    [
        ("<ComicInfo><Notes>[cv_issue_id:8]</Notes></ComicInfo>", ""),
        ("<ComicInfo><Notes>[cv_vol_id:43]</Notes></ComicInfo>", ""),
        ("", metron('<IDS><ID source="Comic Vine" primary="true">8</ID></IDS>')),
        ("", metron('<IDS><ID source="Comic Vine" primary="true">7</ID></IDS>', attrs='id="43"')),
    ],
)
def test_conflicting_exact_ids_stop_even_explicit_override(ci, mi):
    series, issue = snapshots(title="New")
    issue = issue.model_copy(update={"origins": (origin("title", user=True),)})
    with pytest.raises(ArchiveMetadataRenderError, match="identity_conflict"):
        render_archive_metadata(series, issue, files(ci, mi), replace_managed=True)


def test_unverified_archive_id_is_not_promoted_or_discarded():
    incoming = files(mi=metron('<IDS><ID source="Metron">9</ID></IDS>'))
    with pytest.raises(ArchiveMetadataRenderError, match="unverified_identity"):
        render_archive_metadata(*snapshots(), incoming)


@pytest.mark.parametrize("old,new", [("Local title", "Provider title"), ("Local title", None)])
def test_unassembled_local_values_cannot_be_overwritten_or_erased(old, new):
    with pytest.raises(ArchiveMetadataRenderError, match="unreconciled_field"):
        render_archive_metadata(
            *snapshots(title=new), files(ci=f"<ComicInfo><Title>{old}</Title></ComicInfo>")
        )


@pytest.mark.parametrize(
    "embedded,canonical",
    [
        ("Civil War - Unmasked", "Civil War: Unmasked"),
        ("Batman - The Dark Knight - Golden Dawn", "Batman: The Dark Knight: Golden Dawn"),
    ],
)
def test_exact_issue_anchor_allows_series_separator_aliases(embedded, canonical):
    series, issue = snapshots()
    series = series.model_copy(update={"values": MetadataValues(title=canonical)})
    incoming = files(
        ci=(
            f"<ComicInfo><Series>{embedded}</Series>"
            "<Web>https://comicvine.gamespot.com/issue/4000-7/</Web></ComicInfo>"
        )
    )
    pair = render_archive_metadata(series, issue, incoming)
    assert ET.fromstring(pair.comicinfo).findtext("Series") == canonical
    assert parse_metroninfo(pair.metroninfo).series == canonical


@pytest.mark.parametrize(
    "embedded,canonical,anchored",
    [
        ("Civil War - Unmasked", "Civil War: Unmasked", False),
        ("X-Men", "X Men", True),
        ("Different Book", "Civil War: Unmasked", True),
    ],
)
def test_series_aliases_never_authorize_unbound_or_semantic_changes(embedded, canonical, anchored):
    series, issue = snapshots()
    series = series.model_copy(update={"values": MetadataValues(title=canonical)})
    web = "<Web>https://comicvine.gamespot.com/issue/4000-7/</Web>" if anchored else ""
    with pytest.raises(ArchiveMetadataRenderError, match="unreconciled_field"):
        render_archive_metadata(
            series, issue, files(ci=f"<ComicInfo><Series>{embedded}</Series>{web}</ComicInfo>")
        )


@pytest.mark.parametrize("value", [None, "", "Reviewed title"])
def test_explicit_user_override_updates_or_clears_both_documents(value):
    series, issue = snapshots(title=value)
    issue = issue.model_copy(update={"origins": (origin("title", user=True),)})
    result = render_archive_metadata(
        series,
        issue,
        files(
            ci="<ComicInfo><Title>Local</Title></ComicInfo>",
            mi=metron("<Stories><Story>Other local</Story></Stories>"),
        ),
    )
    assert result.comicinfo and result.metroninfo
    assert ET.fromstring(result.comicinfo).findtext("Title") == (value or None)
    assert parse_metroninfo(result.metroninfo).stories == ((value,) if value else ())


@pytest.mark.parametrize(
    "replace,edited,allowed", [(False, False, False), (True, True, False), (True, False, True)]
)
def test_managed_refresh_requires_unchanged_previous_baseline(replace, edited, allowed):
    series, previous = snapshots(title="Old managed")
    previous = previous.model_copy(update={"origins": (origin("title"),)})
    issue = previous.model_copy(
        update={"values": previous.values.model_copy(update={"title": "New managed"})}
    )
    incoming = files(
        ci=f"<ComicInfo><Title>{'Local edit' if edited else 'Old managed'}</Title></ComicInfo>"
    )
    if not allowed:
        with pytest.raises(ArchiveMetadataRenderError, match="unreconciled_field"):
            render_archive_metadata(
                series, issue, incoming, previous_issue=previous, replace_managed=replace
            )
    else:
        result = render_archive_metadata(
            series, issue, incoming, previous_issue=previous, replace_managed=True
        )
        assert parse_metroninfo(result.metroninfo).stories == ("New managed",)


@pytest.mark.parametrize(
    "ci,mi",
    [
        ("<ComicInfo><Number>50-o</Number></ComicInfo>", ""),
        ("<ComicInfo><Number>50x</Number></ComicInfo>", ""),
        ("", metron("<Number>50-o</Number>")),
    ],
)
def test_issue_designation_change_is_not_a_descriptive_override(ci, mi):
    series, issue = snapshots()
    issue = issue.model_copy(update={"origins": (origin("issue_number_text", user=True),)})
    with pytest.raises(ArchiveMetadataRenderError, match="issue_number_conflict"):
        render_archive_metadata(series, issue, files(ci, mi))


@pytest.mark.parametrize("number", ["50-X", "050-x"])
def test_equivalent_lettered_issue_numbers_keep_canonical_text(number):
    result = render_archive_metadata(
        *snapshots(), files(ci=f"<ComicInfo><Number>{number}</Number></ComicInfo>")
    )
    assert result.comicinfo and result.metroninfo
    assert parse_metroninfo(result.metroninfo).number == "50-x"


@pytest.mark.parametrize(
    "ci,mi",
    [
        ("<ComicInfo><Extension>Keep me</Extension></ComicInfo>", ""),
        ("<ComicInfo custom='keep'><Series>Example</Series></ComicInfo>", ""),
        ("", metron("<Extension>Keep me</Extension>")),
        ("<ComicInfo><Title>broken", ""),
        ("<!DOCTYPE ComicInfo [<!ENTITY x 'secret'>]><ComicInfo>&x;</ComicInfo>", ""),
        ("", '<MetronInfo version="9"><Series><Name>Example</Name></Series></MetronInfo>'),
    ],
)
def test_unsupported_or_unsafe_content_requires_review_not_loss(ci, mi):
    with pytest.raises(ArchiveMetadataRenderError):
        render_archive_metadata(*snapshots(), files(ci, mi))


def test_preserves_pages_notes_comments_and_rich_supported_resources():
    incoming = files(
        ci="<ComicInfo><!--reader note--><Notes>My notes</Notes><Pages>"
        '<Page Image="0" Type="FrontCover" Bookmark="Start" /></Pages>'
        "<Manga>No</Manga></ComicInfo>",
        mi=metron(
            '<IDS><ID source="Comic Vine" primary="true">7</ID></IDS>'
            '<Characters><Character id="99">Hero</Character></Characters>'
            '<Prices><Price country="US">3.99</Price></Prices>'
            "<Notes>Metron note</Notes><GTIN><ISBN>123</ISBN></GTIN>"
        ),
    )
    result = render_archive_metadata(*snapshots(), incoming)
    assert b"<!--reader note-->" in result.comicinfo
    ci, mi = ET.fromstring(result.comicinfo), ET.fromstring(result.metroninfo)
    assert ci.find("Pages/Page").attrib == {"Image": "0", "Type": "FrontCover", "Bookmark": "Start"}
    assert ci.findtext("Manga") == "No"
    assert ci.findtext("Notes").startswith("My notes")
    assert mi.findtext("Notes") == "Metron note"
    assert ci.findtext("Characters") == mi.findtext("Characters/Character") == "Hero"
    assert mi.find("Characters/Character").get("id") == "99"
    assert mi.findtext("Prices/Price") == "3.99"
    assert mi.findtext("GTIN/ISBN") == "123"
    assert incoming.comicinfo.payload.endswith(b"</ComicInfo>")
    assert b"[cv_issue_id:7]" not in incoming.comicinfo.payload


def test_changing_primary_never_reinterprets_resource_ids():
    series, issue = snapshots()
    series = series.model_copy(update={"identities": (CV_SERIES, METRON_SERIES)})
    issue = issue.model_copy(update={"identities": (CV_ISSUE, METRON_ISSUE)})
    incoming = files(
        mi=metron(
            '<IDS><ID source="Comic Vine" primary="true">7</ID></IDS>'
            '<Characters><Character id="99">Hero</Character></Characters>'
        )
    )
    with pytest.raises(ArchiveMetadataRenderError, match="resource_namespace_change"):
        render_archive_metadata(series, issue, incoming, primary_identity=METRON_ISSUE)


@pytest.mark.parametrize(
    "ci,mi",
    [
        ("<ComicInfo><Year>2025</Year></ComicInfo>", ""),
        ("<ComicInfo><Month>8</Month></ComicInfo>", ""),
        ("<ComicInfo><Day>27</Day></ComicInfo>", ""),
    ],
)
def test_partial_publication_date_cannot_be_replaced_by_unrelated_full_date(ci, mi):
    with pytest.raises(ArchiveMetadataRenderError, match="unreconciled_field"):
        render_archive_metadata(*snapshots(cover_date=date(2026, 9, 28)), files(ci, mi))


def test_schema_failure_never_returns_half_a_pair():
    series, issue = snapshots()
    series = series.model_copy(
        update={"values": series.values.model_copy(update={"language": "english"})}
    )
    with pytest.raises(ArchiveMetadataRenderError, match="invalid_output"):
        render_archive_metadata(series, issue, files())


def test_story_arc_names_and_order_are_coordinated_without_attaching_identity():
    incoming = files(
        mi=metron(
            "<Arcs><Arc><Name>First arc</Name><Number>2</Number></Arc>"
            "<Arc><Name>Second arc</Name><Number>5</Number></Arc></Arcs>"
        )
    )
    result = render_archive_metadata(*snapshots(), incoming)
    assert result.comicinfo and result.metroninfo
    ci = ET.fromstring(result.comicinfo)
    assert ci.findtext("StoryArc") == "First arc, Second arc"
    assert ci.findtext("StoryArcNumber") == "2, 5"
    assert len(parse_metroninfo(result.metroninfo).arcs) == 2


def test_conflicting_story_arc_membership_stops_for_review():
    incoming = files(
        ci="<ComicInfo><StoryArc>Other</StoryArc></ComicInfo>",
        mi=metron("<Arcs><Arc><Name>First arc</Name></Arc></Arcs>"),
    )
    with pytest.raises(ArchiveMetadataRenderError, match="unreconciled_field"):
        render_archive_metadata(*snapshots(), incoming)


def test_rich_roles_do_not_create_false_cross_document_credit_conflicts():
    series, issue = snapshots(credits=({"name": "Creator", "role": "writer, artist, translator"},))
    result = render_archive_metadata(series, issue, files())
    assert result.comicinfo and result.metroninfo
    parsed = reconcile_archive_metadata(
        files(result.comicinfo.decode(), result.metroninfo.decode())
    )
    assert not parsed.differences
    assert parsed.issue.credits == issue.values.credits


def test_reconciliation_compares_only_representable_credit_roles():
    parsed = reconcile_archive_metadata(
        files(
            "<ComicInfo><Writer>Creator</Writer><Translator>Creator</Translator></ComicInfo>",
            metron(
                "<Credits><Credit><Creator>Creator</Creator><Roles><Role>Writer</Role>"
                "<Role>Artist</Role><Role>Translator</Role></Roles></Credit></Credits>"
            ),
        )
    )
    assert not parsed.differences
    assert parsed.issue.credits[0].role == "artist, translator, writer"


def test_metadata_comments_are_preserved_but_still_bounded():
    from pullbox.core.metadata_xml import MetadataXmlError, parse_metadata_xml

    root = parse_metadata_xml(
        "<ComicInfo><!--note--><?local hint?></ComicInfo>",
        root_name="ComicInfo",
        preserve_misc=True,
    )
    assert len(root) == 2
    with pytest.raises(MetadataXmlError, match="complexity_limit"):
        parse_metadata_xml(
            "<ComicInfo>" + "<!--note-->" * 4096 + "</ComicInfo>",
            root_name="ComicInfo",
            preserve_misc=True,
        )


def test_locg_release_references_are_preserved_not_emitted_as_canonical_issue_ids():
    incoming = files(
        mi=metron(
            '<IDS><ID source="Comic Vine" primary="true">7</ID>'
            '<ID source="League of Comic Geeks">123</ID>'
            '<ID source="League of Comic Geeks">456</ID></IDS>'
        )
    )
    result = render_archive_metadata(*snapshots(), incoming)
    assert {ref.external_id for ref in parse_metroninfo(result.metroninfo).release_references} == {
        "123",
        "456",
    }


@pytest.mark.parametrize(
    "content",
    [
        "<GTIN><ISBN><Extension>local data</Extension></ISBN></GTIN>",
        '<GTIN><UPC custom="keep">123</UPC></GTIN>',
    ],
)
def test_schema_anytype_is_not_permission_to_keep_unsupported_extensions(content):
    with pytest.raises(ArchiveMetadataRenderError, match="unsupported_content"):
        render_archive_metadata(*snapshots(), files(mi=metron(content)))


@pytest.mark.parametrize(
    "ci,mi",
    [
        ('<ComicInfo><Pages><Page Image="0" unknown="keep" /></Pages></ComicInfo>', ""),
        ("<ComicInfo><Title>A</Title><Title>B</Title></ComicInfo>", ""),
        ("<ComicInfo><Year>2026</Year><Month>2</Month><Day>31</Day></ComicInfo>", ""),
    ],
)
def test_ambiguous_structured_values_do_not_get_replaced(ci, mi):
    with pytest.raises(ArchiveMetadataRenderError):
        render_archive_metadata(*snapshots(), files(ci, mi))


@pytest.mark.parametrize(
    "field,xml",
    [
        ("title", '<Stories><Story id="88">Old title</Story></Stories>'),
        ("publisher", '<Publisher id="88"><Name>Old publisher</Name></Publisher>'),
        (
            "credits",
            '<Credits><Credit><Creator id="88">Old creator</Creator>'
            "<Roles><Role>Writer</Role></Roles></Credit></Credits>",
        ),
    ],
)
def test_descriptive_override_cannot_relabel_opaque_resource_ids(field, xml):
    series, issue = snapshots(
        title="New title", credits=({"name": "New creator", "role": "writer"},)
    )
    if field == "publisher":
        series = series.model_copy(update={"origins": (origin(field, user=True),)})
    else:
        issue = issue.model_copy(update={"origins": (origin(field, user=True),)})
    incoming = files(mi=metron('<IDS><ID source="Comic Vine" primary="true">7</ID></IDS>' + xml))
    with pytest.raises(ArchiveMetadataRenderError, match="resource_metadata_change"):
        render_archive_metadata(series, issue, incoming)


def test_unchanged_credit_resource_ids_survive_the_round_trip():
    series, issue = snapshots(credits=({"name": "Creator", "role": "writer"},))
    incoming = files(
        mi=metron(
            '<IDS><ID source="Comic Vine" primary="true">7</ID></IDS><Credits>'
            '<Credit><Creator id="88">Creator</Creator><Roles>'
            '<Role id="2">Writer</Role></Roles></Credit></Credits>'
        )
    )
    result = render_archive_metadata(series, issue, incoming)
    assert ET.fromstring(result.metroninfo).find("Credits/Credit/Creator").get("id") == "88"
    assert ET.fromstring(result.metroninfo).find("Credits/Credit/Roles/Role").get("id") == "2"


def test_old_schema_version_is_read_but_new_output_is_schema_11():
    incoming = files(mi=metron().replace("<MetronInfo>", '<MetronInfo version="1.0">'))
    result = render_archive_metadata(*snapshots(), incoming)
    validate_metroninfo_xml(result.metroninfo)
    assert "version" not in ET.fromstring(result.metroninfo).attrib


def test_unknown_role_is_reviewable_not_silently_changed_to_other():
    series, issue = snapshots(credits=({"name": "Creator", "role": "invented role"},))
    with pytest.raises(ArchiveMetadataRenderError, match="invalid_output"):
        render_archive_metadata(series, issue, files())


def test_primary_cannot_be_selected_from_observed_ids():
    series, issue = snapshots()
    issue = issue.model_copy(update={"observed_identities": (METRON_ISSUE,)})
    with pytest.raises(ArchiveMetadataRenderError, match="invalid_primary_identity"):
        render_archive_metadata(series, issue, files(), primary_identity=METRON_ISSUE)


def test_no_provider_identity_is_invented_for_local_metadata():
    series, issue = snapshots()
    series = series.model_copy(update={"identities": ()})
    issue = issue.model_copy(update={"identities": ()})
    result = render_archive_metadata(series, issue, files())
    assert result.comicinfo and result.metroninfo
    assert not parse_metroninfo(result.metroninfo).identities
    assert ET.fromstring(result.metroninfo).find("Series").get("id") is None


def test_partial_calendar_is_preserved_without_inventing_a_day():
    result = render_archive_metadata(
        *snapshots(),
        files(
            ci="<ComicInfo><Year>2026</Year><Month>9</Month></ComicInfo>",
        ),
    )
    ci = ET.fromstring(result.comicinfo)
    assert ci.findtext("Year") == "2026" and ci.findtext("Month") == "9"
    assert ci.find("Day") is None
    assert parse_metroninfo(result.metroninfo).cover_date is None


def test_identity_comments_are_not_lost_during_id_normalization():
    incoming = files(mi=metron('<IDS><ID source="Comic Vine">7<!--verified--></ID></IDS>'))
    result = render_archive_metadata(*snapshots(), incoming)
    assert b"<!--verified-->" in result.metroninfo


def test_known_source_alias_is_normalized_for_output_not_rejected_as_schema_drift():
    incoming = files(mi=metron('<IDS><ID source="ComicVine" primary="true">7</ID></IDS>'))
    result = render_archive_metadata(*snapshots(), incoming)
    assert b'source="Comic Vine"' in result.metroninfo
    validate_metroninfo_xml(result.metroninfo)


def test_preserved_comments_cannot_disappear_when_replacing_a_rich_container():
    series, issue = snapshots(credits=())
    issue = issue.model_copy(update={"origins": (origin("credits", user=True),)})
    incoming = files(
        mi=metron(
            "<Credits><!--private note--><Credit><Creator>Name</Creator>"
            "<Roles><Role>Writer</Role></Roles></Credit></Credits>"
        )
    )
    with pytest.raises(ArchiveMetadataRenderError, match="unsupported_content"):
        render_archive_metadata(series, issue, incoming)


@pytest.mark.parametrize(
    "field,old,new,tag",
    [
        ("series_type", "Single Issue", "Annual", "Format"),
        ("issue_count", 12, 13, "Count"),
    ],
)
def test_reviewed_series_fields_update_both_formats(field, old, new, tag):
    series, issue = snapshots()
    field_origin = origin(field, user=True)
    if field == "issue_count":
        field_origin = field_origin.model_copy(update={"domain": MetadataDomain.ISSUES})
    series = series.model_copy(
        update={
            "values": series.values.model_copy(update={field: new}),
            "origins": (field_origin,),
        }
    )
    result = render_archive_metadata(
        series,
        issue,
        files(ci=f"<ComicInfo><{tag}>{old}</{tag}></ComicInfo>"),
    )
    assert ET.fromstring(result.comicinfo).findtext(tag) == str(new)
    parsed = parse_metroninfo(result.metroninfo)
    assert (parsed.issue_count if field == "issue_count" else parsed.series_format) == new


def test_mismatched_baseline_cannot_authorize_a_refresh():
    series, issue = snapshots(title="New")
    previous = issue.model_copy(
        update={
            "identities": (METRON_ISSUE,),
            "values": MetadataValues(title="Old"),
            "origins": (origin("title"),),
        }
    )
    with pytest.raises(ArchiveMetadataRenderError, match="invalid_baseline"):
        render_archive_metadata(
            series, issue, files(), previous_issue=previous, replace_managed=True
        )


@pytest.mark.parametrize(
    "tag,parent,child",
    [
        ("Genre", "Genres", "Genre"),
        ("Tags", "Tags", "Tag"),
        ("Characters", "Characters", "Character"),
        ("Teams", "Teams", "Team"),
        ("Locations", "Locations", "Location"),
    ],
)
def test_comicinfo_lists_are_carried_to_metron_without_making_up_ids(tag, parent, child):
    result = render_archive_metadata(
        *snapshots(),
        files(
            ci=f"<ComicInfo><{tag}>First, Second</{tag}></ComicInfo>",
        ),
    )
    nodes = ET.fromstring(result.metroninfo).findall(f"{parent}/{child}")
    assert [node.text for node in nodes] == ["First", "Second"]
    assert all(not node.attrib for node in nodes)


@pytest.mark.parametrize(
    "ci_tag,mi_xml",
    [
        ("Genre", "<Genres><Genre>Other</Genre></Genres>"),
        ("Imprint", "<Publisher><Name>Publisher</Name><Imprint>Other</Imprint></Publisher>"),
        ("AgeRating", "<AgeRating>Teen</AgeRating>"),
    ],
)
def test_conflicting_passthrough_fields_are_not_silently_chosen(ci_tag, mi_xml):
    with pytest.raises(ArchiveMetadataRenderError, match="unreconciled_field"):
        render_archive_metadata(
            *snapshots(),
            files(
                ci=f"<ComicInfo><{ci_tag}>First</{ci_tag}></ComicInfo>",
                mi=metron(mi_xml),
            ),
        )


def test_legacy_arc_names_and_positions_are_preserved_in_both_documents():
    result = render_archive_metadata(
        *snapshots(),
        files(
            ci=(
                "<ComicInfo><StoryArc>First, Second</StoryArc>"
                "<StoryArcNumber>2, 5</StoryArcNumber></ComicInfo>"
            )
        ),
    )
    assert [(arc.name, arc.number) for arc in parse_metroninfo(result.metroninfo).arcs] == [
        ("First", 2),
        ("Second", 5),
    ]


@pytest.mark.parametrize(
    "number", ["0", "-1", "2, 3", "9" * 5000], ids=["zero", "negative", "count", "oversized"]
)
def test_unrepresentable_arc_order_has_a_fixed_review_error(number):
    with pytest.raises(ArchiveMetadataRenderError, match="unsupported_content"):
        render_archive_metadata(
            *snapshots(),
            files(
                ci=(
                    "<ComicInfo><StoryArc>First</StoryArc>"
                    f"<StoryArcNumber>{number}</StoryArcNumber></ComicInfo>"
                )
            ),
        )


def test_arc_order_disagreement_requires_review():
    with pytest.raises(ArchiveMetadataRenderError, match="unreconciled_field"):
        render_archive_metadata(
            *snapshots(),
            files(
                ci="<ComicInfo><StoryArc>First</StoryArc><StoryArcNumber>2</StoryArcNumber></ComicInfo>",
                mi=metron("<Arcs><Arc><Name>First</Name><Number>3</Number></Arc></Arcs>"),
            ),
        )


def test_complementary_arc_order_fills_metron_gap_without_reidentification():
    result = render_archive_metadata(
        *snapshots(),
        files(
            ci="<ComicInfo><StoryArc>First</StoryArc><StoryArcNumber>2</StoryArcNumber></ComicInfo>",
            mi=metron("<Arcs><Arc><Name>First</Name></Arc></Arcs>"),
        ),
    )
    assert parse_metroninfo(result.metroninfo).arcs[0].number == 2


def test_counts_are_bounded_before_integer_conversion():
    with pytest.raises(ArchiveMetadataRenderError, match="invalid_input"):
        render_archive_metadata(
            *snapshots(), files(ci=("<ComicInfo><Count>" + "9" * 5000 + "</Count></ComicInfo>"))
        )


def test_explicit_empty_credits_remain_explicit_in_both_formats():
    result = render_archive_metadata(*snapshots(credits=()), files())
    parsed = reconcile_archive_metadata(
        files(result.comicinfo.decode(), result.metroninfo.decode())
    )
    assert parsed.comicinfo.issue.credits == parsed.metroninfo.issue.credits == ()


def test_real_archive_can_be_read_once_rendered_and_left_unchanged(tmp_path):
    import hashlib
    import zipfile

    from pullbox.core.archive_metadata import read_open_zip_metadata

    path = tmp_path / "example.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("page.jpg", b"untouched image fixture")
        archive.writestr("ComicInfo.xml", "<ComicInfo><Title>Local title</Title></ComicInfo>")
    before = hashlib.sha256(path.read_bytes()).digest()
    with zipfile.ZipFile(path) as archive:
        source = read_open_zip_metadata(archive)
        result = render_archive_metadata(*snapshots(title="Local title"), source)
    assert parse_metroninfo(result.metroninfo).stories == ("Local title",)
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_passive_primary_context_and_series_id_survive_without_issue_attachment():
    series, issue = snapshots()
    passive_series = ExternalIdentityRef(Namespace.LOCG, Kind.SERIES, "99")
    series = series.model_copy(update={"identities": (passive_series,)})
    issue = issue.model_copy(update={"identities": ()})
    incoming = files(
        mi=metron(
            '<IDS><ID source="League of Comic Geeks" primary="true">123</ID></IDS>',
            attrs='id="99"',
        )
    )
    result = render_archive_metadata(series, issue, incoming)
    parsed = parse_metroninfo(result.metroninfo)
    assert parsed.primary_source == Namespace.LOCG
    assert {e.identity for e in parsed.evidence} == {passive_series}
    assert parsed.release_references[0].external_id == "123"


def test_managed_rich_credit_refresh_compares_the_comicinfo_projection():
    series, previous = snapshots(credits=({"name": "Old", "role": "writer, artist"},))
    previous = previous.model_copy(update={"origins": (origin("credits"),)})
    initial = render_archive_metadata(series, previous, files())
    incoming = files(initial.comicinfo.decode(), initial.metroninfo.decode())
    _, issue = snapshots(credits=({"name": "New", "role": "writer, artist"},))
    issue = issue.model_copy(update={"origins": (origin("credits"),)})
    result = render_archive_metadata(
        series, issue, incoming, previous_issue=previous, replace_managed=True
    )
    assert ET.fromstring(result.comicinfo).findtext("Writer") == "New"
    assert parse_metroninfo(result.metroninfo).credits[0].creator.value == "New"


@pytest.mark.parametrize(
    "canonical,expected",
    [
        ("standard", "Single Issue"),
        ("tpb", "Trade Paperback"),
        ("hardcover", "Hardcover"),
        ("one_shot", "One-Shot"),
        ("annual", "Annual"),
        ("omnibus", "Omnibus"),
        ("graphic_novel", "Graphic Novel"),
        ("Limited Series", "Limited Series"),
    ],
)
def test_canonical_application_types_serialize_as_schema_formats(canonical, expected):
    series, issue = snapshots()
    series = series.model_copy(
        update={"values": series.values.model_copy(update={"series_type": canonical})}
    )
    result = render_archive_metadata(series, issue, files())
    assert ET.fromstring(result.comicinfo).findtext("Format") == expected
    assert parse_metroninfo(result.metroninfo).series_format == expected
    assert (
        render_archive_metadata(
            series, issue, files(result.comicinfo.decode(), result.metroninfo.decode())
        )
        == result
    )


def test_generic_type_does_not_erase_a_more_specific_existing_format():
    series, issue = snapshots()
    series = series.model_copy(
        update={"values": series.values.model_copy(update={"series_type": "standard"})}
    )
    incoming = files(
        mi=metron(series="<Name>Example &amp; Friends</Name><Format>Limited Series</Format>")
    )
    with pytest.raises(ArchiveMetadataRenderError, match="unreconciled_field"):
        render_archive_metadata(series, issue, incoming)


@pytest.mark.parametrize("kind", ["special", "compendium", "deluxe", "volume"])
def test_types_without_a_metron_equivalent_remain_in_comicinfo(kind):
    series, issue = snapshots()
    series = series.model_copy(
        update={"values": series.values.model_copy(update={"series_type": kind})}
    )
    result = render_archive_metadata(series, issue, files())
    assert ET.fromstring(result.comicinfo).findtext("Format") == kind.title()
    assert ET.fromstring(result.metroninfo).find("Series/Format") is None
    validate_metroninfo_xml(result.metroninfo)


def test_schema_year_uses_four_digits_without_changing_the_canonical_year():
    series, issue = snapshots()
    series = series.model_copy(
        update={"values": series.values.model_copy(update={"year_start": 1})}
    )
    result = render_archive_metadata(series, issue, files())
    assert ET.fromstring(result.metroninfo).findtext("Series/StartYear") == "0001"
