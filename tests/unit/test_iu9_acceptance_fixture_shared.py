"""Shared contracts for deterministic IU9 acceptance fixtures."""

from __future__ import annotations

import hashlib
import json
import zipfile
from typing import TYPE_CHECKING

import pytest

from pullbox.core.source_metadata import MetadataSignal, SourceMetadataExtractor
from scripts.iu9_acceptance_fixtures.shared import (
    RAR3_SIGNATURE,
    RAR5_SIGNATURE,
    CbrSeedValidationError,
    consume_cbr_seed_set,
    create_deterministic_cbz,
    load_json,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_deterministic_cbz_has_valid_pages_metadata_and_stable_bytes(tmp_path: Path) -> None:
    first = tmp_path / "first.cbz"
    second = tmp_path / "second.cbz"
    different = tmp_path / "different.cbz"

    create_deterministic_cbz(
        first,
        seed=1300,
        case_id="decimal-issue",
        series="Number Lab",
        number="0.5",
        title="The Half Step",
        year=2026,
        publisher="Fixture House",
    )
    create_deterministic_cbz(
        second,
        seed=1300,
        case_id="decimal-issue",
        series="Number Lab",
        number="0.5",
        title="The Half Step",
        year=2026,
        publisher="Fixture House",
    )
    create_deterministic_cbz(
        different,
        seed=1301,
        case_id="decimal-issue",
        series="Number Lab",
        number="0.5",
        title="The Half Step",
        year=2026,
        publisher="Fixture House",
    )

    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes() != different.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == [
            "ComicInfo.xml",
            "pages/001.png",
            "pages/002.png",
            "pages/003.png",
        ]
        comic_info = archive.read("ComicInfo.xml").decode()
        assert "<Series>Number Lab</Series>" in comic_info
        assert "<Number>0.5</Number>" in comic_info
        assert "<Title>The Half Step</Title>" in comic_info
        assert archive.read("pages/001.png").startswith(b"\x89PNG\r\n\x1a\n")


def test_deterministic_cbz_round_trips_canonical_comicvine_identity(tmp_path: Path) -> None:
    archive = tmp_path / "Identity Series 001.cbz"
    create_deterministic_cbz(
        archive,
        seed=1300,
        case_id="identity-round-trip",
        series="Identity Series",
        number="1",
        comicvine_series_id=654321,
        comicvine_issue_id=765432,
    )

    metadata = SourceMetadataExtractor().from_archive_path(archive)

    assert metadata.comicvine_series_id == 654321
    assert metadata.comicvine_issue_id == 765432
    assert metadata.signals["comicvine_series_id"] == MetadataSignal.COMICINFO
    assert metadata.signals["comicvine_issue_id"] == MetadataSignal.COMICINFO


def _write_cbr_seed_descriptor(seed_dir: Path, *, corrupt_digest: bool = False) -> None:
    seeds = (
        ("rar3", "iu9-rar3.cbr", RAR3_SIGNATURE + b"rar3-test-payload" * 8),
        ("rar5", "iu9-rar5.cbr", RAR5_SIGNATURE + b"rar5-test-payload" * 8),
    )
    descriptor_rows: list[dict[str, object]] = []
    for seed_id, filename, payload in seeds:
        (seed_dir / filename).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        descriptor_rows.append(
            {
                "id": seed_id,
                "filename": filename,
                "archive_format": seed_id,
                "sha256": "0" * 64 if corrupt_digest and seed_id == "rar5" else digest,
                "source_url": f"https://example.invalid/{filename}",
                "license": "CC0-1.0",
                "expected_members": ["001.png"],
            }
        )
    (seed_dir / "cbr-seeds.json").write_text(
        json.dumps({"schema_version": 1, "seeds": descriptor_rows}),
        encoding="utf-8",
    )


def test_cbr_seed_contract_validates_provenance_digest_and_rar_family(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seeds"
    destination = tmp_path / "fixture"
    seed_dir.mkdir()
    _write_cbr_seed_descriptor(seed_dir)

    evidence = consume_cbr_seed_set(seed_dir, destination)

    assert [item.seed_id for item in evidence] == ["rar3", "rar5"]
    assert [item.archive_format for item in evidence] == ["rar3", "rar5"]
    assert [item.destination.name for item in evidence] == [
        "Genuine CBR Seeds 001 (2026).cbr",
        "Genuine CBR Seeds 002 (2026).cbr",
    ]
    assert all(item.license == "CC0-1.0" for item in evidence)
    assert all(item.destination.is_file() for item in evidence)
    assert load_json(seed_dir / "cbr-seeds.json")["schema_version"] == 1


def test_cbr_seed_contract_rejects_digest_or_signature_mismatch(tmp_path: Path) -> None:
    bad_digest = tmp_path / "bad-digest"
    bad_digest.mkdir()
    _write_cbr_seed_descriptor(bad_digest, corrupt_digest=True)

    with pytest.raises(CbrSeedValidationError, match="SHA-256"):
        consume_cbr_seed_set(bad_digest, tmp_path / "digest-output")

    bad_signature = tmp_path / "bad-signature"
    bad_signature.mkdir()
    _write_cbr_seed_descriptor(bad_signature)
    rar3_path = bad_signature / "iu9-rar3.cbr"
    rar3_path.write_bytes(b"PK\x03\x04" + rar3_path.read_bytes())
    descriptor = load_json(bad_signature / "cbr-seeds.json")
    descriptor["seeds"][0]["sha256"] = hashlib.sha256(rar3_path.read_bytes()).hexdigest()
    (bad_signature / "cbr-seeds.json").write_text(
        json.dumps(descriptor),
        encoding="utf-8",
    )

    with pytest.raises(CbrSeedValidationError, match="RAR3 signature"):
        consume_cbr_seed_set(bad_signature, tmp_path / "signature-output")
