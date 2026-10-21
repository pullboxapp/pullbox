"""Rich embedded values remain bounded evidence, not provider identity claims."""

from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

import pytest

from pullbox.core.metroninfo import MetronInfoDiagnosticCode as Code
from pullbox.core.metroninfo import parse_metroninfo

FIXTURE = Path(__file__).parents[1] / "fixtures" / "metroninfo" / "descriptive.xml"


def _xml(extra: str = "", series: str = "<Series><Name>Harbor Lights</Name></Series>") -> str:
    return f"<MetronInfo>{series}{extra}</MetronInfo>"


def _resource(value: str, resource_id: str | None = None, language: str | None = None) -> dict:
    return {"value": value, "resource_id": resource_id, "language": language}


def test_rich_document_preserves_descriptive_fields_and_resource_attributes() -> None:
    data = parse_metroninfo(FIXTURE.read_bytes())
    values = asdict(data)
    expected = {
        "sort_name": "Harbor Lights, The",
        "language": "fr",
        "volume_count": 3,
        "publisher_id": "press:7",
        "imprint": _resource("Night Line", "imprint:8"),
        "alternative_names": (
            _resource("Les lumieres", "name:9", "fr"),
            _resource("Harbour Lights"),
        ),
        "collection_title": "Collected Nights",
        "manga_volume": "02",
        "summary": "A storm arrives.\nThe harbor waits.",
        "notes": "Reader notes & preserved context.",
        "page_count": 1200,
        "age_rating": "Teen Plus",
        "story_resources": (_resource("New Shores", "story:10"), _resource("Homeward")),
        "genres": (_resource("Adventure", "genre:11"),),
        "tags": (_resource("Coastal", "tag:12"),),
        "characters": (_resource("Captain Example", "character:13"),),
        "teams": (_resource("Harbor Watch", "team:14"),),
        "locations": (_resource("The Harbor", "location:15"),),
        "reprints": (_resource("First edition", "issue:16"),),
        "credits": (
            {
                "creator": _resource("Alex Example", "creator:17"),
                "roles": (_resource("Writer", "role:18"), _resource("Artist")),
            },
            {"creator": _resource("Sam Example"), "roles": ()},
        ),
    }
    assert {key: values.get(key) for key in expected} == expected
    assert data.stories == ("New Shores", "Homeward")
    assert not data.diagnostics and not data.has_unmapped_content
    assert {
        (item.identity.namespace.value, item.identity.entity_kind.value, item.identity.external_id)
        for item in data.evidence
    } == {("metron", "issue", "101"), ("comicvine", "issue", "202"), ("metron", "series", "21")}


@pytest.mark.parametrize("version", ["", ' version="1.0"', ' version="1.1"'])
def test_rich_read_is_compatible_without_inventing_missing_language_or_credits(
    version: str,
) -> None:
    data = asdict(parse_metroninfo(_xml().replace("<MetronInfo>", f"<MetronInfo{version}>")))
    assert "credits" in data and data["credits"] is None
    assert "language" in data and data["language"] is None
    assert asdict(parse_metroninfo(_xml("<Credits/>"))).get("credits") == ()


def test_credits_preserve_unknown_roles_and_unclassified_people_without_identity_context() -> None:
    data = parse_metroninfo(
        _xml(
            '<Credits><Credit><Creator id="0017">Same Name</Creator><Roles>'
            '<Role id="local-role">Experimental Role</Role></Roles></Credit>'
            '<Credit><Creator id="0018">Same Name</Creator><Roles/></Credit></Credits>'
        )
    )
    assert asdict(data).get("credits") == (
        {
            "creator": _resource("Same Name", "0017"),
            "roles": (_resource("Experimental Role", "local-role"),),
        },
        {"creator": _resource("Same Name", "0018"), "roles": ()},
    )
    assert data.evidence == () and data.primary_source is None
    assert not data.diagnostics


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("PageCount", "0", 0),
        ("PageCount", "1200", 1200),
        ("PageCount", "-1", None),
        ("PageCount", "1.5", None),
        ("PageCount", "2147483648", None),
    ],
)
def test_page_counts_are_bounded_nonnegative_integers(
    field: str, value: str, expected: int | None
) -> None:
    data = parse_metroninfo(_xml(f"<{field}>{value}</{field}>"))
    assert "page_count" in asdict(data) and asdict(data)["page_count"] == expected
    assert (Code.INVALID_FIELD in {item.code for item in data.diagnostics}) == (expected is None)


@pytest.mark.parametrize(
    "xml,locator",
    [
        ("<Summary>first</Summary><Summary>second</Summary>", "Summary"),
        ("<Credits/><Credits/>", "Credits"),
        (
            "<Credits><Credit><Creator>A</Creator><Creator>B</Creator></Credit></Credits>",
            "Credits/Credit[1]/Creator",
        ),
        (
            "<Credits><Credit><Creator>A</Creator><Roles/><Roles/></Credit></Credits>",
            "Credits/Credit[1]/Roles",
        ),
        ("<Tags/><Tags/>", "Tags"),
    ],
)
def test_duplicate_rich_fields_remain_ambiguous(xml: str, locator: str) -> None:
    data = parse_metroninfo(_xml(xml))
    assert (Code.AMBIGUOUS_FIELD, locator) in {
        (item.code, item.locator) for item in data.diagnostics
    }
    assert data.has_unmapped_content


@pytest.mark.parametrize(
    "xml,locator",
    [
        (
            "<Credits><Credit><Roles><Role>Writer</Role></Roles></Credit></Credits>",
            "Credits/Credit[1]/Creator",
        ),
        ("<Credits><Credit><Creator> </Creator></Credit></Credits>", "Credits/Credit[1]/Creator"),
        (
            "<Credits><Credit><Creator><Name>A</Name></Creator></Credit></Credits>",
            "Credits/Credit[1]/Creator",
        ),
        ('<Tags><Tag id="' + "x" * 4097 + '">A</Tag></Tags>', "Tags/Tag[1]/@id"),
        ("<Summary>" + "x" * 200001 + "</Summary>", "Summary"),
    ],
)
def test_malformed_rich_fields_report_safe_locators(xml: str, locator: str) -> None:
    data = parse_metroninfo(_xml(xml))
    assert (Code.INVALID_FIELD, locator) in {(item.code, item.locator) for item in data.diagnostics}
    assert "x" * 100 not in repr(data.diagnostics)


@pytest.mark.parametrize("lang", ["en-US", "ENG", "E1", "../", ""])
def test_invalid_series_language_is_reported_not_inferred(lang: str) -> None:
    data = parse_metroninfo(_xml(series=f'<Series lang="{lang}"><Name>A</Name></Series>'))
    assert (Code.INVALID_FIELD, "Series/@lang") in {
        (item.code, item.locator) for item in data.diagnostics
    }
    assert asdict(data).get("language") is None


@pytest.mark.parametrize(
    "xml",
    [
        '<Credits><Credit><Creator extra="unknown">A</Creator></Credit></Credits>',
        '<Credits><Credit extra="unknown"><Creator>A</Creator></Credit></Credits>',
        '<Tags><Tag lang="fr">A</Tag></Tags>',
        "<Credits><Credit><Creator>A</Creator><Roles><Role>Writer</Role><FutureRole>A</FutureRole></Roles></Credit></Credits>",
        "<Summary>Known</Summary><Extension>Preserve or stop</Extension>",
        "<Tags><Tag>A</Tag>mixed tail</Tags>",
    ],
)
def test_unknown_rich_content_still_prevents_claiming_lossless_rewrite(xml: str) -> None:
    data = parse_metroninfo(_xml(xml))
    assert data.has_unmapped_content and Code.UNMAPPED_CONTENT in {
        item.code for item in data.diagnostics
    }


@pytest.mark.parametrize("count", [128, 129])
def test_credit_limit_rejects_entire_list_instead_of_truncating(count: int) -> None:
    data = parse_metroninfo(
        _xml("<Credits>" + "<Credit><Creator>A</Creator></Credit>" * count + "</Credits>")
    )
    assert "credits" in asdict(data)
    assert len(asdict(data)["credits"]) == (count if count == 128 else 0)
    assert (Code.COMPLEXITY_LIMIT in {item.code for item in data.diagnostics}) == (count > 128)


@pytest.mark.parametrize("count", [32, 33])
def test_role_limit_never_returns_a_truncated_credit(count: int) -> None:
    data = parse_metroninfo(
        _xml(
            "<Credits><Credit><Creator>A</Creator><Roles>"
            + "<Role>Writer</Role>" * count
            + "</Roles></Credit></Credits>"
        )
    )
    assert "credits" in asdict(data)
    if count == 32:
        assert len(asdict(data)["credits"][0]["roles"]) == 32
    else:
        assert asdict(data)["credits"] == ()
        assert Code.COMPLEXITY_LIMIT in {item.code for item in data.diagnostics}


def test_rich_data_is_deeply_immutable() -> None:
    data = parse_metroninfo(FIXTURE.read_bytes())
    assert asdict(data).get("credits")
    with pytest.raises(FrozenInstanceError):
        data.credits[0].creator.value = "Other"


def test_rich_read_cannot_fetch_resource_ids_or_write_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = FIXTURE.read_bytes()

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Descriptive metadata attempted external I/O")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    data = parse_metroninfo(payload)
    assert data.credits and data.credits[0].creator.resource_id == "creator:17"
    assert not data.diagnostics


@pytest.mark.parametrize(
    "roles", ["<Role>Writer</Role><Role/>", "<Role>Writer</Role><Role><Name>Artist</Name></Role>"]
)
def test_invalid_role_does_not_publish_partial_credit_list(roles: str) -> None:
    data = parse_metroninfo(
        _xml(
            "<Credits><Credit><Creator>Good Person</Creator></Credit>"
            f"<Credit><Creator>Second Person</Creator><Roles>{roles}</Roles></Credit></Credits>"
        )
    )
    assert data.credits == ()
    assert Code.INVALID_FIELD in {item.code for item in data.diagnostics}
