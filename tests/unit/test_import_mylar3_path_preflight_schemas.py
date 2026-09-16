"""Tests for Mylar path-preflight request validation."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from pullbox.schemas.import_mylar3_path_preflight import MylarPathPreviewRequest

if TYPE_CHECKING:
    from pathlib import Path


def _database(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE comics (ComicLocation TEXT)")
    connection.commit()
    connection.close()
    return path


def test_request_accepts_directory_containing_mylar_database(tmp_path: Path) -> None:
    _database(tmp_path / "mylar.db")

    request = MylarPathPreviewRequest(source_path=str(tmp_path), source_type="mylar3")

    assert request.source_path == str(tmp_path.resolve())


def test_request_rejects_duplicate_normalized_stored_prefixes(tmp_path: Path) -> None:
    db_path = _database(tmp_path / "mylar.db")

    with pytest.raises(ValidationError, match="duplicate stored prefix"):
        MylarPathPreviewRequest(
            source_path=str(db_path),
            source_type="mylar3",
            auto_detect=False,
            mappings=[
                {"stored_prefix": "/books", "pullbox_prefix": "/library"},
                {"stored_prefix": "/books/", "pullbox_prefix": "/library-two"},
            ],
        )


def test_request_rejects_exact_duplicate_stored_prefixes(tmp_path: Path) -> None:
    db_path = _database(tmp_path / "mylar.db")

    with pytest.raises(ValidationError, match="duplicate stored prefix"):
        MylarPathPreviewRequest(
            source_path=str(db_path),
            source_type="mylar3",
            auto_detect=False,
            mappings=[
                {"stored_prefix": "/books", "pullbox_prefix": "/library"},
                {"stored_prefix": "/books", "pullbox_prefix": "/library-two"},
            ],
        )


def test_request_rejects_folder_source_type(tmp_path: Path) -> None:
    db_path = _database(tmp_path / "mylar.db")

    with pytest.raises(ValidationError, match="only supports Mylar"):
        MylarPathPreviewRequest(source_path=str(db_path), source_type="filesystem")


def test_request_rejects_container_root_mapping(tmp_path: Path) -> None:
    db_path = _database(tmp_path / "mylar.db")

    with pytest.raises(ValidationError, match="safe absolute directory"):
        MylarPathPreviewRequest(
            source_path=str(db_path),
            source_type="mylar3",
            auto_detect=False,
            mappings=[{"stored_prefix": "/books", "pullbox_prefix": "/"}],
        )
