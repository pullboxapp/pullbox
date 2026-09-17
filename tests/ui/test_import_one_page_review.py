"""Compact one-page review and exact series-scoped safety decisions."""

import re
from unittest.mock import Mock

import pytest

from pullbox.models.import_job import ImportedFile, ImportedFileStatus, ImportedSeries, ImportJob
from pullbox.services.import_safety_diagnostics import ImportSafetyCategory
from tests.ui.test_import_safety_bulk_ui import (
    _csrf_header_for,
    _safety_block,
    _seed_bulk_safety_job,
)

pytest_plugins = ["conftest_security"]


async def _seed_one_page_job(sec_db, count=2):
    seeded = await _seed_bulk_safety_job(sec_db, include_dangerous=True)
    async with sec_db() as session:
        for file_id in seeded["file_ids"][:count]:
            file = await session.get(ImportedFile, file_id)
            file.file_size = 1_800_000
            file.diagnostics = {
                "safety_block": _safety_block(
                    ImportSafetyCategory.SINGLE_PAGE_COMIC, overrideable=True
                ),
                "content_inspection": {"page_count": 1},
                "archive_format": {"detected": "cbz"},
            }
        await session.commit()
    return seeded


async def _review(client, seeded):
    return await client.get(
        f"/import/{seeded['job_id']}/review-partial?status=decide&reason=single_page_comic"
    )


def _token(html, action):
    match = re.search(
        rf'data-testid="import-review-one-page-{action}".*?name="token" value="([^"]+)"',
        html,
        re.DOTALL,
    )
    assert match is not None
    return match.group(1)


@pytest.mark.parametrize("count,label", [(1, "Allow All"), (2, "Allow All")])
async def test_one_page_review_is_compact_and_has_explicit_series_actions(
    authenticated_client, sec_db, count, label
):
    seeded = await _seed_one_page_job(sec_db, count)
    response = await _review(authenticated_client, seeded)
    assert response.status_code == 200
    html = response.text
    assert f"{count} {'file' if count == 1 else 'files'} in 1 series" in html
    assert f"Skip all {count}" in html
    assert "Review one by one" not in html
    assert 'data-testid="import-review-one-page-review"' not in html
    assert 'data-testid="import-review-one-page-helper"' in html
    assert label in html
    assert re.search(r">\s*Skip All\s*</button>", html)
    assert 'data-testid="import-review-more-actions"' not in html
    assert 'data-testid="import-review-one-page-files"' in html
    assert "1.7 MB" in html
    assert "ZIP" in html and "1 page" in html
    assert "Select all ready" not in html
    assert "Why this needs review" not in html
    compact = html.split('data-testid="import-review-one-page-files"', 1)[1].split("</section>", 1)[
        0
    ]
    assert "Change series" not in compact
    assert "Source files" not in compact
    assert "damaged archive" not in compact
    assert 'data-testid="import-review-one-page-more"' not in html
    assert "Change series" not in html
    assert "Source files" not in html
    assert "other reasons in details" not in html
    assert "What needs attention" in html
    assert '<span class="sr-only">Select</span>' not in html
    assert '<span class="sr-only">Details</span>' in html
    assert "0/4" in html
    assert "Cover art, a damaged download, or an intentional one-pager." in html
    assert "One-page archives</p>" in compact
    assert ">Allow</button>" in compact
    assert ">View File</button>" in compact
    assert compact.index(">View File</button>") < compact.index(">Allow</button>")
    assert "Allow once" not in compact
    assert 'data-testid="import-review-one-page-category-actions"' in html
    assert 'data-testid="import-review-one-page-detection"' in html
    assert "1 image page" in html
    assert "of 3 members" not in html
    assert "/private/import" not in html
    assert _token(html, "allow")


@pytest.mark.parametrize("action", ["allow", "skip"])
async def test_series_shortcut_only_changes_its_one_page_files(
    authenticated_client, sec_db, monkeypatch, action
):
    from pullbox.tasks import import_task

    rematch = Mock()
    monkeypatch.setattr(import_task, "trigger_import_series_rematch", rematch)
    seeded = await _seed_one_page_job(sec_db)
    other = await _seed_one_page_job(sec_db)
    html = (await _review(authenticated_client, seeded)).text
    response = await authenticated_client.post(
        f"/import/{seeded['job_id']}/series/{seeded['series_id']}/one-page/{action}"
        "?status=decide&reason=single_page_comic",
        data={"token": _token(html, action)},
        headers=_csrf_header_for(authenticated_client),
    )
    assert response.status_code == 200
    async with sec_db() as session:
        files = [await session.get(ImportedFile, i) for i in seeded["file_ids"]]
        expected = (
            ImportedFileStatus.SAFETY_APPROVED if action == "allow" else ImportedFileStatus.SKIPPED
        )
        assert [file.status for file in files[:2]] == [expected, expected]
        assert all(file.status == ImportedFileStatus.SAFETY_BLOCKED for file in files[2:])
        assert not any(file.include_in_import for file in files[:2])
        for file_id in other["file_ids"]:
            assert (
                await session.get(ImportedFile, file_id)
            ).status == ImportedFileStatus.SAFETY_BLOCKED
        parent = await session.get(ImportedSeries, seeded["series_id"])
        assert parent.selected_for_import is False
    if action == "allow":
        rematch.assert_called_once_with(seeded["job_id"], seeded["series_id"])
    else:
        rematch.assert_not_called()


@pytest.mark.parametrize("change", ["file", "job", "actor", "series", "action", "added", "control"])
async def test_series_shortcut_rejects_changed_or_wrong_scope_without_partial_approval(
    authenticated_client, sec_db, change
):
    from pullbox.models.import_job import ImportJobStatus

    seeded = await _seed_one_page_job(sec_db)
    html = (await _review(authenticated_client, seeded)).text
    token = _token(html, "skip" if change == "action" else "allow")
    series_id = seeded["series_id"]
    async with sec_db() as session:
        if change == "file":
            file = await session.get(ImportedFile, seeded["file_ids"][1])
            file.diagnostics = {
                "safety_block": _safety_block(
                    ImportSafetyCategory.DANGEROUS_PATH_OR_PAYLOAD, overrideable=True
                )
            }
        elif change == "job":
            (await session.get(ImportJob, seeded["job_id"])).status = ImportJobStatus.COMPLETED
        elif change == "actor":
            from itsdangerous import URLSafeTimedSerializer

            from pullbox.core.config_resolver import get_application_secret

            signer = URLSafeTimedSerializer(get_application_secret(), salt="import-review-file-v1")
            scope = signer.loads(token)
            scope["actor"] += 1
            token = signer.dumps(scope)
        elif change == "series":
            parent = ImportedSeries(import_job_id=seeded["job_id"], raw_series_name="Other")
            session.add(parent)
            await session.flush()
            series_id = parent.id
        elif change == "added":
            session.add(
                ImportedFile(
                    import_job_id=seeded["job_id"],
                    import_series_id=series_id,
                    file_name="New cover.cbz",
                    file_path="/private/import/New cover.cbz",
                    file_size=1000,
                    file_format="cbz",
                    status=ImportedFileStatus.SAFETY_BLOCKED,
                    diagnostics={
                        "safety_block": _safety_block(
                            ImportSafetyCategory.SINGLE_PAGE_COMIC, overrideable=True
                        )
                    },
                )
            )
        elif change == "control":
            from pullbox.models.import_job import ImportControlRequest

            (
                await session.get(ImportJob, seeded["job_id"])
            ).control_request = ImportControlRequest.CANCEL
        await session.commit()
    response = await authenticated_client.post(
        f"/import/{seeded['job_id']}/series/{series_id}/one-page/allow",
        data={"token": token},
        headers=_csrf_header_for(authenticated_client),
    )
    assert response.status_code == 409
    async with sec_db() as session:
        for file_id in seeded["file_ids"]:
            assert (
                await session.get(ImportedFile, file_id)
            ).status == ImportedFileStatus.SAFETY_BLOCKED


async def test_one_page_shortcut_cannot_approve_an_ineligible_file(authenticated_client, sec_db):
    seeded = await _seed_one_page_job(sec_db)
    async with sec_db() as session:
        file = await session.get(ImportedFile, seeded["file_ids"][1])
        file.diagnostics = {
            "safety_block": _safety_block(
                ImportSafetyCategory.SINGLE_PAGE_COMIC, overrideable=False
            )
        }
        await session.commit()
    html = (await _review(authenticated_client, seeded)).text
    assert 'data-testid="import-review-one-page-allow"' not in html
    assert 'data-testid="import-review-one-page-skip"' in html


async def test_one_page_shortcut_requires_csrf(authenticated_client, sec_db):
    seeded = await _seed_one_page_job(sec_db)
    html = (await _review(authenticated_client, seeded)).text
    response = await authenticated_client.post(
        f"/import/{seeded['job_id']}/series/{seeded['series_id']}/one-page/allow",
        data={"token": _token(html, "allow")},
    )
    assert response.status_code == 403
    async with sec_db() as session:
        for file_id in seeded["file_ids"]:
            assert (
                await session.get(ImportedFile, file_id)
            ).status == ImportedFileStatus.SAFETY_BLOCKED
