"""Benchmark fixtures must exercise realistic member counts without network calls."""

import zipfile

from scripts.benchmark_import_scan import _build_tree


def test_benchmark_can_reproduce_multi_page_archives(tmp_path):
    _build_tree(
        tmp_path, series_count=2, files_per_series=3, trusted_comicinfo=True, archive_pages=32
    )
    files = list(tmp_path.rglob("*.cbz"))
    assert len(files) == 6
    for path in files:
        with zipfile.ZipFile(path) as archive:
            assert len(archive.infolist()) == 33
            assert archive.read("ComicInfo.xml").startswith(b"<?xml")
