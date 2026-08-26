"""Unit tests for shared source metadata extraction."""

from __future__ import annotations

import json
import zipfile
from typing import TYPE_CHECKING

import py7zr

from pullbox.core import source_metadata
from pullbox.core.source_metadata import MetadataSignal, SourceMetadataExtractor
from pullbox.models.issue import IssueType

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _write_cbz(path: Path, comicinfo_xml: str | None = None) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        if comicinfo_xml is not None:
            zf.writestr("ComicInfo.xml", comicinfo_xml)
        zf.writestr("page001.jpg", b"fake")


def _write_cbz_entries(path: Path, entries: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for entry in entries:
            zf.writestr(entry, b"fake")


def _write_cb7(path: Path, comicinfo_xml: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload_dir = path.parent / "cb7_payload"
    payload_dir.mkdir()
    page_path = payload_dir / "page001.jpg"
    page_path.write_bytes(b"fake")
    with py7zr.SevenZipFile(path, "w") as archive:
        archive.write(page_path, "page001.jpg")
        if comicinfo_xml is not None:
            comicinfo_path = payload_dir / "ComicInfo.xml"
            comicinfo_path.write_text(comicinfo_xml)
            archive.write(comicinfo_path, "ComicInfo.xml")


class TestArchiveMetadataExtraction:
    """Archive extraction should unify ComicInfo, sidecars, and folder hints."""

    def test_sidecar_series_id_and_booktype_override_filename(self, tmp_path: Path) -> None:
        folder = tmp_path / "Absolute Martian Manhunter (2025) [TPB]"
        folder.mkdir()
        (folder / "series.json").write_text(
            json.dumps(
                {
                    "comicid": 168590,
                    "booktype": "TPB",
                    "status": "Ended",
                    "total_issues": 1,
                }
            )
        )
        archive = folder / "Absolute Martian Manhunter 001 (2025).cbz"
        _write_cbz(archive)

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.comicvine_series_id == 168590
        assert metadata.issue_type == IssueType.TPB
        assert metadata.series_status == "Ended"
        assert metadata.issue_count_hint == 1
        assert metadata.signals["comicvine_series_id"] == MetadataSignal.SIDECAR
        assert metadata.signals["issue_type"] == MetadataSignal.SIDECAR

    def test_conflicting_sidecar_and_comicinfo_ids_are_recorded(self, tmp_path: Path) -> None:
        folder = tmp_path / "Batman (2016)"
        folder.mkdir()
        (folder / "series.json").write_text(json.dumps({"comicid": 11111, "issueid": 22222}))
        archive = folder / "Batman 001.cbz"
        _write_cbz(
            archive,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Batman</Series>
              <Number>1</Number>
              <Notes>[cv_vol_id:97508] [cv_issue_id:123456]</Notes>
            </ComicInfo>
            """,
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.comicvine_series_id == 11111
        assert metadata.comicvine_issue_id == 22222
        assert metadata.diagnostics["identity_conflicts"] == [
            {
                "field": "comicvine_series_id",
                "comicinfo": 97508,
                "sidecar": 11111,
            },
            {
                "field": "comicvine_issue_id",
                "comicinfo": 123456,
                "sidecar": 22222,
            },
        ]

    def test_comicinfo_can_supply_series_issue_and_year(self, tmp_path: Path) -> None:
        folder = tmp_path / "Batman (2016)"
        folder.mkdir()
        archive = folder / "Batman 045.cbz"
        _write_cbz(
            archive,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Batman</Series>
              <Number>45</Number>
              <Volume>2016</Volume>
              <Publisher>DC Comics</Publisher>
              <Web>https://comicvine.gamespot.com/batman-45/4000-987654/</Web>
            </ComicInfo>
            """,
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.series_name == "Batman"
        assert metadata.issue_number == 45.0
        assert metadata.year == 2016
        assert metadata.publisher == "DC Comics"
        assert metadata.comicvine_issue_id == 987654
        assert metadata.signals["comicvine_issue_id"] == MetadataSignal.COMICINFO

    def test_comicinfo_limited_series_number_normalizes_to_issue(self, tmp_path: Path) -> None:
        folder = tmp_path / "Necronomicon (2008)"
        folder.mkdir()
        archive = folder / "Necronomicon 04 (of 04) (2008) (Digital).cbz"
        _write_cbz(
            archive,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Necronomicon</Series>
              <Number>04 (of 04)</Number>
              <Volume>2008</Volume>
              <Publisher>BOOM! Studios</Publisher>
            </ComicInfo>
            """,
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.series_name == "Necronomicon"
        assert metadata.issue_number == 4.0
        assert metadata.signals["issue_number"] == MetadataSignal.COMICINFO
        assert metadata.diagnostics["comicinfo"]["number"] == "04 (of 04)"

    def test_unparseable_comicinfo_number_does_not_erase_filename_issue(
        self,
        tmp_path: Path,
    ) -> None:
        folder = tmp_path / "Necronomicon (2008)"
        folder.mkdir()
        archive = folder / "Necronomicon 04 (of 04) (2008) (Digital).cbz"
        _write_cbz(
            archive,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Necronomicon</Series>
              <Number>not an issue number</Number>
              <Volume>2008</Volume>
              <Publisher>BOOM! Studios</Publisher>
            </ComicInfo>
            """,
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.issue_number == 4.0
        assert "issue_number" not in metadata.signals
        assert metadata.diagnostics["comicinfo_issue_number_ignored"] == {
            "reason": "unparseable",
            "number": "not an issue number",
            "filename_issue_number": 4.0,
        }

    def test_cb7_comicinfo_is_extracted_for_import_matching(self, tmp_path: Path) -> None:
        folder = tmp_path / "Chicken Devil (2021)"
        folder.mkdir()
        archive = folder / "Chicken Devil 004 (2022).cb7"
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

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.series_name == "Chicken Devil"
        assert metadata.issue_number == 4.0
        assert metadata.year == 2021
        assert metadata.publisher == "Aftershock Comics"
        assert metadata.comicvine_issue_id == 905404
        assert metadata.signals["comicvine_issue_id"] == MetadataSignal.COMICINFO
        assert metadata.diagnostics["has_comicinfo"] is True
        assert metadata.diagnostics["filename_parse"]["series_name"] == "Chicken Devil"
        assert metadata.diagnostics["filename_parse"]["year"] == 2022

    def test_filename_parse_is_preserved_when_comicinfo_overrides_series_name(
        self,
        tmp_path: Path,
    ) -> None:
        folder = tmp_path / "Chicken Devil (2021)"
        folder.mkdir()
        archive = folder / "Chicken Devil 004 (2022).cb7"
        _write_cb7(
            archive,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Chicken Devils</Series>
              <Number>4</Number>
              <Volume>2022</Volume>
              <Year>2023</Year>
              <Publisher>Aftershock Comics</Publisher>
              <Web>https://comicvine.gamespot.com/chicken-devils-4-the-chickens-made-me-do-it/4000-996957/</Web>
            </ComicInfo>
            """,
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.series_name == "Chicken Devils"
        assert metadata.diagnostics["filename_parse"]["series_name"] == "Chicken Devil"
        assert metadata.diagnostics["filename_parse"]["issue_number"] == 4.0
        assert metadata.diagnostics["filename_parse"]["year"] == 2022

    def test_comicinfo_trailing_book_ordinal_does_not_override_filename_base(
        self,
        tmp_path: Path,
    ) -> None:
        folder = tmp_path / "Incoming"
        folder.mkdir()
        archive = folder / "Not Drunk Enough Book Two (2020) (Digital-Empire).cbz"
        _write_cbz(
            archive,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Not Drunk Enough Book Two</Series>
              <Publisher>Oni Press</Publisher>
            </ComicInfo>
            """,
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.series_name == "Not Drunk Enough"
        assert metadata.issue_number == 2.0
        assert metadata.signals.get("series_name") is None
        assert metadata.diagnostics["comicinfo"]["series"] == "Not Drunk Enough Book Two"
        assert metadata.diagnostics["comicinfo_series_ignored"] == {
            "reason": "redundant_ordinal_suffix",
            "series": "Not Drunk Enough Book Two",
            "filename_series": "Not Drunk Enough",
            "issue_number": 2.0,
        }

    def test_comicinfo_keeps_real_series_suffix_after_book_ordinal(
        self,
        tmp_path: Path,
    ) -> None:
        folder = tmp_path / "Incoming"
        folder.mkdir()
        archive = folder / "Something Is Killing The Children Book Two (2021).cbz"
        _write_cbz(
            archive,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Something Is Killing The Children Book Two Deluxe Edition</Series>
              <Publisher>BOOM! Studios</Publisher>
            </ComicInfo>
            """,
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.series_name == "Something Is Killing The Children Book Two Deluxe Edition"
        assert metadata.signals["series_name"] == MetadataSignal.COMICINFO
        assert "comicinfo_series_ignored" not in metadata.diagnostics

    def test_volume_subtitle_filename_uses_base_series_and_volume_issue(
        self,
        tmp_path: Path,
    ) -> None:
        folder = tmp_path / "Incoming"
        folder.mkdir()
        archive = folder / "Immortal Thor v01 - All Weather Turns To Storm (2024) (Digital).cbz"
        _write_cbz(archive)

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.series_name == "Immortal Thor"
        assert metadata.issue_number == 1.0
        assert metadata.volume == "v01"
        assert metadata.signals["issue_number"] == MetadataSignal.RELEASE_TITLE
        assert metadata.diagnostics["volume_subtitle_hint"] == {
            "base_series": "Immortal Thor",
            "subtitle": "All Weather Turns To Storm",
            "issue_number": 1.0,
        }

    def test_tpb_volume_subtitle_release_uses_base_series_and_volume_issue(self) -> None:
        metadata = SourceMetadataExtractor().from_release_title(
            "Invincible Vol. 9 Out of This World (2008) (Digital TPB+ Extras) (Zone-Empire)"
        )

        assert metadata.series_name == "Invincible"
        assert metadata.issue_number == 9.0
        assert metadata.volume == "Vol. 9"
        assert metadata.issue_type == IssueType.TPB
        assert metadata.diagnostics["volume_subtitle_hint"] == {
            "base_series": "Invincible",
            "subtitle": "Out of This World",
            "issue_number": 9.0,
        }

    def test_publisher_prefixed_volume_subtitle_release_uses_actual_base_series(self) -> None:
        metadata = SourceMetadataExtractor().from_release_title(
            "Image.Comics-Drifter.Vol.01.Out.Of.The.Night.2015.Retail.Comic.eBook-BitBook"
        )

        assert metadata.series_name == "Drifter"
        assert metadata.issue_number == 1.0
        assert metadata.volume == "Vol 01"
        assert metadata.issue_type == IssueType.VOLUME
        assert metadata.diagnostics["volume_subtitle_hint"] == {
            "base_series": "Drifter",
            "subtitle": "Out Of The Night",
            "issue_number": 1.0,
        }

    def test_redundant_comicinfo_volume_subtitle_does_not_override_filename(
        self,
        tmp_path: Path,
    ) -> None:
        folder = tmp_path / "Incoming"
        folder.mkdir()
        archive = folder / "Immortal Thor v04 - The Son Of Thor (2025) (Digital).cbz"
        _write_cbz(
            archive,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Title>The Son Of Thor</Title>
              <Series>The Immortal Thor: The Son Of Thor</Series>
              <Number>1</Number>
              <Volume>4</Volume>
              <Publisher>Marvel</Publisher>
            </ComicInfo>
            """,
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.series_name == "Immortal Thor"
        assert metadata.issue_number == 4.0
        assert metadata.publisher == "Marvel"
        assert metadata.signals.get("series_name") is None
        assert metadata.signals["issue_number"] == MetadataSignal.RELEASE_TITLE
        assert metadata.diagnostics["comicinfo_series_ignored"] == {
            "reason": "redundant_volume_subtitle",
            "series": "The Immortal Thor: The Son Of Thor",
            "filename_series": "Immortal Thor",
            "subtitle": "The Son Of Thor",
            "issue_number": 4.0,
        }
        assert metadata.diagnostics["comicinfo_issue_number_ignored"] == {
            "reason": "filename_volume_conflict",
            "number": "1",
            "filename_volume": "v04",
            "filename_issue_number": 4.0,
        }

    def test_comicinfo_notes_can_supply_series_comicvine_id(self, tmp_path: Path) -> None:
        folder = tmp_path / "Batman (2016)"
        folder.mkdir()
        archive = folder / "Batman 001.cbz"
        _write_cbz(
            archive,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Batman</Series>
              <Volume>2016</Volume>
              <Notes>[cvid:97508]</Notes>
            </ComicInfo>
            """,
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.comicvine_series_id == 97508
        assert metadata.signals["comicvine_series_id"] == MetadataSignal.COMICINFO
        assert metadata.diagnostics["comicvine_series_id_source"] == "comicvine_note_id"

    def test_comicinfo_notes_can_supply_descriptive_comicvine_ids(
        self,
        tmp_path: Path,
    ) -> None:
        folder = tmp_path / "Batman (2016)"
        folder.mkdir()
        archive = folder / "Batman 001.cbz"
        _write_cbz(
            archive,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Batman</Series>
              <Volume>2016</Volume>
              <Notes>[cv_vol_id:97508] [cv_issue_id:123456]</Notes>
            </ComicInfo>
            """,
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.comicvine_series_id == 97508
        assert metadata.comicvine_issue_id == 123456
        assert metadata.signals["comicvine_series_id"] == MetadataSignal.COMICINFO
        assert metadata.signals["comicvine_issue_id"] == MetadataSignal.COMICINFO
        assert metadata.diagnostics["comicvine_series_id_source"] == "comicvine_note_id"

    def test_comicinfo_notes_volume_url_marks_series_id_trusted(self, tmp_path: Path) -> None:
        folder = tmp_path / "Batman (2016)"
        folder.mkdir()
        archive = folder / "Batman 001.cbz"
        _write_cbz(
            archive,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Batman</Series>
              <Volume>2016</Volume>
              <Notes>https://comicvine.gamespot.com/batman/4050-97508/</Notes>
            </ComicInfo>
            """,
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.comicvine_series_id == 97508
        assert metadata.diagnostics["comicvine_series_id_source"] == "comicvine_volume_url"

    def test_comicinfo_web_volume_url_is_series_id_not_issue_id(
        self,
        tmp_path: Path,
    ) -> None:
        folder = tmp_path / "Dark Nights Death Metal Omnibus"
        folder.mkdir()
        archive = folder / "Dark Nights - Death Metal Omnibus v01 (2024).cbz"
        _write_cbz(
            archive,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Dark Nights: Death Metal Omnibus</Series>
              <Number>1</Number>
              <Volume>209077</Volume>
              <Year>2024</Year>
              <Title>Death Metal Omnibus</Title>
              <Publisher>DC Comics</Publisher>
              <Web>https://comicvine.gamespot.com/dark-nights-death-metal-omnibus-1-hc/4050-166912/</Web>
              <Notes>Scraped metadata from ComicVine [CVDB1132072].</Notes>
            </ComicInfo>
            """,
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.comicvine_series_id == 166912
        assert metadata.comicvine_issue_id is None
        assert metadata.signals["comicvine_series_id"] == MetadataSignal.COMICINFO
        assert "comicvine_issue_id" not in metadata.signals
        assert metadata.diagnostics["comicvine_series_id_source"] == "comicvine_volume_url"

    def test_comicinfo_loose_cvdb_notes_do_not_supply_series_id(
        self,
        tmp_path: Path,
    ) -> None:
        folder = tmp_path / "Dark Nights Death Metal Omnibus"
        folder.mkdir()
        archive = folder / "Dark Nights - Death Metal Omnibus v01 (2024).cbz"
        _write_cbz(
            archive,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Dark Nights: Death Metal Omnibus</Series>
              <Number>1</Number>
              <Notes>Scraped metadata from ComicVine [CVDB1132072].</Notes>
            </ComicInfo>
            """,
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.comicvine_series_id is None
        assert metadata.comicvine_issue_id is None
        assert "comicvine_series_id" not in metadata.signals
        assert "comicvine_issue_id" not in metadata.signals

    def test_comicinfo_retailer_web_and_notes_are_ignored_for_matching(
        self,
        tmp_path: Path,
    ) -> None:
        folder = tmp_path / "Batman (2016)"
        folder.mkdir()
        archive = folder / "Batman 001.cbz"
        _write_cbz(
            archive,
            """<?xml version="1.0"?>
            <ComicInfo>
              <Series>Batman</Series>
              <Volume>2016</Volume>
              <Web>https://www.amazon.com/dp/B09BATMAN/ref=comixology</Web>
              <Notes>Comixology metadata [cvid:97508]</Notes>
            </ComicInfo>
            """,
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.comicvine_series_id is None
        assert metadata.comicvine_issue_id is None
        assert "comicvine_series_id" not in metadata.signals
        assert "comicvine_issue_id" not in metadata.signals
        assert metadata.diagnostics["comicinfo"]["web"] is None
        assert metadata.diagnostics["comicinfo"]["notes"] is None

    def test_folder_hints_are_preserved_when_comicinfo_missing(self, tmp_path: Path) -> None:
        folder = tmp_path / "Cairo Hardcover (2007)"
        folder.mkdir()
        archive = folder / "Cairo Hardcover (2007).cbz"
        _write_cbz(archive)

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.issue_type == IssueType.HC
        assert metadata.signals["issue_type"] == MetadataSignal.RELEASE_TITLE
        assert metadata.folder_issue_type == IssueType.HC

    def test_archive_page_names_can_supply_issue_mismatch_hint(
        self,
        tmp_path: Path,
    ) -> None:
        folder = tmp_path / "Hello Darkness"
        folder.mkdir()
        archive = folder / "Hello Darkness 020 (2026).cbz"
        _write_cbz_entries(
            archive,
            [
                f"Hello Darkness 020 (2026)/Hello Darkness 021 (2026)-{index:03d}.jpg"
                for index in range(8)
            ]
            + ["Hello Darkness 020 (2026)/zDCP.jpg"],
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.issue_number == 20.0
        assert metadata.diagnostics["has_comicinfo"] is False
        assert metadata.diagnostics["archive_entry_issue_hint"] == {
            "series_name": "Hello Darkness",
            "issue_number": 21.0,
            "year": 2026,
            "confidence": "strong",
            "total_image_entries": 9,
            "parseable_image_entries": 8,
            "matching_entry_count": 8,
            "sample_entries": [
                "Hello Darkness 020 (2026)/Hello Darkness 021 (2026)-000.jpg",
                "Hello Darkness 020 (2026)/Hello Darkness 021 (2026)-001.jpg",
                "Hello Darkness 020 (2026)/Hello Darkness 021 (2026)-002.jpg",
            ],
        }

    def test_archive_page_name_hint_ignores_bare_page_numbers(
        self,
        tmp_path: Path,
    ) -> None:
        folder = tmp_path / "Hello Darkness"
        folder.mkdir()
        archive = folder / "Hello Darkness 020 (2026).cbz"
        _write_cbz_entries(
            archive,
            [f"Hello Darkness 020 (2026)/{index:03d}.jpg" for index in range(8)],
        )

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.issue_number == 20.0
        assert "archive_entry_issue_hint" not in metadata.diagnostics
        assert metadata.diagnostics["archive_entry_issue_hint_checked"] is True

    def test_archive_identity_probe_can_skip_page_name_issue_hint(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        folder = tmp_path / "Hello Darkness"
        folder.mkdir()
        archive = folder / "Hello Darkness 020 (2026).cbz"
        _write_cbz(archive)

        def _unexpected_issue_hint(*args: object, **kwargs: object) -> None:
            raise AssertionError("identity probe must not inspect archive page names")

        monkeypatch.setattr(
            SourceMetadataExtractor,
            "archive_entry_issue_hint_from_path",
            _unexpected_issue_hint,
        )

        metadata = SourceMetadataExtractor().from_archive_path(
            archive,
            include_archive_entry_issue_hint=False,
        )

        assert metadata.diagnostics["archive_metadata_loaded"] is True
        assert "archive_entry_issue_hint_checked" not in metadata.diagnostics

    def test_archive_page_name_hint_skips_parsing_when_too_few_images(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        parse_calls = 0

        def _count_parse_calls(title: str) -> object:
            nonlocal parse_calls
            parse_calls += 1
            return None

        monkeypatch.setattr(source_metadata, "parse_release_title", _count_parse_calls)

        hint = source_metadata.archive_entry_issue_hint_from_names(
            [
                "Tiny Series/Tiny Series 002-001.jpg",
                "Tiny Series/Tiny Series 002-002.jpg",
                "Tiny Series/readme.txt",
            ],
            expected_series_name="Tiny Series",
        )

        assert hint is None
        assert parse_calls == 0

    def test_malformed_sidecars_do_not_crash(self, tmp_path: Path) -> None:
        folder = tmp_path / "Weird Book [TPB]"
        folder.mkdir()
        (folder / "series.json").write_text("{bad json")
        (folder / "cvinfo").write_text("comicid: nope\nbooktype:")
        archive = folder / "Weird Book.cbz"
        _write_cbz(archive)

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.series_name == "Weird Book"
        assert metadata.issue_type == IssueType.TPB
        assert metadata.comicvine_series_id is None

    def test_unicode_and_noisy_titles_remain_parseable(self, tmp_path: Path) -> None:
        folder = tmp_path / "Tommi Gunn Collected [2015] [TPB]"
        folder.mkdir()
        archive = folder / "Tommi Gunn Collected [2015] [TPB] [Digital] [ASO].cbz"
        _write_cbz(archive)

        metadata = SourceMetadataExtractor().from_archive_path(archive)

        assert metadata.series_name == "Tommi Gunn Collected"
        assert metadata.year == 2015
        assert metadata.issue_type == IssueType.TPB
