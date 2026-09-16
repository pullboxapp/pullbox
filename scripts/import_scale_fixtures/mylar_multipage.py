"""Create a separate multipage copy of the synthetic Mylar scale fixture."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from scripts.iu9_acceptance_fixtures.shared import sha256_file


def _relative_path(folder: str, name: str, root: PurePosixPath) -> Path:
    relative = PurePosixPath(folder).relative_to(root) / name
    if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative):
        raise ValueError("Fixture source path must stay inside its recorded root")
    return Path(*relative.parts)


def _copy_archive(original: Path, destination: Path, pages: int) -> None:
    if original.stat().st_size > 1_000_000:
        raise ValueError("Only tiny synthetic one-page archives may be upgraded")
    with zipfile.ZipFile(original) as archive:
        names = archive.namelist()
        images = [name for name in names if name.endswith(".png")]
        if len(images) != 1 or set(names) != {images[0], "ComicInfo.xml"}:
            raise ValueError("Expected synthetic PNG plus ComicInfo.xml only")
        if any(info.file_size > 1_000_000 for info in archive.infolist()):
            raise ValueError("Synthetic member exceeds fixture size limit")
        metadata, image = archive.read("ComicInfo.xml"), archive.read(images[0])
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in [
            ("ComicInfo.xml", metadata),
            *[(f"pages/{number:03d}.png", image) for number in range(1, pages + 1)],
        ]:
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)


def upgrade_fixture(
    source: Path,
    output: Path,
    *,
    container_root: str,
    pages: int = 32,
    single_page_every: int = 2000,
) -> dict[str, object]:
    """Preserve source data while generating a new synthetic benchmark fixture."""
    started = time.monotonic()
    source, output = source.resolve(), output.absolute()
    if output.exists():
        raise FileExistsError("Output must not already exist")
    if output.is_relative_to(source):
        raise ValueError("Output must be separate from the original fixture")
    if not 2 <= pages <= 1000 or single_page_every < 0:
        raise ValueError("Invalid page count or safety exception interval")
    report = json.loads((source / "generation-report.json").read_text())
    if report.get("fixture_kind") != "mylar-import-from-cv-stress-manifest":
        raise ValueError("Source is not the supported synthetic Mylar fixture")
    old_root, new_root = (
        PurePosixPath(report["recorded_source_root"]),
        PurePosixPath(container_root),
    )
    if not new_root.is_absolute() or ".." in new_root.parts:
        raise ValueError("Container root must be an absolute safe path")
    before = sha256_file(source / "mylar.db")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".mylar-multipage-", dir=output.parent))
    count = 0
    total_bytes = 0
    try:
        with (
            closing(
                sqlite3.connect(f"{(source / 'mylar.db').as_uri()}?mode=ro", uri=True)
            ) as original,
            closing(sqlite3.connect(staging / "mylar.db")) as db,
        ):
            original.backup(db)
            rows = original.execute(
                "SELECT i.IssueID, c.ComicLocation, i.Location FROM issues i "
                "JOIN comics c ON c.ComicID=i.ComicID ORDER BY i.IssueID"
            )
            with (staging / "fixture-manifest.jsonl").open("w") as manifest:
                for issue_id, folder, name in rows:
                    relative = _relative_path(folder, name, old_root)
                    source_path = source / "source" / relative
                    if source_path.resolve() != source_path or not source_path.is_file():
                        raise ValueError("Fixture paths must be regular files without symlinks")
                    count += 1
                    page_count = (
                        1 if single_page_every and count % single_page_every == 0 else pages
                    )
                    target = staging / "source" / relative
                    _copy_archive(source_path, target, page_count)
                    size = target.stat().st_size
                    total_bytes += size
                    db.execute(
                        "UPDATE issues SET ComicSize=? WHERE IssueID=?", (str(size), issue_id)
                    )
                    manifest.write(
                        json.dumps(
                            {
                                "issue_id": issue_id,
                                "relative_path": relative.as_posix(),
                                "archive_sha256": sha256_file(target),
                                "archive_size": size,
                                "page_count": page_count,
                                "expected_safety_review": page_count == 1,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\n"
                    )
            for rowid, folder in original.execute("SELECT rowid, ComicLocation FROM comics"):
                relative = _relative_path(folder, "", old_root)
                db.execute(
                    "UPDATE comics SET ComicLocation=? WHERE rowid=?",
                    ((new_root / relative.as_posix()).as_posix(), rowid),
                )
            if count != report["issue_count"]:
                raise ValueError("Fixture issue count differs from the generation report")
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ValueError("Generated database failed integrity check")
            db.commit()
        if sha256_file(source / "mylar.db") != before:
            raise ValueError("Source database changed during generation")
        result: dict[str, object] = {
            "fixture_kind": "mylar-import-multipage",
            "source_fixture": str(source),
            "recorded_source_root": container_root,
            "series_count": report["series_count"],
            "issue_count": count,
            "archive_pages": pages,
            "single_page_every": single_page_every,
            "single_page_files": count // single_page_every if single_page_every else 0,
            "archive_bytes": total_bytes,
            "sqlite_integrity_check": integrity,
            "source_database_sha256": before,
            "database_sha256": sha256_file(staging / "mylar.db"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "notes": [
                "Generated image members, not real comic payloads or disk-throughput evidence.",
                "Original fixture and all original archive metadata are preserved.",
                "Single-page exceptions are expected to require safety review.",
            ],
        }
        (staging / "generation-report.json").write_text(json.dumps(result, indent=2) + "\n")
        staging.rename(output)
        return result
    except BaseException:
        shutil.rmtree(staging)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--container-root", required=True)
    parser.add_argument("--pages", type=int, default=32)
    parser.add_argument("--single-page-every", type=int, default=2000)
    args = parser.parse_args()
    print(
        json.dumps(
            upgrade_fixture(
                args.source,
                args.output,
                container_root=args.container_root,
                pages=args.pages,
                single_page_every=args.single_page_every,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
