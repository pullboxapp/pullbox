"""Short-lived actor-bound previews for individual review mutations."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from itsdangerous import BadSignature, URLSafeTimedSerializer

from pullbox.core.config_resolver import get_application_secret
from pullbox.core.exceptions import ValidationError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from pullbox.models.import_job import ImportedFile, ImportedSeries, ImportJob


def review_scope(job: ImportJob, parent: ImportedSeries, file: ImportedFile) -> str:
    diagnostics = {
        key: value for key, value in file.diagnostics.items() if key != "review_source_action"
    }
    payload = [
        job.id,
        job.status.value,
        job.control_request.value,
        job.source_path,
        job.mylar3_path_map,
        job.file_handling_mode.value,
        parent.id,
        parent.cv_id,
        parent.user_selected_cv_id,
        parent.series_id,
        file.id,
        file.file_path,
        file.status.value,
        file.matched_issue_id,
        file.matched_issue_cv_id,
        file.include_in_import,
        file.conflict_group_id,
        file.source_signature,
        diagnostics,
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


async def sign_review_scope(session: AsyncSession, payload: dict[str, Any]) -> str:
    signer = URLSafeTimedSerializer(get_application_secret(), salt="import-review-file-v1")
    return signer.dumps(payload)


async def verify_review_scope(session: AsyncSession, token: str, expected: dict[str, Any]) -> None:
    signer = URLSafeTimedSerializer(get_application_secret(), salt="import-review-file-v1")
    try:
        actual = signer.loads(token, max_age=900)
    except BadSignature as exc:
        raise ValidationError("This preview expired. Open the action again.") from exc
    if actual != expected:
        raise ValidationError("This file changed after the preview. Open the action again.")
