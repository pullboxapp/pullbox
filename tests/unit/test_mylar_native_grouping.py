"""Native Mylar grouping never steals recorded or reconciled source references."""

from copy import deepcopy

import pytest

from pullbox.core.collection_scanner import DiscoveredFile, DiscoveredSeries
from pullbox.services.import_native_grouping import group_mylar_native_extras


def source_group():
    file = DiscoveredFile(
        file_path="/source/Old Harbor/extra.cbz",
        file_name="extra.cbz",
        file_size=100,
        file_format="cbz",
        parsed_series="Old Harbor",
        parsed_issue_number=3,
        parsed_year=2020,
        parsed_publisher=None,
        has_comicinfo=False,
        comicvine_issue_id=None,
        issue_number_raw="3",
        source_signature={"size": 100, "mtime_ns": 22},
        metadata_diagnostics={
            "mylar3_unrecorded_file": {"expected_series": "Old Harbor"},
            "metron_import": {
                "primary_source": "metron",
                "identities": [
                    {"namespace": "metron", "kind": "series", "id": "21"},
                    {"namespace": "metron", "kind": "issue", "id": "101"},
                ],
                "problems": [],
                "number": "3",
            },
        },
    )
    return DiscoveredSeries(
        raw_series_name="Old Harbor",
        raw_year=2020,
        raw_publisher=None,
        source_folder="/source/Old Harbor",
        source_folder_relative="Old Harbor",
        file_count=1,
        sample_paths=[file.file_path],
        files=[file],
        mylar3_cv_id=900,
    )


@pytest.mark.parametrize(
    "record",
    [
        "recorded",
        "mylar3_issue",
        "mylar3_path_reconciliation",
        "mylar3_cross_folder_reconciliation",
    ],
)
def test_recorded_or_reconciled_mylar_reference_is_never_rebucketed(record):
    group = source_group()
    if record == "recorded":
        group.files[0].metadata_diagnostics.pop("mylar3_unrecorded_file")
    else:
        group.files[0].metadata_diagnostics[record] = {"issue_id": 901}
    original = deepcopy(group)
    assert group_mylar_native_extras(group) == [original]
    assert group == original


def test_native_extra_keeps_original_empty_mylar_series_and_source_evidence():
    group = source_group()
    original = deepcopy(group)
    recorded, native = group_mylar_native_extras(group)
    assert recorded.mylar3_cv_id == 900
    assert not recorded.files and recorded.file_count == 0 and not recorded.has_files
    assert native.mylar3_cv_id is None and native.file_count == 1
    assert native.files[0].source_signature == original.files[0].source_signature
    assert native.source_folder == recorded.source_folder == original.source_folder
    assert group == original
