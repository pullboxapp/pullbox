"""Unchanged manual corrections remain reviewable across process restarts."""

import os
import subprocess
import sys


def test_preview_evidence_is_independent_of_python_hash_seed():
    code = """
from datetime import UTC, datetime
from pathlib import Path
from pullbox.schemas.metadata_snapshot import MetadataValues
from pullbox.services.metadata_series_refresh_state import RefreshEntityState, SeriesRefreshState
from pullbox.services.archive_metadata_binding import ArchiveMetadataTarget, ArchiveMetadataBinding
from pullbox.services.series_metadata_propagation_preview import stable_propagation_evidence
import hashlib
state = RefreshEntityState(1, MetadataValues(title='Example'), None, 0, (), 0, None,
    frozenset({'language', 'publisher', 'sort_title'}))
binding = ArchiveMetadataBinding(1, 1, '/comics', '/comics/example.cbz', 100,
    datetime(2026,1,1,tzinfo=UTC), SeriesRefreshState(state,(state,),()))
target = ArchiveMetadataTarget(binding,Path('/comics/example.cbz'),(1,2,100,4,5,33152),())
for value in (state,target):
    print(hashlib.sha256(stable_propagation_evidence(value)).hexdigest())
"""
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONHASHSEED": seed},
            text=True,
            timeout=30,
        )
        for seed in ("1", "2", "3")
    ]
    assert len(set(outputs)) == 1, "Identical metadata must not become stale after restart"
