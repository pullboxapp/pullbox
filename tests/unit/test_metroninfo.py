"""Synthetic MetronInfo contracts based on the upstream 1.0 and 1.1 schemas."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path

import pytest

from pullbox.core.metadata_identity import (
    IdentityEvidenceKind,
    IdentityNamespace,
    MetadataEntityKind,
)
from pullbox.core.metroninfo import (
    MAX_METRONINFO_BYTES,
    MAX_METRONINFO_DEPTH,
    MAX_METRONINFO_NODES,
    MetronInfoData,
    parse_metroninfo,
)
from pullbox.core.metroninfo import (
    MetronInfoDiagnosticCode as Code,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "metroninfo"


def _xml(
    ids: str = '<ID source="Metron" primary="true">101</ID>',
    *,
    series: str = '<Series id="21"><Name>Harbor Lights</Name></Series>',
    extra: str = "",
    version: str = "",
) -> str:
    return f"<MetronInfo{version}><IDS>{ids}</IDS>{series}<Number>13a</Number>{extra}</MetronInfo>"


def _keys(data: MetronInfoData) -> set[tuple[str, str, str]]:
    return {
        (item.identity.namespace.value, item.identity.entity_kind.value, item.identity.external_id)
        for item in data.evidence
    }


def _codes(data: MetronInfoData) -> set[Code]:
    return {item.code for item in data.diagnostics}


@pytest.mark.parametrize("length", [4097, 11531, 200000])
def test_long_summary_retains_full_supported_description(length: int) -> None:
    summary = "S" * length
    data = parse_metroninfo(_xml(extra=f"<Summary>{summary}</Summary>"))
    assert data.summary == summary
    assert data.diagnostics == ()


@pytest.mark.parametrize(
    "extra,locator",
    [
        (f"<Summary>{'S' * 200001}</Summary>", "Summary"),
        ("<Summary><Unexpected>text</Unexpected></Summary>", "Summary"),
        (f"<Notes>{'N' * 4097}</Notes>", "Notes"),
    ],
)
def test_summary_allowance_does_not_relax_other_limits(extra: str, locator: str) -> None:
    data = parse_metroninfo(_xml(extra=extra))
    assert any(d.code is Code.INVALID_FIELD and d.locator == locator for d in data.diagnostics)


def test_v1_0_core_fields_and_parent_identity_use_primary_source() -> None:
    data = parse_metroninfo((FIXTURES / "v1_0.xml").read_bytes())
    assert data.series == "Harbor Lights"
    assert data.number == "13a" and data.publisher == "Example Press"
    assert (data.volume, data.start_year, data.issue_count, data.series_format) == (
        2,
        2020,
        12,
        "Limited Series",
    )
    assert data.cover_date == date(2020, 6, 1) and data.store_date == date(2020, 4, 15)
    assert data.stories == ("New Shores", "Homeward")
    assert _keys(data) == {("metron", "series", "21"), ("metron", "issue", "101")}
    assert data.primary_source is IdentityNamespace.METRON
    assert all(item.evidence_kind is IdentityEvidenceKind.METRONINFO_XML for item in data.evidence)
    assert all(item.source_instance is None for item in data.evidence)
    assert data.diagnostics == ()


def test_v1_1_multi_source_ids_preserve_lettered_number_and_locg_discovery_scope() -> None:
    data = parse_metroninfo((FIXTURES / "v1_1.xml").read_bytes())
    assert data.number == "50-x" and data.alternative_number == "150"
    assert _keys(data) == {
        ("metron", "series", "21"),
        ("metron", "issue", "101"),
        ("comicvine", "issue", "202"),
        ("gcd", "issue", "303"),
    }
    assert [item.external_id for item in data.release_references] == ["404"]
    assert not data.release_references[0].primary
    assert data.has_unmapped_content and Code.UNMAPPED_CONTENT in _codes(data)


@pytest.mark.parametrize(
    "source,namespace",
    [
        ("Comic Vine", "comicvine"),
        ("comicvine", "comicvine"),
        (" GCD ", "gcd"),
        ("Grand Comics Database", "gcd"),
        ("metron", "metron"),
        ("LOCG", "locg"),
        ("League of Comic Geeks", "locg"),
    ],
)
def test_known_source_aliases_assign_series_id_only_to_declared_provider(
    source: str, namespace: str
) -> None:
    data = parse_metroninfo(_xml(f'<ID source="{source}" primary="1">00101</ID>'))
    assert (namespace, "series", "21") in _keys(data)
    assert data.primary_source.value == namespace
    if namespace == "locg":
        assert len(data.evidence) == 1 and data.release_references[0].primary
    else:
        assert (namespace, "issue", "101") in _keys(data)


@pytest.mark.parametrize(
    "ids,code",
    [
        ('<ID source="Metron">101</ID>', Code.MISSING_PRIMARY),
        ('<ID source="Metron" primary="yes">101</ID>', Code.INVALID_PRIMARY),
        (
            '<ID source="Metron" primary="true">101</ID>'
            '<ID source="Comic Vine" primary="true">202</ID>',
            Code.AMBIGUOUS_PRIMARY,
        ),
        (
            '<ID source="Unknown" primary="true">1</ID><ID source="Metron">101</ID>',
            Code.UNKNOWN_SOURCE,
        ),
    ],
)
def test_ambiguous_or_absent_primary_does_not_guess_parent_provider(ids: str, code: Code) -> None:
    data = parse_metroninfo(_xml(ids))
    assert data.series == "Harbor Lights"
    assert data.primary_source is None
    assert all(item.identity.entity_kind is MetadataEntityKind.ISSUE for item in data.evidence)
    assert code in _codes(data)


def test_duplicate_identical_ids_coalesce_but_primary_flag_is_retained() -> None:
    data = parse_metroninfo(
        _xml('<ID source="Metron">00101</ID><ID source="Metron" primary="true">101</ID>')
    )
    assert len(data.identities) == 2
    assert all(item.primary for item in data.identities)


def test_repeated_primary_declarations_remain_ambiguous_even_with_same_id() -> None:
    data = parse_metroninfo(_xml('<ID source="Metron" primary="true">101</ID>' * 2))
    assert data.primary_source is None
    assert _keys(data) == {("metron", "issue", "101")}
    assert Code.AMBIGUOUS_PRIMARY in _codes(data)


def test_exact_id_conflicts_keep_both_values_instead_of_primary_winning() -> None:
    data = parse_metroninfo(
        _xml('<ID source="Metron" primary="true">101</ID><ID source="Metron">102</ID>')
    )
    assert {("metron", "issue", "101"), ("metron", "issue", "102")} <= _keys(data)
    assert Code.EXACT_ID_CONFLICT in _codes(data)


@pytest.mark.parametrize(
    "value", ["0", "-1", "1.5", "13a", "https://secret.invalid/?token=hidden", "1" * 256, "\u0661"]
)
def test_invalid_id_is_diagnosed_without_echoing_raw_input(value: str) -> None:
    data = parse_metroninfo(_xml(f'<ID source="Metron" primary="true">{value}</ID>'))
    assert not any(item.identity.entity_kind is MetadataEntityKind.ISSUE for item in data.evidence)
    assert Code.INVALID_ID in _codes(data)
    assert value not in repr(data.diagnostics)


def test_unknown_sources_are_diagnostics_not_invented_provider_keys() -> None:
    data = parse_metroninfo(
        _xml(
            '<ID source="MangaDex">a-b-c</ID><ID source="private-token">5</ID>'
            '<ID source="Metron" primary="true">101</ID>'
        )
    )
    assert _keys(data) == {("metron", "series", "21"), ("metron", "issue", "101")}
    assert Code.UNKNOWN_SOURCE in _codes(data)
    assert "private-token" not in repr(data.diagnostics)


def test_story_arcs_are_separate_from_issue_identity_and_keep_reading_order() -> None:
    data = parse_metroninfo((FIXTURES / "v1_0.xml").read_bytes())
    arc = data.arcs[0]
    assert (arc.name, arc.number) == ("The Crossing", 2)
    assert arc.evidence.identity.entity_kind is MetadataEntityKind.STORY_ARC
    assert arc.evidence.identity.external_id == "91"
    assert not any(
        item.identity.entity_kind is MetadataEntityKind.STORY_ARC for item in data.evidence
    )


@pytest.mark.parametrize("version", ["1.0", "1.1"])
def test_explicit_compatible_version_does_not_hide_identity(version: str) -> None:
    assert len(parse_metroninfo(_xml(version=f' version="{version}"')).evidence) == 2


def test_unknown_version_retains_core_text_without_guessing_id_semantics() -> None:
    data = parse_metroninfo(_xml(version=' version="9.0"'))
    assert data.series == "Harbor Lights" and data.number == "13a"
    assert data.evidence == () and data.primary_source is None
    assert Code.UNSUPPORTED_VERSION in _codes(data)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"])
def test_approved_unicode_encodings_are_supported(encoding: str) -> None:
    declaration = "UTF-16" if encoding.startswith("utf-16") else "UTF-8"
    payload = f'<?xml version="1.0" encoding="{declaration}"?>{_xml()}'.encode(encoding)
    assert parse_metroninfo(payload).series == "Harbor Lights"


@pytest.mark.parametrize(
    "payload,code",
    [
        (b"<MetronInfo>", Code.INVALID_XML),
        (b"<ComicInfo />", Code.INVALID_ROOT),
        (b'<MetronInfo xmlns="https://unknown.invalid"/>', Code.INVALID_ROOT),
        (b"<!DOCTYPE MetronInfo><MetronInfo/>", Code.UNSAFE_XML),
        (
            b'<!DOCTYPE MetronInfo [<!ENTITY secret SYSTEM "file:///private/secret">]><MetronInfo><Number>&secret;</Number></MetronInfo>',
            Code.UNSAFE_XML,
        ),
        (
            b'<!DOCTYPE MetronInfo SYSTEM "https://example.invalid/schema"><MetronInfo/>',
            Code.UNSAFE_XML,
        ),
        (
            b'<MetronInfo xmlns:x="http://www.w3.org/2001/XInclude"><x:include href="file:///private/secret"/></MetronInfo>',
            Code.UNSAFE_XML,
        ),
        (b'<?xml version="1.0" encoding="UTF-16"?><MetronInfo/>', Code.INVALID_XML),
        ("<MetronInfo>\ud800</MetronInfo>", Code.INVALID_XML),
    ],
)
def test_unsafe_or_invalid_xml_returns_safe_diagnostics_not_partial_evidence(
    payload: bytes | str, code: Code
) -> None:
    data = parse_metroninfo(payload)
    assert data.evidence == () and data.series is None
    assert code in _codes(data)
    assert "secret" not in repr(data.diagnostics) and "private" not in repr(data.diagnostics)


@pytest.mark.parametrize(
    "payload",
    [b" " * (MAX_METRONINFO_BYTES + 1), "\u00e9" * (MAX_METRONINFO_BYTES // 2 + 1)],
    ids=["bytes", "unicode"],
)
def test_input_byte_limit_precedes_parsing(payload: bytes | str) -> None:
    assert Code.TOO_LARGE in _codes(parse_metroninfo(payload))


def test_depth_and_node_limits_fail_closed() -> None:
    deep = (
        "<MetronInfo>"
        + "<Nested>" * MAX_METRONINFO_DEPTH
        + "</Nested>" * MAX_METRONINFO_DEPTH
        + "</MetronInfo>"
    )
    wide = "<MetronInfo>" + "<Unknown/>" * MAX_METRONINFO_NODES + "</MetronInfo>"
    assert Code.COMPLEXITY_LIMIT in _codes(parse_metroninfo(deep))
    assert Code.COMPLEXITY_LIMIT in _codes(parse_metroninfo(wide))


def test_remote_schema_hint_is_never_fetched() -> None:
    data = parse_metroninfo(
        _xml().replace(
            "<MetronInfo>",
            '<MetronInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xsi:noNamespaceSchemaLocation="https://secret.invalid/schema" >',
        )
    )
    assert data.series == "Harbor Lights" and len(data.evidence) == 2
    assert "secret" not in repr(data.diagnostics)


def test_duplicate_series_nodes_keep_issue_ids_but_do_not_choose_a_parent() -> None:
    data = parse_metroninfo(
        _xml(
            series='<Series id="21"><Name>One</Name></Series>'
            '<Series id="22"><Name>Two</Name></Series>'
        )
    )
    assert data.series is None
    assert _keys(data) == {("metron", "issue", "101")}
    assert Code.AMBIGUOUS_FIELD in _codes(data)


def test_bad_optional_fields_do_not_erase_valid_identity() -> None:
    data = parse_metroninfo(
        _xml(
            series='<Series id="21"><Name>Harbor Lights</Name>'
            "<StartYear>bad</StartYear><IssueCount>-1</IssueCount></Series>",
            extra="<CoverDate>2020-02-31</CoverDate>",
        )
    )
    assert data.start_year is None and data.issue_count is None and data.cover_date is None
    assert len(data.evidence) == 2 and Code.INVALID_FIELD in _codes(data)


def test_results_are_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        parse_metroninfo(_xml()).series = "Changed"


@pytest.mark.parametrize("timezone", ["Z", "+00:00", "-12:30", "+14:00"])
def test_xsd_calendar_dates_and_years_preserve_local_calendar_with_timezone(timezone: str) -> None:
    data = parse_metroninfo(
        _xml(
            series="<Series><Name>Harbor Lights</Name>"
            f"<StartYear>2020{timezone}</StartYear></Series>",
            extra=f"<CoverDate>2020-06-01{timezone}</CoverDate>",
        )
    )
    assert data.start_year == 2020 and data.cover_date == date(2020, 6, 1)


@pytest.mark.parametrize("year", ["2", "020", "+2020", "2020+14:01", "2020+25:00"])
def test_invalid_calendar_year_is_not_silently_accepted(year: str) -> None:
    data = parse_metroninfo(
        _xml(series=f"<Series><Name>Harbor Lights</Name><StartYear>{year}</StartYear></Series>")
    )
    assert data.start_year is None and Code.INVALID_FIELD in _codes(data)


@pytest.mark.parametrize(
    "extra",
    [
        "unexpected text",
        "<Extension><Data>retained elsewhere</Data></Extension>",
        '<Series extra="unknown"><Name>Other</Name></Series>',
    ],
)
def test_unmapped_content_cannot_be_silently_lost_by_a_future_writer(extra: str) -> None:
    data = parse_metroninfo(_xml(extra=extra))
    assert data.has_unmapped_content and Code.UNMAPPED_CONTENT in _codes(data)


def test_unknown_series_mixed_content_is_reported_without_hiding_valid_fields() -> None:
    data = parse_metroninfo(
        _xml(series='<Series id="21">unmapped text<Name>Harbor Lights</Name></Series>')
    )
    assert data.series == "Harbor Lights" and len(data.evidence) == 2
    assert data.has_unmapped_content


@pytest.mark.parametrize("encoding", ["iso-8859-1", "utf-7"])
def test_unapproved_declared_encodings_are_not_decoded(encoding: str) -> None:
    payload = f'<?xml version="1.0" encoding="{encoding}"?>{_xml()}'.encode(encoding)
    assert Code.UNSUPPORTED_ENCODING in _codes(parse_metroninfo(payload))


def test_unicode_input_with_utf16_declaration_is_already_decoded() -> None:
    assert (
        parse_metroninfo(f'<?xml version="1.0" encoding="UTF-16"?>{_xml()}').series
        == "Harbor Lights"
    )


def test_attribute_count_is_bounded() -> None:
    attributes = " ".join(f'a{index}="value"' for index in range(17))
    assert Code.COMPLEXITY_LIMIT in _codes(parse_metroninfo(f"<MetronInfo {attributes}/>"))


def test_duplicate_scalar_fields_are_reported_instead_of_picking_the_first() -> None:
    data = parse_metroninfo(_xml(extra="<Number>13b</Number>"))
    assert data.number is None and Code.AMBIGUOUS_FIELD in _codes(data)


def test_reading_order_survives_missing_identity_context() -> None:
    data = parse_metroninfo(
        _xml(
            '<ID source="Metron">101</ID>',
            extra='<Arcs><Arc id="91"><Name>The Crossing</Name><Number>2</Number></Arc></Arcs>',
        )
    )
    assert data.arcs[0].name == "The Crossing" and data.arcs[0].number == 2
    assert data.arcs[0].evidence is None and Code.MISSING_PRIMARY in _codes(data)


def test_parser_cannot_make_network_or_filesystem_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Local metadata parser attempted external I/O")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    assert parse_metroninfo(_xml()).series == "Harbor Lights"
