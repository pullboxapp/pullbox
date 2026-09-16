"""Unit tests for archive reader behavior across comic archive formats."""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

import py7zr

from pullbox.core.archive import ArchiveReader

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch


def _write_cb7(path: Path, comicinfo_xml: str, *, nested: bool = False) -> None:
    """Create a CB7 archive with a ComicInfo.xml file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload_dir = path.parent / "payload"
    payload_dir.mkdir()

    comicinfo_name = "metadata/ComicInfo.xml" if nested else "ComicInfo.xml"
    comicinfo_path = payload_dir / comicinfo_name
    comicinfo_path.parent.mkdir(parents=True, exist_ok=True)
    comicinfo_path.write_text(comicinfo_xml)

    page_path = payload_dir / "pages" / "page001.jpg"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_bytes(b"fake image")

    with py7zr.SevenZipFile(path, "w") as archive:
        archive.write(comicinfo_path, comicinfo_name)
        archive.write(page_path, "pages/page001.jpg")


class TestArchiveReaderCb7:
    """CB7 archives should behave like the other archive formats."""

    def test_read_comicinfo_from_cb7(self, tmp_path: Path) -> None:
        archive = tmp_path / "test.cb7"
        _write_cb7(
            archive,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Chicken Devil</Series>
              <Number>4</Number>
              <Volume>2021</Volume>
              <Year>2022</Year>
              <Publisher>Aftershock Comics</Publisher>
              <Web>https://comicvine.gamespot.com/chicken-devil-4-the-chicken-is-in-the-details/4000-905404/</Web>
            </ComicInfo>
            """,
        )

        comicinfo = ArchiveReader(archive).read_comicinfo()

        assert comicinfo is not None
        assert comicinfo.series == "Chicken Devil"
        assert comicinfo.number == "4"
        assert comicinfo.volume == "2021"
        assert comicinfo.year == 2022
        assert comicinfo.web == (
            "https://comicvine.gamespot.com/chicken-devil-4-the-chicken-is-in-the-details/4000-905404/"
        )

    def test_read_nested_file_from_cb7(self, tmp_path: Path) -> None:
        archive = tmp_path / "nested.cb7"
        _write_cb7(
            archive,
            "<ComicInfo><Series>Batman</Series></ComicInfo>",
            nested=True,
        )

        xml_bytes = ArchiveReader(archive).read_file("metadata/ComicInfo.xml")

        assert b"<Series>Batman</Series>" in xml_bytes


def test_read_comicinfo_prefers_canonical_root_member(tmp_path: Path) -> None:
    archive = tmp_path / "duplicate-comicinfo.cbz"
    with zipfile.ZipFile(archive, "w") as payload:
        payload.writestr(
            "Legacy/ComicInfo.xml",
            "<ComicInfo><Series>Wrong nested metadata</Series></ComicInfo>",
        )
        payload.writestr(
            "ComicInfo.xml",
            "<ComicInfo><Series>Babyteeth</Series><Number>1</Number></ComicInfo>",
        )

    comicinfo = ArchiveReader(archive).read_comicinfo()

    assert comicinfo is not None
    assert comicinfo.series == "Babyteeth"
    assert comicinfo.number == "1"


class TestArchiveReaderCbr:
    """CBR archives should use the central RAR backend before opening."""

    def test_list_cbr_files_configures_rar_backend(
        self,
        tmp_path: Path,
        monkeypatch: MonkeyPatch,
    ) -> None:
        import rarfile

        from pullbox.core import archive as archive_module

        cbr = tmp_path / "test.cbr"
        cbr.write_bytes(b"Rar!\x1a\x07\x00fake")
        backend_calls: list[bool] = []

        class FakeRarFile:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def __enter__(self) -> FakeRarFile:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def namelist(self) -> list[str]:
                return ["ComicInfo.xml", "page_001.jpg"]

        monkeypatch.setattr(
            archive_module,
            "configure_rarfile_backend",
            lambda: backend_calls.append(True),
        )
        monkeypatch.setattr(rarfile, "RarFile", FakeRarFile)

        assert ArchiveReader(cbr).list_files() == ["ComicInfo.xml", "page_001.jpg"]
        assert backend_calls == [True]
