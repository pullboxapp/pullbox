"""One naming editor preserves global inheritance and explicit library policies."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pullbox.models.config import SystemConfig
from pullbox.models.library import LibraryRoot, LibraryRootPolicy, LibraryRootPolicySource
from pullbox.services.auth_service import SESSION_COOKIE_NAME, AuthService

if TYPE_CHECKING:
    from httpx import AsyncClient

pytest_plugins = ["conftest_security"]


def _csrf(client: AsyncClient) -> dict[str, str]:
    token = client.cookies.get(SESSION_COOKIE_NAME) or ""
    return {"X-CSRF-Token": AuthService.get_csrf_token_from_session(token) or ""}


async def _state(client: AsyncClient, root_id: int | None = None):
    response = await client.get(
        "/api/v1/config/naming", params={"library_root_id": root_id} if root_id else {}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _save(client: AsyncClient, state, *, policy=None, use_global=False):
    return await client.put(
        "/api/v1/config/naming",
        headers=_csrf(client),
        json={
            "library_root_id": state["library_root_id"],
            "expected_fingerprint": state["fingerprint"],
            "policy": policy or state["policy"],
            "use_global": use_global,
        },
    )


async def test_media_has_one_scoped_naming_editor(authenticated_client: AsyncClient):
    page = await authenticated_client.get("/settings?tab=media")
    assert page.status_code == 200
    assert 'data-testid="settings-naming-editor"' in page.text
    assert "Library-specific naming" not in page.text
    assert "Use global naming defaults" in page.text
    for field in (
        "comic_file_template",
        "annual_file_template",
        "non_standard_file_template",
        "single_non_standard_file_template",
    ):
        assert page.text.count(f'name="{field}"') == 1
    assert page.text.count('name="replace_illegal_characters"') == 1
    assert page.text.count('name="colon_replacement"') == 1
    assert 'name="rename_on_import"' in page.text


async def test_global_and_library_naming_roundtrip_preserves_files_and_other_config(
    authenticated_client: AsyncClient, sec_db, tmp_path: Path
):
    existing = tmp_path / "issue 01.cbz"
    existing.write_bytes(b"leave the user's file alone")
    async with sec_db() as session:
        root = LibraryRoot(name="Comics", path=str(tmp_path), enabled=True)
        other = LibraryRoot(name="Archive", path=str(tmp_path / "archive"), enabled=True)
        session.add_all(
            [root, other, SystemConfig(key="rename_on_import", value="false", value_type="bool")]
        )
        await session.commit()
        root_id, other_id = root.id, other.id
    global_state = await _state(authenticated_client)
    inherited = await _state(authenticated_client, root_id)
    assert inherited["use_global"] is True
    custom = {
        **inherited["policy"],
        "series_path_template": "{Publisher}/{Series} ({Year})",
        "colon_replacement": "space",
    }
    response = await _save(authenticated_client, inherited, policy=custom)
    assert response.status_code == 200, response.text
    assert response.json()["use_global"] is False
    global_policy = {**global_state["policy"], "series_path_template": "{Series} [{Year}]"}
    assert (
        await _save(authenticated_client, global_state, policy=global_policy)
    ).status_code == 200
    assert (await _state(authenticated_client, root_id))["policy"] == custom
    assert (await _state(authenticated_client, other_id))["policy"] == global_policy
    custom_state = await _state(authenticated_client, root_id)
    restored = await _save(authenticated_client, custom_state, use_global=True)
    assert restored.status_code == 200
    assert restored.json()["use_global"] is True
    assert restored.json()["policy"] == global_policy
    async with sec_db() as session:
        assert await session.scalar(select(LibraryRootPolicy)) is None
        assert (await session.get(SystemConfig, "rename_on_import")).value == "false"
    assert existing.read_bytes() == b"leave the user's file alone"
    assert list(tmp_path.iterdir()) == [existing]


@pytest.mark.parametrize(
    "field,value",
    [
        ("series_path_template", "../{Series}"),
        ("comic_file_template", "{Series}/issue"),
        ("annual_file_template", "{Unknown}"),
    ],
)
async def test_invalid_naming_rejected_by_save_and_preview(
    authenticated_client: AsyncClient, field, value
):
    state = await _state(authenticated_client)
    policy = {**state["policy"], field: value}
    saved = await _save(authenticated_client, state, policy=policy)
    assert saved.status_code == 422
    preview = await authenticated_client.post(
        "/api/v1/config/naming/preview",
        json={"policy": policy},
        headers=_csrf(authenticated_client),
    )
    assert preview.status_code == 422
    assert (await _state(authenticated_client))["fingerprint"] == state["fingerprint"]


async def test_naming_stale_save_is_rejected(authenticated_client: AsyncClient):
    state = await _state(authenticated_client)
    assert (
        await _save(
            authenticated_client,
            state,
            policy={**state["policy"], "series_path_template": "{Publisher}/{Series}"},
        )
    ).status_code == 200
    stale = await _save(authenticated_client, state)
    assert stale.status_code == 409


async def test_shared_preview_uses_nested_paths_and_character_cleanup(
    authenticated_client: AsyncClient,
):
    state = await _state(authenticated_client)
    policy = {
        **state["policy"],
        "series_path_template": "{Publisher}/{Series} ({Year})",
        "comic_file_template": "{Series}: #{Issue:03d}",
        "colon_replacement": "space",
    }
    preview = await authenticated_client.post(
        "/api/v1/config/naming/preview",
        json={"policy": policy},
        headers=_csrf(authenticated_client),
    )
    assert preview.status_code == 200, preview.text
    examples = preview.json()["examples"]
    assert examples["series_path_template"][0]["output"] == "DC Comics/Absolute Batman (2024)"
    assert examples["comic_file_template"][0]["output"] == "Absolute Batman #017.cbz"
    assert len(examples) == 5
    assert all(examples.values())


async def test_import_adopted_policy_is_preserved_until_explicit_save(
    authenticated_client: AsyncClient, sec_db, tmp_path: Path
):
    defaults = await _state(authenticated_client)
    async with sec_db() as session:
        root = LibraryRoot(name="Imported library", path=str(tmp_path), enabled=True)
        session.add(root)
        await session.flush()
        policy = LibraryRootPolicy(
            library_root_id=root.id,
            **defaults["policy"],
            source=LibraryRootPolicySource.IMPORT_ADOPTION,
            revision=4,
        )
        session.add(policy)
        await session.commit()
        root_id, policy_id = root.id, policy.id
    state = await _state(authenticated_client, root_id)
    assert state["source"] == "import_adoption"
    assert state["revision"] == 4
    await authenticated_client.post(
        "/api/v1/config/naming/preview",
        json={"policy": state["policy"]},
        headers=_csrf(authenticated_client),
    )
    assert (await _state(authenticated_client, root_id)) == state
    response = await _save(
        authenticated_client,
        state,
        policy={**state["policy"], "comic_file_template": "{Series} #{Issue}"},
    )
    assert response.status_code == 200
    assert response.json()["source"] == "manual"
    assert response.json()["revision"] == 5
    assert (await _save(authenticated_client, state, use_global=True)).status_code == 409
    async with sec_db() as session:
        stored = await session.get(LibraryRootPolicy, policy_id)
        assert stored is not None
        assert stored.revision == 5


async def test_unknown_scope_never_falls_back_to_global(authenticated_client: AsyncClient):
    defaults = await _state(authenticated_client)
    missing = {**defaults, "library_root_id": 999999}
    assert (await _save(authenticated_client, missing)).status_code == 404
    assert (await _state(authenticated_client)) == defaults
