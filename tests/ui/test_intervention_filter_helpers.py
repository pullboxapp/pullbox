"""Tests for intervention filter and metadata helpers."""

from __future__ import annotations

from types import SimpleNamespace

from pullbox.models.pending_match import PendingMatchStatus


def test_intervention_filter_normalizers_keep_invalid_values_safe() -> None:
    """Intervention filters should normalize unknown UI/query values."""
    from pullbox.ui.intervention_filter_helpers import (
        normalize_intervention_confidence_filter,
        normalize_intervention_history_sort,
        normalize_intervention_lane,
        normalize_intervention_outcome_filter,
        normalize_intervention_protocol_filter,
        normalize_intervention_reason_filter,
        normalize_intervention_tab,
    )

    assert normalize_intervention_tab("history") == "history"
    assert normalize_intervention_tab("nonsense") == "queue"
    assert normalize_intervention_confidence_filter("HIGH") == "high"
    assert normalize_intervention_confidence_filter("certain") == ""
    assert normalize_intervention_reason_filter("size_warning") == "size_warning"
    assert normalize_intervention_reason_filter("needs_review") == ""
    assert normalize_intervention_protocol_filter("TORRENT") == "torrent"
    assert normalize_intervention_protocol_filter("DIRECT") == "direct"
    assert normalize_intervention_protocol_filter("DC") == "dc"
    assert normalize_intervention_protocol_filter("magnet") == ""
    assert normalize_intervention_outcome_filter("APPROVED") == "approved"
    assert normalize_intervention_outcome_filter("pending") == ""
    assert normalize_intervention_history_sort("title") == "title"
    assert normalize_intervention_history_sort("-confidence") == "-confidence"
    assert normalize_intervention_history_sort("created_at") == "-resolved_at"
    assert normalize_intervention_lane("recovery") == "recovery"
    assert normalize_intervention_lane("unexpected") == "review"


def test_dc_intervention_protocol_label_is_not_usenet() -> None:
    from pullbox.ui.intervention_filter_helpers import intervention_protocol_label

    assert intervention_protocol_label(False, "dc") == "Direct Connect"


def test_intervention_review_reason_labels_and_summaries() -> None:
    """Review reason helpers should preserve the established display contract."""
    from pullbox.ui.intervention_filter_helpers import (
        intervention_review_reason_codes,
        intervention_review_reason_summary,
    )

    reason_codes = intervention_review_reason_codes(
        {
            "series_match_type": "fuzzy",
            "issue_match": False,
            "year_match": False,
            "type_match": False,
            "size_warning": True,
        }
    )

    assert reason_codes == [
        "fuzzy_series",
        "issue_mismatch",
        "year_mismatch",
        "type_mismatch",
        "size_warning",
    ]
    assert intervention_review_reason_codes({}) == ["needs_review"]
    assert (
        intervention_review_reason_summary(reason_codes) == "Fuzzy series match · Issue mismatch +3"
    )
    assert intervention_review_reason_summary(["size_warning"]) == "Size warning"


def test_build_intervention_item_meta_formats_protocol_outcome_and_source() -> None:
    """Pending-match metadata should remain template-ready after extraction."""
    from pullbox.ui.intervention_filter_helpers import build_intervention_item_meta

    pending_match = SimpleNamespace(
        is_torrent=True,
        status=PendingMatchStatus.REJECTED,
        indexer=SimpleNamespace(name="  Prowlarr Source  "),
        match_details={
            "series_match_type": "exact",
            "issue_match": True,
            "year_match": True,
            "type_match": True,
            "series_similarity": 0.946,
            "indexer_name": "Ignored",
            "rejection_reason": "Wrong year",
        },
    )

    assert build_intervention_item_meta(pending_match) == {
        "protocol_label": "Torrent",
        "outcome_label": "Rejected",
        "review_reason_codes": ["needs_review"],
        "review_reason_labels": ["Needs review"],
        "review_reason_summary": "Needs review",
        "match_type_label": "Exact series match",
        "similarity_pct": 94.6,
        "source_label": "Prowlarr Source",
        "rejection_reason": "Wrong year",
        "lane": "review",
        "recovery_summary": "",
    }


def test_build_intervention_item_meta_falls_back_to_match_details_source() -> None:
    """Source labels should fall back to match details when no indexer row exists."""
    from pullbox.ui.intervention_filter_helpers import build_intervention_item_meta

    pending_match = SimpleNamespace(
        is_torrent=False,
        status="expired",
        indexer=None,
        match_details={
            "series_match_type": "fuzzy",
            "issue_match": False,
            "indexer_name": "NZBgeek",
        },
    )

    meta = build_intervention_item_meta(pending_match)

    assert meta["protocol_label"] == "Usenet"
    assert meta["outcome_label"] == "Expired"
    assert meta["match_type_label"] == "Fuzzy series match"
    assert meta["source_label"] == "NZBgeek"
    assert meta["review_reason_summary"] == "Fuzzy series match · Issue mismatch"


def test_build_intervention_item_meta_labels_direct_provider() -> None:
    """Direct adapter rows identify their provider and selected artifact host."""
    from pullbox.ui.intervention_filter_helpers import build_intervention_item_meta

    pending_match = SimpleNamespace(
        is_torrent=False,
        status="pending",
        indexer=None,
        match_details={
            "source_kind": "direct",
            "provider_name": "pullbox.getcomics",
            "provider_identity": "pullbox.getcomics",
            "artifact_host_kind": "datanodes",
            "series_match_type": "exact",
        },
    )

    meta = build_intervention_item_meta(pending_match)

    assert meta["protocol_label"] == "Direct"
    assert meta["source_label"] == "GetComics via DataNodes"


def test_build_intervention_item_meta_identifies_direct_acquisition_recovery() -> None:
    """Failed direct acquisition is not presented as an ambiguous match review."""
    from pullbox.ui.intervention_filter_helpers import build_intervention_item_meta

    pending_match = SimpleNamespace(
        is_torrent=False,
        status="pending",
        indexer=None,
        match_details={
            "source_kind": "direct",
            "provider_name": "pullbox.getcomics",
            "artifact_host_kind": "terabox",
            "failure_class": "artifact_host_auth_required",
            "failure_code": "artifact_host_auth_required",
            "series_match_type": "exact",
        },
    )

    meta = build_intervention_item_meta(pending_match)

    assert meta["lane"] == "recovery"
    assert meta["recovery_summary"] == "TeraBox authentication required"
