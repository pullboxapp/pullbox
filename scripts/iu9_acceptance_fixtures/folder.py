"""Deterministic real-world folder-import fixture generator."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from typing import TYPE_CHECKING

from .shared import (
    consume_cbr_seed_set,
    create_deterministic_cbz,
    create_deterministic_zip,
    deterministic_jpeg,
    prepare_fixture_root,
    snapshot_tree,
    write_bytes,
    write_manifest,
    write_text,
)

if TYPE_CHECKING:
    from pathlib import Path

_SCHEMA_VERSION = 1


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _synthetic_series_id(series: str) -> int:
    digest = hashlib.sha256(series.casefold().encode("utf-8")).digest()
    return 500_000 + (int.from_bytes(digest[:4], "big") % 400_000)


def _case(
    case_id: str,
    *,
    paths: list[str],
    expected_outcome: str,
    tags: list[str],
    issue_number: str | None = None,
    archive_format: str | None = None,
    note: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "id": case_id,
        "paths": paths,
        "expected_outcome": expected_outcome,
        "tags": tags,
    }
    if issue_number is not None:
        row["issue_number"] = issue_number
    if archive_format is not None:
        row["archive_format"] = archive_format
    if note is not None:
        row["note"] = note
    return row


def generate_folder_fixture(
    output_root: Path,
    *,
    seed: int = 1300,
    cbr_seed_dir: Path | None = None,
    cbr_expected_sha256: dict[str, str] | None = None,
) -> Path:
    """Generate resettable folder trees and a relative acceptance manifest."""
    root = prepare_fixture_root(output_root)
    source_a = root / "roots" / "source-a"
    source_b = root / "roots" / "source-b"
    source_a.mkdir(parents=True)
    source_b.mkdir(parents=True)
    cases: list[dict[str, object]] = []

    def add_cbz(
        case_id: str,
        relative_path: str,
        *,
        series: str,
        number: str,
        title: str | None = None,
        year: int = 2026,
        publisher: str = "Fixture House",
        expected_outcome: str = "success",
        tags: list[str] | None = None,
        metadata_number: str | None = None,
        comicvine_series_id: int | None = None,
        comicvine_issue_id: int | None = None,
        include_comicinfo: bool = True,
        identity_source: str = "comicinfo",
    ) -> Path:
        path = root / relative_path
        resolved_series_id = comicvine_series_id or _synthetic_series_id(series)
        resolved_issue_id = comicvine_issue_id or 700_000 + len(cases)
        create_deterministic_cbz(
            path,
            seed=seed,
            case_id=case_id,
            series=series,
            number=metadata_number or number,
            title=title,
            year=year,
            publisher=publisher,
            comicvine_series_id=resolved_series_id if include_comicinfo else None,
            comicvine_issue_id=resolved_issue_id if include_comicinfo else None,
            include_comicinfo=include_comicinfo,
        )
        case = _case(
            case_id,
            paths=[relative_path],
            expected_outcome=expected_outcome,
            tags=tags or [],
            issue_number=number,
            archive_format="cbz",
        )
        if metadata_number is not None:
            case["comicinfo_number"] = metadata_number
        case["metadata_profile"] = "canonical-comicinfo" if include_comicinfo else "pages-only"
        case["identity_source"] = identity_source
        if include_comicinfo:
            case["comicvine_series_id"] = resolved_series_id
            case["comicvine_issue_id"] = resolved_issue_id
        cases.append(case)
        return path

    issue_only = add_cbz(
        "issue_only_filename",
        "roots/source-a/Absolute Batman/Issue 01.cbz",
        series="Absolute Batman",
        number="1",
        title="The Zoo",
        year=2024,
        publisher="DC Comics",
        expected_outcome="review",
        tags=[
            "series-folder",
            "filename-without-series",
            "minimal-layout",
            "metadata-poor",
        ],
        include_comicinfo=False,
        identity_source="folder-and-filename",
    )
    add_cbz(
        "publisher_series_layout",
        "roots/source-a/DC Comics/Absolute Batman/Issue 02.cbz",
        series="Absolute Batman",
        number="2",
        title="The Bat",
        year=2024,
        publisher="DC Comics",
        expected_outcome="review",
        tags=[
            "publisher-folder",
            "series-folder",
            "filename-without-series",
            "metadata-poor",
        ],
        include_comicinfo=False,
        identity_source="publisher-folder-and-filename",
    )
    add_cbz(
        "issue_title_filename",
        ("roots/source-a/Batman (2011)/Batman The Court of Owls, Part One Issue 001.cbz"),
        series="Batman",
        number="1",
        title="The Court of Owls, Part One",
        year=2011,
        publisher="DC Comics",
        expected_outcome="review",
        tags=["issue-title", "issue-token", "series-year-folder", "metadata-poor"],
        include_comicinfo=False,
        identity_source="filename",
    )

    number_cases = (
        ("number_zero", "Issue 0.cbz", "0"),
        ("number_dot_five", "Issue 0.5.cbz", "0.5"),
        ("number_leading_dot_five", "Issue .5.cbz", ".5"),
        ("number_half", "Issue One Half.cbz", "1/2"),
        ("number_suffix", "Issue 1A.cbz", "1A"),
        ("number_ten_thousand", "Issue 10000.cbz", "10000"),
        ("number_one_million", "Issue 1000000.cbz", "1000000"),
    )
    for case_id, filename, number in number_cases:
        add_cbz(
            case_id,
            f"roots/source-a/Number Lab (2026)/{filename}",
            series="Number Lab",
            number=number,
            title=f"Number {number}",
            tags=["issue-number-edge", "exact-text"],
        )

    add_cbz(
        "unicode_punctuation",
        "roots/source-a/Étoiles & L\u2019ombre (2025)/Étoiles \u2014 Issue 001 (日本語).cbz",
        series="Étoiles & L\u2019ombre",
        number="1",
        title="L\u2019été \u2014 日本語",
        year=2025,
        tags=["unicode", "punctuation", "ampersand", "apostrophe"],
    )
    add_cbz(
        "nested_generic_containers",
        "roots/source-a/Comics/Archive/By Publisher/Indie/Deep Series/Issue 003.cbz",
        series="Deep Series",
        number="3",
        tags=["deep-layout", "generic-containers", "filename-without-series"],
    )
    add_cbz(
        "uppercase_extension",
        "roots/source-a/Upper Case (2022)/Upper Case 001.CBZ",
        series="Upper Case",
        number="1",
        year=2022,
        tags=["uppercase-extension"],
    )
    add_cbz(
        "conflicting_metadata",
        "roots/source-a/Conflict Series (2023)/Conflict Series Issue 004.cbz",
        series="Conflict Series",
        number="4",
        metadata_number="5",
        year=2023,
        expected_outcome="review",
        tags=["metadata-conflict", "filename-versus-comicinfo"],
    )

    loose_one = add_cbz(
        "loose_mixed_series",
        "roots/source-b/Loose Imports/Alpha 001.cbz",
        series="Alpha",
        number="1",
        year=2019,
        tags=["loose-files", "mixed-series"],
    )
    loose_two = root / "roots/source-b/Loose Imports/Beta 007.cbz"
    create_deterministic_cbz(
        loose_two,
        seed=seed,
        case_id="loose_mixed_series_beta",
        series="Beta",
        number="7",
        title="Seven",
        year=2020,
        publisher="Fixture House",
        comicvine_series_id=_synthetic_series_id("Beta"),
        comicvine_issue_id=799_007,
    )
    cases[-1]["paths"] = [_relative(root, loose_one), _relative(root, loose_two)]

    duplicate_source = add_cbz(
        "duplicate_identical",
        "roots/source-b/Duplicates/Copy A/Duplicate Series 001.cbz",
        series="Duplicate Series",
        number="1",
        expected_outcome="review",
        tags=["duplicate-content", "identical-bytes"],
    )
    duplicate_copy = root / "roots/source-b/Duplicates/Copy B/Duplicate Series 001.cbz"
    duplicate_copy.parent.mkdir(parents=True)
    shutil.copyfile(duplicate_source, duplicate_copy)
    duplicate_copy.chmod(0o644)
    cases[-1]["paths"] = [_relative(root, duplicate_source), _relative(root, duplicate_copy)]

    add_cbz(
        "duplicate_different",
        "roots/source-b/Duplicates/Variant/Duplicate Series 001.cbz",
        series="Duplicate Series",
        number="1",
        title="Different Payload",
        expected_outcome="review",
        tags=["duplicate-identity", "different-bytes"],
    )
    hardlink = root / "roots/source-b/Duplicates/Hardlink/Duplicate Series 001.cbz"
    hardlink.parent.mkdir(parents=True)
    os.link(duplicate_source, hardlink)
    cases.append(
        _case(
            "hardlink_duplicate",
            paths=[_relative(root, duplicate_source), _relative(root, hardlink)],
            expected_outcome="review",
            tags=["hardlink", "duplicate-content"],
            issue_number="1",
            archive_format="cbz",
        )
    )

    disguised = root / "roots/source-b/Archive Oddities/ZIP Payload Issue 001.cbr"
    create_deterministic_cbz(
        disguised,
        seed=seed,
        case_id="mislabeled_zip_cbr",
        series="Archive Oddities",
        number="1",
        title="ZIP Wearing a CBR Extension",
        year=2026,
        publisher="Fixture House",
        comicvine_series_id=_synthetic_series_id("Archive Oddities"),
        comicvine_issue_id=799_101,
    )
    cases.append(
        _case(
            "mislabeled_zip_cbr",
            paths=[_relative(root, disguised)],
            expected_outcome="success",
            tags=["mislabeled-archive", "zip-as-cbr"],
            issue_number="1",
            archive_format="zip-mislabeled-cbr",
        )
    )
    corrupt = write_bytes(
        root / "roots/source-b/Archive Oddities/Corrupt Issue 002.cbz",
        b"IU9 corrupt archive\n",
    )
    cases.append(
        _case(
            "corrupt_cbz",
            paths=[_relative(root, corrupt)],
            expected_outcome="blocked",
            tags=["corrupt-archive", "fail-closed"],
            issue_number="2",
            archive_format="unreadable",
        )
    )
    empty = create_deterministic_zip(
        root / "roots/source-b/Archive Oddities/Empty Issue 003.cbz",
        {},
    )
    cases.append(
        _case(
            "empty_cbz",
            paths=[_relative(root, empty)],
            expected_outcome="blocked",
            tags=["empty-archive", "no-pages"],
            issue_number="3",
            archive_format="cbz",
        )
    )

    arc_one = root / "roots/source-b/Story Arcs/Fixture Crisis/01 - Alpha 001.cbz"
    arc_two = root / "roots/source-b/Story Arcs/Fixture Crisis/02 - Beta 007.cbz"
    create_deterministic_cbz(
        arc_one,
        seed=seed,
        case_id="story_arc_alpha",
        series="Alpha",
        number="1",
        title="The Beginning",
        year=2019,
        publisher="Fixture House",
        comicvine_series_id=_synthetic_series_id("Alpha"),
        comicvine_issue_id=799_201,
    )
    create_deterministic_cbz(
        arc_two,
        seed=seed,
        case_id="story_arc_beta",
        series="Beta",
        number="7",
        title="The Crossover",
        year=2020,
        publisher="Fixture House",
        comicvine_series_id=_synthetic_series_id("Beta"),
        comicvine_issue_id=799_207,
    )
    cases.append(
        _case(
            "story_arc_reading_order",
            paths=[_relative(root, arc_one), _relative(root, arc_two)],
            expected_outcome="review",
            tags=["story-arc", "mixed-series", "reading-order-prefix"],
            note="Folder shape is arc evidence, not proof; user review remains authoritative.",
        )
    )

    hidden = write_bytes(root / "roots/source-b/Mixed Siblings/.DS_Store", b"synthetic\n")
    notes = write_text(
        root / "roots/source-b/Mixed Siblings/README.txt",
        "Non-comic sibling that the scanner must ignore.\n",
    )
    cases.append(
        _case(
            "hidden_noncomic",
            paths=[_relative(root, hidden), _relative(root, notes)],
            expected_outcome="success",
            tags=["ignored-file", "hidden-file", "non-comic"],
        )
    )

    links = root / "roots/source-b/Links"
    links.mkdir(parents=True)
    safe_link = links / "Issue 01 linked.cbz"
    safe_link.symlink_to(os.path.relpath(issue_only, links))
    broken_link = links / "Missing Issue 999.cbz"
    broken_link.symlink_to("../does-not-exist/Missing Issue 999.cbz")
    cases.extend(
        (
            _case(
                "safe_symlink",
                paths=[_relative(root, safe_link)],
                expected_outcome="review",
                tags=["symlink", "link-inside-source-roots"],
            ),
            _case(
                "broken_symlink",
                paths=[_relative(root, broken_link)],
                expected_outcome="blocked",
                tags=["symlink", "broken-link", "fail-closed"],
            ),
        )
    )

    long_title = "Long Series " + ("Very " * 32) + "End"
    add_cbz(
        "long_near_limit_name",
        f"roots/source-b/Long Paths/{long_title}/Issue 001.cbz",
        series=long_title,
        number="1",
        expected_outcome="review",
        tags=["long-path", "long-series-name", "naming-limit"],
    )

    trusted_archive = add_cbz(
        "trusted_comicvine_identity",
        ("roots/source-a/Trusted Identity Series (2024)/Trusted Identity Series 001 (2024).cbz"),
        series="Trusted Identity Series",
        number="1",
        title="Trusted Identity",
        year=2024,
        publisher="Fixture House",
        tags=["canonical-comicinfo", "canonical-series-sidecar", "provider-free-identity"],
        comicvine_series_id=123456,
        comicvine_issue_id=700100,
        identity_source="series-sidecar-and-comicinfo",
    )
    sidecar_dir = trusted_archive.parent
    series_json = {
        "comicid": 123456,
        "name": "Trusted Identity Series",
        "year": 2024,
        "publisher": "Fixture House",
        "comicvine": {
            "id": 123456,
            "url": "https://comicvine.gamespot.com/volume/4050-123456/",
        },
    }
    sidecars = [
        write_text(
            sidecar_dir / "series.json",
            json.dumps(series_json, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        ),
        write_text(
            sidecar_dir / "cvinfo",
            ("comicid: 123456\nurl: https://comicvine.gamespot.com/volume/4050-123456/\n"),
        ),
        write_bytes(sidecar_dir / "cover.jpg", deterministic_jpeg()),
        write_bytes(sidecar_dir / "folder.jpg", deterministic_jpeg()),
    ]
    cases.append(
        _case(
            "series_sidecar_metadata",
            paths=[_relative(root, path) for path in sidecars],
            expected_outcome="success",
            tags=["series-json", "comicvine-link", "cover-sidecars"],
        )
    )

    cbr_manifest: dict[str, object] = {
        "status": "not_provided",
        "required_filenames": ["iu9-rar3.cbr", "iu9-rar5.cbr"],
    }
    if cbr_seed_dir is not None:
        cbr_destination = source_b / "Genuine CBR Seeds (2026)"
        cbr_evidence = consume_cbr_seed_set(
            cbr_seed_dir,
            cbr_destination,
            expected_sha256=cbr_expected_sha256,
        )
        cbr_manifest = {
            "status": "provided",
            "seeds": [item.to_manifest(root) for item in cbr_evidence],
        }
        for issue_number, item in enumerate(cbr_evidence, start=1):
            cases.append(
                _case(
                    f"genuine_{item.archive_format}_cbr",
                    paths=[_relative(root, item.destination)],
                    expected_outcome="review",
                    tags=[
                        "genuine-cbr",
                        item.archive_format,
                        "external-cc0-seed",
                        "metadata-poor-no-comicinfo",
                    ],
                    issue_number=str(issue_number),
                    archive_format=item.archive_format,
                    note="Filename is parseable; provider identity requires review or matching.",
                )
            )

    manifest: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "fixture_kind": "iu9-folder-import",
        "seed": seed,
        "roots": [
            {
                "id": "source-a",
                "source_relative": "roots/source-a",
                "recommended_mode": "managed-copy-or-in-place",
            },
            {
                "id": "source-b",
                "source_relative": "roots/source-b",
                "recommended_mode": "managed-copy-or-in-place",
            },
        ],
        "cbr_seed_set": cbr_manifest,
        "cases": cases,
        "tree": snapshot_tree(root),
    }
    return write_manifest(root, manifest)
