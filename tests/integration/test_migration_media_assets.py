"""Physical media outside library_files must survive a database migration too."""

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing

import pytest

from tests.integration import test_migration_assets as comics

assets = comics.assets
backup = comics.backup
server = comics.server


@pytest.fixture
def placements(assets):
    from pullbox.services.story_arc_placement_service import (
        _fingerprint_regular_nofollow,
        _fingerprint_target,
    )

    db, root = assets
    source = root / "private-1.cbz"
    (root / "arc").mkdir()
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO story_arcs(id,name) VALUES(2,'arc')")
        conn.execute(
            "INSERT INTO issue_story_arcs(id,story_arc_id,issue_id,sequence_number,source_ordinal)"
            " VALUES(3,2,83,1,0)"
        )
        conn.execute("UPDATE library_files SET issue_id=83 WHERE id=401")
        for i, mode in enumerate(("copy", "hardlink", "symlink", "reference_only"), start=10):
            path = root / "arc" / mode
            if mode == "hardlink":
                os.link(source, path)
            elif mode == "symlink":
                path.symlink_to("../" + source.name)
            else:
                shutil.copyfile(source, path)
            conn.execute(
                "INSERT INTO story_arc_placements(id,issue_story_arc_id,library_root_id,"
                "library_file_id,placement_path,mode,ownership,symlink_style,"
                "source_fingerprint,last_result,state) VALUES(?,3,77,401,?,?,?,?,?,?,'current')",
                (
                    i,
                    "/comics/arc/" + mode,
                    mode,
                    "referenced" if mode == "reference_only" else "managed",
                    "relative" if mode == "symlink" else None,
                    json.dumps(_fingerprint_regular_nofollow(source)),
                    json.dumps({"target_fingerprint": _fingerprint_target(path)}),
                ),
            )
    return assets


def test_verifies_actual_placements_without_changing_or_adopting_them(placements):
    db, library = placements
    before = comics.digest(db)
    code, proof = comics.run(placements)
    assert code == 0, proof
    assert proof["placements_checked"] == 4
    code, checked = comics.run(placements, "--compare-sample", proof["sample_sha256"])
    assert code == 0 and checked["sample_compared"]
    assert os.path.samefile(library / "arc/hardlink", library / "private-1.cbz")
    assert (library / "arc/symlink").is_symlink()
    assert comics.digest(db) == before


def test_identical_bytes_do_not_prove_hardlink_or_symlink_identity(placements):
    _, root = placements
    target = root / "arc/hardlink"
    target.unlink()
    shutil.copyfile(root / "private-1.cbz", target)
    link = root / "arc/symlink"
    link.unlink()
    link.symlink_to("../private-2.cbz")
    code, report = comics.run(placements)
    assert code == 1 and not report["sample_sha256"]
    assert report["media_examples"]["arc_representation_changed"] == [11, 12]


def test_drifted_and_missing_arc_artifacts_block_without_repair(placements):
    _, root = placements
    (root / "arc/copy").write_bytes(b"some other comic")
    (root / "arc/reference_only").unlink()
    code, report = comics.run(placements)
    assert code == 1
    assert report["media_examples"]["arc_content_changed"] == [10]
    assert report["media_examples"]["arc_file_missing"] == [13]
    assert not (root / "arc/reference_only").exists()


@pytest.fixture
def art(assets, tmp_path):
    db, root = assets
    cache = tmp_path / "art-cache"
    series = root / "private-series"
    series.mkdir()
    (series / "cover.jpg").write_bytes(b"series cover")
    (cache / "71").mkdir(parents=True)
    (cache / "71/issue_0000.5.png").write_bytes(b"issue cover")
    (cache / "story_arcs/2").mkdir(parents=True)
    (cache / "story_arcs/2/story_arc.webp").write_bytes(b"arc cover")
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "UPDATE series SET path='/comics/private-series',library_root_id=77,"
            "cover_path='/api/v1/series/71/cover' WHERE id=71"
        )
        conn.execute("UPDATE issues SET cover_path='/api/v1/issues/83/cover' WHERE id=83")
        conn.execute(
            "INSERT INTO story_arcs(id,name,cover_path) "
            "VALUES(2,'arc','/api/v1/story-arcs/2/cover')"
            " ON CONFLICT(id) DO UPDATE SET cover_path=excluded.cover_path"
        )
    return assets, cache


def test_cover_cache_and_folder_art_are_compared_without_fetching(art):
    library, cache = art
    code, baseline = comics.run(library, "--covers-dir", str(cache))
    assert code == 0, baseline
    assert baseline["covers_checked"] == 3
    (cache / "71/issue_0000.5.png").write_bytes(b"wrong bytes")
    code, report = comics.run(
        library, "--covers-dir", str(cache), "--compare-sample", baseline["sample_sha256"]
    )
    assert code == 1 and "sample_mismatch" in report["blockers"]
    (cache / "71/issue_0000.5.png").unlink()
    code, report = comics.run(library, "--covers-dir", str(cache))
    assert code == 1 and report["media_examples"]["issue_cover_missing"] == [83]
    assert not report["sample_sha256"]


def test_rejects_old_comic_only_fingerprint_even_when_no_art_is_found(assets):
    db, root = assets
    old = hashlib.sha256(f"pullbox-comic-sample-v1:{comics.digest(db)}:False\n".encode())
    for n in (1, 3, 5):
        path = root / f"private-{n}.cbz"
        old.update(f"77:{400 + n}:{path.stat().st_size}:{comics.digest(path)}\n".encode())
    code, report = comics.run(assets, "--compare-sample", old.hexdigest())
    assert code == 1 and not report["sample_compared"] and not report["sample_sha256"]


def test_stale_saved_owner_proof_is_not_silently_rebound(placements):
    _, root = placements
    path = root / "arc/copy"
    replacement = root / "new-copy"
    shutil.copy2(path, replacement)
    replacement.replace(path)
    code, report = comics.run(placements)
    assert code == 1 and report["media_examples"]["arc_saved_identity_changed"] == [10]


def test_absolute_arc_links_verify_only_in_their_actual_mount_namespace(placements):
    from pullbox.services.story_arc_placement_service import _fingerprint_target

    db, root = placements
    link = root / "arc/symlink"
    link.unlink()
    link.symlink_to(root / "private-1.cbz")
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "UPDATE story_arc_placements SET symlink_style='absolute',last_result=? WHERE id=12",
            (json.dumps({"target_fingerprint": _fingerprint_target(link)}),),
        )
    assert comics.run(placements)[0] == 0
    link.unlink()
    link.symlink_to("/comics/private-1.cbz")
    code, proof = comics.run(placements)
    assert code == 1 and proof["media_examples"]["arc_representation_changed"] == [12]


def test_legacy_issue_id_cover_and_uncatalogued_local_cover_bytes(art, tmp_path):
    assets, cache = art
    legacy = tmp_path / "old-cache"
    (legacy / "71").mkdir(parents=True)
    (cache / "71/issue_0000.5.png").rename(legacy / "71/issue_83.png")
    with closing(sqlite3.connect(assets[0])) as conn, conn:
        conn.execute("UPDATE issues SET cover_path=NULL")
    args = ("--covers-dir", str(cache), "--legacy-covers-dir", str(legacy))
    code, baseline = comics.run(assets, *args)
    assert code == 0 and baseline["covers_checked"] == 3
    (legacy / "71/issue_83.png").unlink()
    code, result = comics.run(assets, *args, "--compare-sample", baseline["sample_sha256"])
    assert code == 1 and result["blockers"] == {"sample_mismatch": 1}


def test_cover_links_never_escape_cache_or_fetch_remote_replacement(art, tmp_path):
    assets, cache = art
    outside = tmp_path / "unrelated"
    outside.write_bytes(b"private outside data")
    p = cache / "story_arcs/2/story_arc.webp"
    p.unlink()
    p.symlink_to(outside)
    with closing(sqlite3.connect(assets[0])) as conn, conn:
        conn.execute("UPDATE story_arcs SET cover_url='https://fixture.invalid/secret-cover'")
    code, report = comics.run(assets, "--covers-dir", str(cache))
    assert code == 1 and not report["sample_sha256"] and "secret-cover" not in str(report)
    assert outside.read_bytes() == b"private outside data" and p.is_symlink()


def test_sealed_postgres_stage_uses_same_snapshot_for_all_media(server, placements, art):
    (db, _), cache = art
    name, url, sql = comics.stages.target(server)
    code, stage = comics.stages.stage(db, url)
    assert code == 0 and stage["verified_tables"]["story_arc_placements"] == 4
    opts = ("--covers-dir", str(cache))
    code, original = comics.run(placements, *opts, snapshot=stage["snapshot_sha256"])
    assert code == 0 and original["placements_checked"] == 4 and original["covers_checked"] == 3
    code, proposed = comics.run(
        placements,
        *opts,
        "--compare-sample",
        original["sample_sha256"],
        snapshot=stage["snapshot_sha256"],
    )
    assert code == 0 and proposed["sample_compared"] and not proposed["cutover_ready"]
    assert comics.stages.state(sql, name) == "false:pullbox:offline-stage:rows-verified"
    comics.stages.bootstrap_is_blocked(url)


def test_arc_only_root_requires_explicit_mapping(placements):
    db, root = placements
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO library_roots(id,path,name,enabled) VALUES(78,'/arcs','arcs',0)")
        conn.execute(
            "UPDATE story_arc_placements SET library_root_id=78,"
            "placement_path=replace(placement_path,'/comics/arc/','/arcs/')"
        )
    code, report = comics.run(placements)
    assert code == 1 and report["root_problems"]["78"] == "media_root_mapping_required"
    code, report = comics.run(placements, "--root", f"78={root / 'arc'}")
    assert code == 0 and report["placements_checked"] == 4


def test_relative_arc_link_must_resolve_in_actual_mount_layout(placements, tmp_path):
    db, root = placements
    # The same-looking relative path is now one directory too high after a mount relocation.
    alias = tmp_path / "layout" / "mounted"
    alias.parent.mkdir()
    alias.symlink_to(root, target_is_directory=True)
    link = root / "arc/symlink"
    link.unlink()
    text = os.path.relpath(root / "private-1.cbz", alias / "arc")
    link.symlink_to(text)
    assert not link.exists()
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("INSERT INTO library_roots(id,path,name,enabled) VALUES(78,'/arcs','arcs',1)")
        stored = json.loads(
            conn.execute("SELECT last_result FROM story_arc_placements WHERE id=12").fetchone()[0]
        )
        info = link.lstat()
        stored["target_fingerprint"].update(
            link_target=text, inode=info.st_ino, device=info.st_dev, mtime_ns=info.st_mtime_ns
        )
        conn.execute(
            "UPDATE story_arc_placements SET library_root_id=78,"
            "placement_path='/arcs/symlink',last_result=? WHERE id=12",
            (json.dumps(stored),),
        )
    code, report = comics.run(placements, "--root", f"78={alias / 'arc'}")
    assert code == 1 and report["media_examples"]["arc_representation_changed"] == [12]
