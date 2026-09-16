"""Direct-execution coverage for both IU9 fixture generator entrypoints."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from scripts.iu9_acceptance_fixtures.shared import RAR3_SIGNATURE, RAR5_SIGNATURE


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _write_cbr_seeds(seed_dir: Path) -> dict[str, str]:
    seed_dir.mkdir()
    rows: list[dict[str, object]] = []
    digests: dict[str, str] = {}
    for seed_id, filename, payload in (
        ("rar3", "iu9-rar3.cbr", RAR3_SIGNATURE + b"cli-rar3" * 8),
        ("rar5", "iu9-rar5.cbr", RAR5_SIGNATURE + b"cli-rar5" * 8),
    ):
        (seed_dir / filename).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        digests[seed_id] = digest
        rows.append(
            {
                "id": seed_id,
                "filename": filename,
                "archive_format": seed_id,
                "sha256": digest,
                "source_url": f"https://example.invalid/{filename}",
                "license": "CC0-1.0",
                "expected_members": ["001.png"],
            }
        )
    (seed_dir / "cbr-seeds.json").write_text(
        json.dumps({"schema_version": 1, "seeds": rows}),
        encoding="utf-8",
    )
    return digests


def test_folder_fixture_cli_runs_directly_from_project_root(tmp_path: Path) -> None:
    output = tmp_path / "folder"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/generate_iu9_folder_fixture.py",
            str(output),
            "--seed",
            "1300",
        ],
        cwd=_project_root(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["fixture_kind"] == "iu9-folder-import"
    assert manifest["seed"] == 1300


def test_mylar_fixture_cli_runs_directly_from_project_root(tmp_path: Path) -> None:
    output = tmp_path / "mylar"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/generate_iu9_mylar_fixture.py",
            str(output),
            "--seed",
            "1300",
        ],
        cwd=_project_root(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["fixture_kind"] == "iu9-mylar-import"
    assert manifest["seed"] == 1300


def test_mylar_fixture_cli_accepts_the_pinned_cbr_contract(tmp_path: Path) -> None:
    output = tmp_path / "mylar-cbr"
    seed_dir = tmp_path / "seeds"
    digests = _write_cbr_seeds(seed_dir)

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/generate_iu9_mylar_fixture.py",
            str(output),
            "--seed",
            "1300",
            "--cbr-seed-dir",
            str(seed_dir),
            "--cbr-rar3-sha256",
            digests["rar3"],
            "--cbr-rar5-sha256",
            digests["rar5"],
        ],
        cwd=_project_root(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["cbr_seed_set"]["status"] == "provided"
