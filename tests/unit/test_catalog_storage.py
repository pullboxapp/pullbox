"""Realistic zstd windows and expansion limits protect the installed catalog."""

import pytest
import zstandard

from pullbox.services.catalog import storage
from pullbox.services.catalog.contract import CatalogError


def test_streams_production_sized_zstd_window(tmp_path):
    data = b"catalog" * 500_000
    archive = tmp_path / "snapshot.zst"
    archive.write_bytes(zstandard.ZstdCompressor(level=3).compress(data))
    output = tmp_path / "snapshot.db"
    error = None
    try:
        storage.decompress(archive, output)
    except CatalogError as exc:
        error = str(exc)
    assert error is None
    assert output.read_bytes() == data


def test_enforces_expansion_ceiling(tmp_path, monkeypatch):
    archive = tmp_path / "snapshot.zst"
    archive.write_bytes(zstandard.ZstdCompressor().compress(b"a" * 10000))
    monkeypatch.setattr(storage, "MAX_DATABASE_BYTES", 100)
    with pytest.raises(CatalogError, match="storage size"):
        storage.decompress(archive, tmp_path / "snapshot.db")
