"""Unit tests for trusted known ComicVine ID import match helpers."""

from __future__ import annotations

from pullbox.core.source_metadata import MetadataSignal, SourceMetadata
from pullbox.providers.base import SeriesMetadata
from pullbox.services.import_known_cv_match import (
    known_cv_id_evaluation_from_source,
    trusted_known_cv_id_without_fetch,
)


def _series_metadata() -> SeriesMetadata:
    return SeriesMetadata(
        provider_id="97508",
        title="Batman",
        sort_title="batman",
        year_start=2016,
        year_end=None,
        status="Ended",
        publisher="DC Comics",
        description=None,
        cover_url=None,
        issue_count=85,
        comicvine_url="https://comicvine.gamespot.com/batman/4050-97508/",
    )


def test_trusted_known_cv_id_accepts_mylar_and_explicit_volume_urls() -> None:
    assert trusted_known_cv_id_without_fetch(
        mylar3_cv_id=97508,
        comicinfo_cv_id=None,
        source_metadata=SourceMetadata(original_title="Batman.cbz", series_name="Batman"),
    )
    assert trusted_known_cv_id_without_fetch(
        mylar3_cv_id=None,
        comicinfo_cv_id=97508,
        source_metadata=SourceMetadata(
            original_title="Batman.cbz",
            series_name="Batman",
            comicvine_series_id=97508,
            signals={"comicvine_series_id": MetadataSignal.COMICINFO},
            diagnostics={"comicvine_series_id_source": "comicvine_volume_url"},
        ),
    )


def test_trusted_known_cv_id_accepts_structured_notes_and_sidecars() -> None:
    for signal, source in (
        (MetadataSignal.COMICINFO, "comicvine_note_id"),
        (MetadataSignal.SIDECAR, "sidecar"),
    ):
        assert trusted_known_cv_id_without_fetch(
            mylar3_cv_id=None,
            comicinfo_cv_id=97508,
            source_metadata=SourceMetadata(
                original_title="Batman.cbz",
                series_name="Batman",
                comicvine_series_id=97508,
                signals={"comicvine_series_id": signal},
                diagnostics={"comicvine_series_id_source": source},
            ),
        )


def test_trusted_known_cv_id_rejects_loose_comicinfo_ids() -> None:
    assert not trusted_known_cv_id_without_fetch(
        mylar3_cv_id=None,
        comicinfo_cv_id=97508,
        source_metadata=SourceMetadata(original_title="Batman.cbz", series_name="Batman"),
    )


def test_trusted_known_cv_id_rejects_conflicting_explicit_ids() -> None:
    assert not trusted_known_cv_id_without_fetch(
        mylar3_cv_id=None,
        comicinfo_cv_id=97508,
        source_metadata=SourceMetadata(
            original_title="Batman.cbz",
            series_name="Batman",
            comicvine_series_id=97508,
            signals={"comicvine_series_id": MetadataSignal.SIDECAR},
            diagnostics={
                "comicvine_series_id_source": "sidecar",
                "identity_conflicts": [
                    {
                        "field": "comicvine_series_id",
                        "comicinfo": 97508,
                        "sidecar": 12345,
                    }
                ],
            },
        ),
    )


def test_known_cv_id_evaluation_from_source_marks_verification_deferred() -> None:
    evaluation = known_cv_id_evaluation_from_source(
        97508,
        match_method="comicinfo_cv_id",
        raw_name="Batman",
        raw_year=2016,
        normalized_query="batman",
        source_metadata=SourceMetadata(
            original_title="Batman 001.cbz",
            series_name="Batman",
            year=2016,
            publisher="DC Comics",
            issue_count_hint=85,
        ),
        match_threshold=0.80,
    )

    assert evaluation.match == {
        "cv_id": 97508,
        "cv_title": "Batman",
        "cv_year": 2016,
        "cv_publisher": "DC Comics",
        "cv_issue_count": 85,
        "cv_url": None,
        "cv_match_score": 1.0,
        "cv_match_method": "comicinfo_cv_id",
    }
    assert evaluation.diagnostics["reason"] == "trusted_known_cv_id_unverified"
    assert evaluation.diagnostics["selected_candidate"]["verification"] == "deferred"


def test_known_cv_id_evaluation_from_metadata_uses_authoritative_provider_fields() -> None:
    from pullbox.services.import_known_cv_match import known_cv_id_evaluation_from_metadata

    evaluation = known_cv_id_evaluation_from_metadata(
        _series_metadata(),
        match_method="mylar3_cv_id",
        raw_name="Bat Man",
        raw_year=2015,
        normalized_query="bat man",
        match_threshold=0.80,
        reason="known_cv_id_cached",
    )

    assert evaluation.match is not None
    assert evaluation.match["cv_id"] == 97508
    assert evaluation.match["cv_title"] == "Batman"
    assert evaluation.match["cv_year"] == 2016
    assert evaluation.match["cv_url"] == "https://comicvine.gamespot.com/batman/4050-97508/"
    assert evaluation.diagnostics["reason"] == "known_cv_id_cached"
