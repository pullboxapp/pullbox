"""Runtime visibility checks for import file-execution collaborator protocols."""

from pullbox.services import import_file_execution_protocols as protocols


def test_import_file_execution_protocols_remain_named_contracts() -> None:
    expected = {
        "ApplyComicInfoFunc",
        "BuildComicInfoPayloadFunc",
        "CleanupPreparedFileFunc",
        "LoadIngestPolicyFunc",
        "LoadMediaSettingsFunc",
        "LoadPermissionPolicyFunc",
        "LoadTrashDirFunc",
        "LogEventFunc",
        "MoveToTrashFunc",
        "PrepareImportFileFunc",
        "RaiseIfCancelledFunc",
        "RecordActionFunc",
        "RegisterLibraryFileFunc",
        "ReportFileProgressFunc",
    }

    assert expected <= set(vars(protocols))
    assert all(getattr(protocols, name)._is_protocol for name in expected)
