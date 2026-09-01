"""Route-contract tests for the rewritten settings shell."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime

import pytest

from pullbox.config import get_settings
from pullbox.core.encryption import encrypt_secret
from pullbox.models.client import DownloadClientConfig
from pullbox.models.config import SystemConfig
from pullbox.models.direct_acquisition import (
    DirectArtifactHostKind,
    DirectHostAccountState,
    DirectHostConfig,
    DirectProviderConfig,
    DirectProviderState,
    DirectProviderTrustLevel,
)
from pullbox.models.download import DownloadClientType
from pullbox.models.indexer import IndexerConfig, IndexerType
from pullbox.models.library import LibraryRoot
from pullbox.utilities.settings import resolve_utility_directory

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-settings-ui")


def _script_block(html: str, start_marker: str, end_marker: str) -> str:
    start = html.index(start_marker)
    end = html.index(end_marker, start)
    return html[start:end]


def _direct_manifest(
    provider_id: str,
    *,
    artifact_hosts: list[str],
) -> dict[str, object]:
    return {
        "protocol_version": "direct-download-provider/v1",
        "provider_id": provider_id,
        "display_name": "Direct Provider",
        "description": "A direct provider fixture.",
        "provider_version": "1.0.0",
        "supported_protocol_versions": ["direct-download-provider/v1"],
        "publisher": "Pullbox",
        "license": "GPL-3.0-or-later",
        "source_domains": ["provider.example"],
        "artifact_host_patterns": artifact_hosts,
        "capabilities": {
            "search": True,
            "resolve": True,
            "health": True,
            "configuration_schema": True,
        },
        "configuration_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }


@pytest.mark.asyncio
class TestSettingsRouteContracts:
    """Verify the settings area renders a stable mounted shell."""

    async def test_settings_clients_prefers_persisted_server_status_message(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        async with sec_db() as session:
            session.add(
                DownloadClientConfig(
                    name="Stable SAB",
                    client_type=DownloadClientType.SABNZBD,
                    url="http://localhost:8080",
                    enabled=True,
                    priority=10,
                    last_success_at=datetime.now(UTC),
                    last_test_message="SABnzbd 4.5.1",
                )
            )
            await session.commit()

        response = await authenticated_client.get("/settings?tab=clients")

        assert response.status_code == 200
        assert "SABnzbd 4.5.1" in response.text
        assert "Connection healthy." not in response.text
        assert "localhost</code> and <code" in response.text
        assert "pbFormatDurationMs(data.response_time_ms)" in response.text

    async def test_settings_clients_describes_process_completed_as_recovery_sweep(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/settings?tab=clients")

        assert response.status_code == 200
        assert "Recovery Sweep Interval" in response.text
        assert (
            "Normal completion handoff is triggered immediately when downloads finish."
            in response.text
        )
        assert 'name="process_completed_interval_seconds"' in response.text
        assert 'value="300"' in response.text
        assert 'min="300"' in response.text

    async def test_settings_clients_uses_simple_download_mapping_copy(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/settings?tab=clients")

        assert response.status_code == 200
        assert "Path inside Pullbox where completed downloads are mounted" in response.text
        assert 'usually <code class="text-pb-text-sec">/downloads</code>' in response.text
        assert (
            "Path reported by the client; only set this when it differs from Pullbox."
            in response.text
        )
        assert 'placeholder="/downloads"' in response.text
        assert 'placeholder="/data/downloads"' in response.text

    async def test_settings_clients_feature_flags_airdcpp_picker_and_renders_contract(
        self,
        authenticated_client,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setenv("PULLBOX_AIRDCPP_ENABLED", "false")
        get_settings.cache_clear()
        disabled = await authenticated_client.get("/settings?tab=clients")
        assert disabled.status_code == 200
        assert 'data-testid="settings-clients-picker-airdcpp"' not in disabled.text

        monkeypatch.setenv("PULLBOX_AIRDCPP_ENABLED", "true")
        get_settings.cache_clear()
        enabled = await authenticated_client.get("/settings?tab=clients")
        get_settings.cache_clear()

        assert enabled.status_code == 200
        assert 'data-testid="settings-clients-picker-airdcpp"' in enabled.text
        assert "AirDC++" in enabled.text
        assert "Direct Connect" in enabled.text
        assert 'data-testid="settings-clients-airdcpp-fields"' in enabled.text
        assert "Minimum Search Interval" in enabled.text
        assert 'min="45"' in enabled.text
        assert "Leave this empty to search every hub currently connected" in enabled.text
        assert "body.airdcpp =" in enabled.text
        assert "minimum_search_interval_seconds" in enabled.text
        assert "actual transfer reachability is validated during a real download" in enabled.text
        assert 'data-testid="settings-clients-airdcpp-client-priority"' in enabled.text
        assert 'data-testid="settings-clients-airdcpp-queue-priority-select"' in enabled.text
        assert '<select x-model="form.airdcpp_queue_priority"' not in enabled.text
        picker_start = enabled.text.index('data-testid="settings-clients-picker-airdcpp"')
        picker_end = enabled.text.index("</button>", picker_start)
        assert ":disabled=" not in enabled.text[picker_start:picker_end]
        assert "Another Direct Connect client" in enabled.text[picker_start:picker_end]

    async def test_settings_client_bulk_tests_are_serialized_to_avoid_write_storm(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/settings?tab=clients")

        assert response.status_code == 200
        block = _script_block(
            response.text,
            "    async testClientConnection(clientId) {",
            "    saveConfig(formEl)",
        )
        assert "Promise.all(" not in block
        assert "for (const client of clients)" in block
        assert "await this.testClientConnection(client.id)" in block

    async def test_settings_metadata_is_minimal_and_shows_last_five_key_chars(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        async with sec_db() as session:
            session.add(
                SystemConfig(
                    key="comicvine_api_key",
                    value=encrypt_secret("1234567890abcde"),
                    value_type="secret",
                )
            )
            await session.commit()

        response = await authenticated_client.get("/settings?tab=metadata")

        assert response.status_code == 200
        assert "Get one at" in response.text
        assert "comicvine.gamespot.com/api" in response.text
        assert "Current stored key:" in response.text
        assert "••••••••••abcde" in response.text
        assert "pbFormatDurationMs(testResult.response_time_ms)" in response.text
        assert "Before you save" not in response.text
        assert "Store a tested key before saving it" not in response.text
        assert "Tune how often metadata is refreshed" not in response.text
        assert "Weekly refresh is a practical default" not in response.text
        assert "Lower values are safer" not in response.text

    async def test_settings_indexer_manager_api_keys_match_metadata_treatment(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        async with sec_db() as session:
            session.add_all(
                [
                    SystemConfig(key="prowlarr_url", value="http://localhost:9696"),
                    SystemConfig(
                        key="prowlarr_api_key",
                        value=encrypt_secret("1234567890abcde"),
                        value_type="secret",
                    ),
                    SystemConfig(key="jackett_url", value="http://localhost:9117"),
                    SystemConfig(
                        key="jackett_api_key",
                        value=encrypt_secret("abcdefghij12345"),
                        value_type="secret",
                    ),
                ]
            )
            await session.commit()

        response = await authenticated_client.get("/settings?tab=indexers")

        assert response.status_code == 200
        assert 'data-testid="settings-indexers-prowlarr-form"' in response.text
        assert '@submit.prevent="saveProwlarrAndSync()"' in response.text
        assert 'name="prowlarr_username"' in response.text
        assert 'autocomplete="username"' in response.text
        assert 'data-testid="settings-indexers-prowlarr-api-key"' in response.text
        assert 'autocomplete="new-password"' in response.text
        assert "(set — leave blank to keep)" in response.text
        assert "Current stored key:" in response.text
        assert "••••••••••abcde" in response.text
        assert "`${data.removed} retired`" in response.text
        assert "`${data.removed} removed`" not in response.text
        assert 'data-testid="settings-indexers-jackett-form"' in response.text
        assert '@submit.prevent="saveJackettAndSync()"' in response.text
        assert 'name="jackett_username"' in response.text
        assert 'data-testid="settings-indexers-jackett-api-key"' in response.text
        assert r"\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u202212345" in response.text
        assert (
            "Jackett owns tracker challenge resolution. Configure FlareSolverr in Jackett "
            "when a tracker requires it."
        ) not in response.text
        assert "pbFormatDurationMs(data.response_time_ms)" in response.text

    async def test_settings_indexers_show_jackett_source_and_retired_state(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        async with sec_db() as session:
            session.add(
                IndexerConfig(
                    name="1337x (Jackett)",
                    indexer_type=IndexerType.TORZNAB,
                    url="http://jackett:9117/api/v2.0/indexers/1337x/results/torznab",
                    api_key=encrypt_secret("jackett-key"),
                    source="jackett",
                    manager_indexer_id="1337x",
                    manager_available=False,
                )
            )
            await session.commit()

        response = await authenticated_client.get("/settings?tab=indexers")

        assert response.status_code == 200
        assert "1337x (Jackett)" in response.text
        assert '<span class="pill pill-purple">Jackett</span>' in response.text
        assert "Unavailable in Jackett" in response.text
        assert "Jackett owns tracker challenge resolution" in response.text

    async def test_settings_indexers_warn_when_managers_sync_the_same_tracker(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        async with sec_db() as session:
            session.add_all(
                [
                    IndexerConfig(
                        name="1337x (Prowlarr)",
                        indexer_type=IndexerType.TORZNAB,
                        url="http://prowlarr:9696/7",
                        api_key=encrypt_secret("prowlarr-key"),
                        source="prowlarr",
                        prowlarr_indexer_id=7,
                        manager_indexer_id="7",
                    ),
                    IndexerConfig(
                        name="1337x (Jackett)",
                        indexer_type=IndexerType.TORZNAB,
                        url="http://jackett:9117/api/v2.0/indexers/1337x/results/torznab",
                        api_key=encrypt_secret("jackett-key"),
                        source="jackett",
                        manager_indexer_id="1337x",
                    ),
                ]
            )
            await session.commit()

        response = await authenticated_client.get("/settings?tab=indexers")

        assert response.status_code == 200
        assert 'data-testid="settings-indexers-manager-duplicates"' in response.text
        assert "1337x is synchronized by both Prowlarr and Jackett" in response.text

    async def test_settings_indexers_source_priority_is_json_escaped(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        payload = """["usenet"]'; window.__pullboxXss = true; //"""
        async with sec_db() as session:
            session.add(SystemConfig(key="source_priority", value=payload, value_type="json"))
            await session.commit()

        response = await authenticated_client.get("/settings?tab=indexers")

        assert response.status_code == 200
        assert "const stored = '" not in response.text
        assert "\\u0027; window.__pullboxXss = true; //" in response.text

    async def test_settings_indexers_source_priority_includes_active_direct_downloads(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/settings?tab=indexers")

        assert response.status_code == 200
        assert "Direct Downloads" in response.text
        assert "dc: 'Direct Connect'" in response.text
        assert "Coming soon" not in response.text
        assert "item === 'direct'" in response.text
        assert "item === 'dc'" in response.text
        assert ">ADC</div>" in response.text
        assert "const supported = ['usenet', 'torrent', 'direct', 'dc'];" in response.text
        assert "JSON.stringify(this.order)" in response.text

    async def test_settings_indexer_bulk_tests_are_serialized_to_avoid_write_storm(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/settings?tab=indexers")

        assert response.status_code == 200
        block = _script_block(
            response.text,
            "    async testIndexerConnection(indexerId) {",
            "    // ── Prowlarr",
        )
        assert "Promise.all(" not in block
        assert "for (const idx of indexers)" in block
        assert "await this.testIndexerConnection(idx.id)" in block
        assert (
            ".then(indexers => this.runIndexerChecksSequentially("
            "indexers.filter(idx => idx.enabled)))"
        ) in block

    async def test_settings_indexers_do_not_run_connection_tests_on_page_load(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/settings?tab=indexers")

        assert response.status_code == 200
        assert "_silentTestAll" not in response.text

    async def test_settings_indexer_modal_formats_structured_api_errors(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/settings?tab=indexers")

        assert response.status_code == 200
        block = _script_block(
            response.text,
            "function indexersSettings() {",
            "</script>",
        )
        assert "formatApiError(payload, fallback)" in block
        assert "Array.isArray(detail)" in block
        assert "this.formatApiError(data, 'Failed to save.')" in block
        assert "this.formatApiError(data, 'Test request failed.')" in block
        assert "new Error(d?.detail || 'Failed to save.')" not in block

    async def test_settings_indexer_modal_scrolls_to_test_and_save_status(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/settings?tab=indexers")

        assert response.status_code == 200
        assert 'x-ref="indexerModalBody"' in response.text
        block = _script_block(
            response.text,
            "function indexersSettings() {",
            "</script>",
        )
        save_block = _script_block(block, "    saveIndexer() {", "    async deleteIndexer() {")
        test_block = _script_block(
            block,
            "    testModal() {",
            "    async testIndexerConnection(indexerId) {",
        )
        assert "scrollIndexerModalToStatus()" in block
        assert "this.scrollIndexerModalToStatus();" in save_block
        assert "this.scrollIndexerModalToStatus();" in test_block
        assert "top: modalBody.scrollHeight" in block
        assert ".catch(err => {" in test_block
        assert "this.testMessage = err.message ||" in test_block

    async def test_settings_media_naming_preview_escapes_template_values_and_results(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        payload = """{Series}'); window.__pullboxXss = true; //"""
        async with sec_db() as session:
            session.add(SystemConfig(key="comic_file_template", value=payload))
            await session.commit()

        response = await authenticated_client.get("/settings?tab=media")

        assert response.status_code == 200
        assert "previewNaming('{{ std_tmpl }}'" not in response.text
        assert "\\u0027); window.__pullboxXss = true; //" in response.text
        assert "escapeHtml(ex.input || '')" in response.text
        assert "escapeHtml(ex.output || '')" in response.text

    async def test_settings_media_import_save_uses_enabled_pointer_cursor(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/settings?tab=media")

        assert response.status_code == 200
        assert (
            ":class=\"isDirty ? 'cursor-pointer' : 'opacity-50 cursor-not-allowed'\""
        ) in response.text

    async def test_settings_media_naming_previews_use_stable_panel_contract(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/settings?tab=media")

        assert response.status_code == 200
        assert response.text.count("settings-media-preview-panel") == 5
        assert 'data-preview-ready="false"' in response.text
        assert 'aria-live="polite"' in response.text
        assert 'aria-busy="false"' in response.text
        assert "this.namingPreviewRequests" in response.text
        assert "lockNamingPreviewHeight(el)" in response.text
        assert "el.dataset.previewReady !==" in response.text
        assert "settings-media-preview-panel min-h-[2rem]" not in response.text

    async def test_settings_media_exposes_per_library_naming_scope_controls(
        self,
        authenticated_client,
        sec_db,
        tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        root_path = tmp_path / "root-policy-ui"
        root_path.mkdir()
        async with sec_db() as session:
            session.add(LibraryRoot(name="Primary Comics", path=str(root_path), enabled=True))
            await session.commit()

        response = await authenticated_client.get("/settings?tab=media")

        assert response.status_code == 200
        assert 'data-testid="settings-media-root-policy"' in response.text
        assert 'data-testid="settings-media-root-policy-scope"' in response.text
        assert "Primary Comics" in response.text
        assert "Use global defaults" in response.text
        assert "Save for this library" in response.text
        assert "/naming-policy/preview" in response.text
        assert 'method: "DELETE"' in response.text

    async def test_settings_media_exposes_safe_multi_library_root_management(
        self,
        authenticated_client,
        sec_db,
        tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        root_path = tmp_path / "managed-library"
        root_path.mkdir()
        async with sec_db() as session:
            session.add(LibraryRoot(name="Managed Library", path=str(root_path), enabled=True))
            await session.commit()

        response = await authenticated_client.get("/settings?tab=media")

        assert response.status_code == 200
        assert 'data-testid="settings-media-library-roots"' in response.text
        assert 'data-testid="settings-media-library-root-add"' in response.text
        assert 'data-testid="settings-media-library-root-name"' in response.text
        assert 'data-testid="settings-media-library-root-path"' in response.text
        assert 'data-testid="settings-media-library-root-reference-role"' in response.text
        assert 'data-testid="settings-media-library-root-managed-role"' in response.text
        assert 'data-testid="settings-media-library-root-default"' in response.text
        assert "Reference existing files" in response.text
        assert "Allow managed files" in response.text
        assert "Default managed destination" in response.text
        assert 'libraryRootRequest("/api/v1/config/library-roots/preview"' in response.text
        assert 'libraryRootRequest("/api/v1/config/library-roots"' in response.text
        assert 'method: "PATCH"' in response.text
        assert 'data-testid="settings-media-library-root-rebind"' in response.text
        assert "Preview rebind" in response.text
        assert "Confirm rebind" in response.text
        assert "/rebind/preview" in response.text
        assert 'confirmation: "REBIND"' in response.text
        assert "Delete library root" not in response.text
        assert "ordinary edits cannot change them" in response.text
        assert "bootstraps the default managed destination" in response.text
        assert (
            "All imports and download handoff work eventually target this library root"
            not in response.text
        )

    async def test_settings_renders_standardized_shell(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/settings")

        assert response.status_code == 200
        assert 'data-testid="settings-page"' in response.text
        assert 'data-admin-workspace-contract="v1"' in response.text
        assert 'hx-boost="false"' in response.text
        assert 'data-testid="settings-header"' in response.text
        assert 'data-admin-workspace-header="v1"' in response.text
        assert 'data-testid="settings-page-title"' in response.text
        assert 'data-testid="settings-page-subtitle"' in response.text
        assert 'class="series-registry-title"' in response.text
        assert ">SET<span>TINGS</span><" in response.text
        assert 'class="series-registry-subtitle"' in response.text
        assert 'data-testid="settings-body"' in response.text
        assert 'data-testid="settings-tabs"' in response.text
        assert 'data-admin-workspace-rail="v1"' in response.text
        assert 'data-testid="settings-content"' in response.text
        assert 'data-testid="page-footer-dock"' in response.text
        assert 'data-testid="settings-footer-dock"' in response.text
        assert 'data-testid="page-dock-inner"' in response.text
        assert 'data-testid="settings-tab-general"' in response.text
        assert 'data-testid="settings-tab-resolvers"' in response.text
        assert "Challenge Resolvers" in response.text
        assert 'data-testid="settings-panel-general"' in response.text

    async def test_general_settings_exposes_usage_stats_toggle(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/settings?tab=general")

        assert response.status_code == 200
        assert "Help improve Pullbox" in response.text
        assert "Share anonymous usage stats" in response.text
        assert 'data-testid="settings-general-usage-stats-toggle"' in response.text
        assert "install and version information" in response.text

    async def test_direct_download_settings_render_native_secret_free_provider_controls(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        async with sec_db() as session:
            session.add(
                DirectProviderConfig(
                    provider_id="community.example",
                    display_name="Example Direct Provider",
                    endpoint="http://direct-provider:8780",
                    enabled=True,
                    priority=25,
                    state=DirectProviderState.HEALTHY,
                    trust_level=DirectProviderTrustLevel.CUSTOM,
                    negotiated_protocol="direct-download-provider/v1",
                    encrypted_bearer_token="encrypted-token-must-not-render",
                    encrypted_configuration={
                        "account_token": "encrypted-account-token-must-not-render"
                    },
                    configuration_metadata={
                        "allow_private_http": True,
                        "automatic_quota_reserve": 5,
                        "quota_status": {
                            "remaining": 22,
                            "limit": 25,
                            "window_seconds": 64_800,
                            "reset_at": "2026-08-01T06:00:00+00:00",
                            "observed_at": "2026-07-31T12:00:00+00:00",
                        },
                        "public_values": {
                            "result_limit": 20,
                            "source_url": "https://annas-archive.gl",
                        },
                        "configured_secret_fields": ["account_token"],
                    },
                    manifest_snapshot={
                        "protocol_version": "direct-download-provider/v1",
                        "provider_id": "community.example",
                        "display_name": "Example Direct Provider",
                        "description": "A provider fixture.",
                        "provider_version": "1.2.3",
                        "supported_protocol_versions": ["direct-download-provider/v1"],
                        "publisher": "Example Publisher",
                        "license": "MIT",
                        "source_domains": ["example.test"],
                        "artifact_host_patterns": ["generic_https", "pixeldrain"],
                        "capabilities": {
                            "search": True,
                            "resolve": True,
                            "health": True,
                            "quota": True,
                            "configuration_schema": True,
                        },
                        "configuration_schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "result_limit": {
                                    "type": "integer",
                                    "title": "Result limit",
                                    "minimum": 1,
                                    "maximum": 100,
                                },
                                "account_token": {
                                    "type": "string",
                                    "title": "Account token",
                                    "x-pullbox-secret": True,
                                },
                                "source_url": {
                                    "type": "string",
                                    "title": "Official URL",
                                    "format": "uri",
                                    "enum": [
                                        "https://annas-archive.gl",
                                        "https://annas-archive.pk",
                                        "https://annas-archive.gd",
                                    ],
                                    "default": "https://annas-archive.gd",
                                },
                            },
                        },
                    },
                )
            )
            await session.commit()

        response = await authenticated_client.get("/settings?tab=direct")

        assert response.status_code == 200
        assert 'data-testid="settings-tab-direct"' in response.text
        assert 'data-testid="settings-panel-direct"' in response.text
        assert 'data-testid="settings-direct-add-provider"' in response.text
        assert 'data-testid="settings-direct-provider-1"' in response.text
        assert 'data-testid="settings-direct-resolver-summary"' not in response.text
        provider_card_position = response.text.index('data-testid="settings-direct-registry-card"')
        host_card_position = response.text.index('data-testid="settings-direct-hosts-card"')
        assert provider_card_position < host_card_position
        assert "Example Direct Provider" in response.text
        assert "http://direct-provider:8780" in response.text
        assert "Result limit" in response.text
        assert "Account token" in response.text
        assert "Official URL" in response.text
        assert "https://annas-archive.gl" in response.text
        assert "control.input_format === 'uri'" in response.text
        assert "control.choices.length || control.suggestions.length" in response.text
        assert "control.suggestions.length ? control.suggestions : control.choices" in response.text
        assert "toggleConfigurationChoices(control.name)" in response.text
        assert "selectConfigurationChoice(control.name, choice)" in response.text
        assert "settings-direct-provider-control-${control.name}-toggle" in response.text
        assert "settings-direct-provider-control-${control.name}-options" in response.text
        assert 'role="listbox"' in response.text
        assert "<datalist" not in response.text
        assert "Configured" in response.text
        assert "22 of 25 remaining" in response.text
        assert "Automatic reserve" in response.text
        assert "Automatic retry after" in response.text
        assert 'data-testid="settings-direct-provider-modal-quota-reserve"' in response.text
        assert 'x-model="form.automatic_quota_reserve"' in response.text
        assert "automatic_quota_reserve: Number(this.form.automatic_quota_reserve)" in response.text
        assert "identity does not verify the running image" in response.text
        assert '@click="openConfigure(1)"' in response.text
        assert '@keydown.enter.prevent="openConfigure(1)"' in response.text
        assert 'role="button"' in response.text
        assert 'tabindex="0"' in response.text
        assert 'data-testid="settings-direct-provider-modal-enabled"' in response.text
        assert 'data-testid="settings-direct-provider-modal-resolver-enabled"' in response.text
        assert 'data-testid="settings-direct-provider-modal-test"' in response.text
        assert 'data-testid="settings-direct-provider-modal-remove"' in response.text
        assert 'data-testid="settings-direct-provider-modal-actions"' in response.text
        assert 'x-ref="providerModalBody"' in response.text
        assert 'x-model="form.enabled"' in response.text
        assert 'class="flex items-center gap-2"' in response.text
        assert "scrollProviderModalToResults()" in response.text
        assert "modalBody.scrollTo({" in response.text
        assert "top: modalBody.scrollHeight" in response.text
        assert response.text.count("requestAnimationFrame(() => {") >= 2
        resolver_toggle_position = response.text.index(
            'data-testid="settings-direct-provider-modal-resolver-enabled"'
        )
        bearer_token_position = response.text.index("Replace bearer token")
        resolver_toggle_markup = response.text[
            resolver_toggle_position - 300 : resolver_toggle_position + 500
        ]
        assert resolver_toggle_position < bearer_token_position
        assert 'class="peer toggle-input"' in resolver_toggle_markup
        assert 'class="toggle-switch"' in resolver_toggle_markup
        assert 'data-testid="settings-direct-provider-test-1"' not in response.text
        assert 'data-testid="settings-direct-provider-configure-1"' not in response.text
        assert 'data-testid="settings-direct-provider-enable-1"' not in response.text
        assert 'data-testid="settings-direct-provider-disable-1"' not in response.text
        assert 'data-testid="settings-direct-provider-remove-1"' not in response.text
        assert ">Provider configuration</summary>" not in response.text
        assert "encrypted-token-must-not-render" not in response.text
        assert "encrypted-account-token-must-not-render" not in response.text

    async def test_direct_download_settings_hide_hosts_until_provider_is_registered(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/settings?tab=direct")

        assert response.status_code == 200
        assert 'data-testid="settings-direct-registry-card"' in response.text
        assert 'data-testid="settings-direct-empty-state"' in response.text
        assert 'data-testid="settings-direct-resolver-summary"' not in response.text
        assert 'data-testid="settings-direct-hosts-card"' not in response.text
        assert 'data-testid="settings-direct-host-modal"' not in response.text

    async def test_direct_download_settings_hide_hosts_for_generic_only_provider(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        async with sec_db() as session:
            session.add(
                DirectProviderConfig(
                    provider_id="pullbox.annas_archive",
                    display_name="Anna's Archive",
                    endpoint="http://annas-archive:8780",
                    enabled=True,
                    state=DirectProviderState.HEALTHY,
                    trust_level=DirectProviderTrustLevel.VERIFIED_PULLBOX,
                    negotiated_protocol="direct-download-provider/v1",
                    encrypted_bearer_token="encrypted-provider-token",
                    manifest_snapshot=_direct_manifest(
                        "pullbox.annas_archive",
                        artifact_hosts=["generic_https"],
                    ),
                )
            )
            await session.commit()

        response = await authenticated_client.get("/settings?tab=direct")

        assert response.status_code == 200
        assert 'data-testid="settings-direct-provider-1"' in response.text
        assert 'data-testid="settings-direct-hosts-card"' not in response.text
        assert 'data-testid="settings-direct-host-modal"' not in response.text

    async def test_direct_download_settings_show_declared_hosts_for_degraded_provider(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        async with sec_db() as session:
            session.add(
                DirectProviderConfig(
                    provider_id="pullbox.getcomics",
                    display_name="GetComics",
                    endpoint="http://getcomics:8780",
                    enabled=True,
                    state=DirectProviderState.DEGRADED,
                    trust_level=DirectProviderTrustLevel.VERIFIED_PULLBOX,
                    negotiated_protocol="direct-download-provider/v1",
                    encrypted_bearer_token="encrypted-provider-token",
                    manifest_snapshot=_direct_manifest(
                        "pullbox.getcomics",
                        artifact_hosts=["generic_https", "pixeldrain"],
                    ),
                )
            )
            await session.commit()

        response = await authenticated_client.get("/settings?tab=direct")

        assert response.status_code == 200
        assert 'data-testid="settings-direct-hosts-card"' in response.text
        assert 'data-testid="settings-direct-host-generic_https"' in response.text
        assert 'data-testid="settings-direct-host-pixeldrain"' in response.text
        assert 'data-testid="settings-direct-host-mega"' not in response.text
        assert 'data-testid="settings-direct-host-validation-generic_https"' in response.text
        assert ">Validated per download</span>" in response.text
        assert "x-show=\"activeHost.host_kind !== 'generic_https'\"" in response.text

    async def test_direct_download_settings_hide_hosts_for_disabled_named_host_provider(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        async with sec_db() as session:
            session.add(
                DirectProviderConfig(
                    provider_id="pullbox.getcomics",
                    display_name="GetComics",
                    endpoint="http://getcomics:8780",
                    enabled=False,
                    state=DirectProviderState.DISABLED,
                    trust_level=DirectProviderTrustLevel.VERIFIED_PULLBOX,
                    negotiated_protocol="direct-download-provider/v1",
                    encrypted_bearer_token="encrypted-provider-token",
                    manifest_snapshot=_direct_manifest(
                        "pullbox.getcomics",
                        artifact_hosts=["generic_https", "pixeldrain"],
                    ),
                )
            )
            await session.commit()

        response = await authenticated_client.get("/settings?tab=direct")

        assert response.status_code == 200
        assert 'data-testid="settings-direct-hosts-card"' not in response.text

    async def test_challenge_resolver_settings_render_ranked_browser_resolver_controls(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.models.direct_acquisition import (
            DirectResolverConfig,
            DirectResolverKind,
            DirectResolverState,
        )

        async with sec_db() as session:
            session.add_all(
                [
                    DirectResolverConfig(
                        name="TRAWL",
                        resolver_kind=DirectResolverKind.TRAWL,
                        priority=10,
                        endpoint="http://trawl:8151",
                        enabled=True,
                        state=DirectResolverState.HEALTHY,
                        allow_private_http=True,
                        timeout_seconds=60,
                        max_concurrency=1,
                        encrypted_auth_headers={
                            "Authorization": "encrypted-resolver-secret-must-not-render"
                        },
                        auth_metadata={"configured_header_names": ["Authorization"]},
                    ),
                    DirectResolverConfig(
                        name="FlareSolverr",
                        resolver_kind=DirectResolverKind.FLARESOLVERR,
                        priority=20,
                        endpoint="http://flaresolverr:8191",
                        enabled=False,
                        state=DirectResolverState.DISABLED,
                        allow_private_http=True,
                        timeout_seconds=60,
                        max_concurrency=1,
                    ),
                ]
            )
            await session.commit()

        response = await authenticated_client.get("/settings?tab=resolvers")

        assert response.status_code == 200
        assert 'data-testid="settings-tab-resolvers"' in response.text
        assert 'data-testid="settings-panel-resolvers"' in response.text
        assert 'data-testid="settings-resolvers-card"' in response.text
        assert "settings-resolver-${profile.id}" in response.text
        assert 'data-testid="settings-resolver-add"' in response.text
        assert '@click="openEdit(profile.id)"' in response.text
        assert '@keydown.enter.prevent="openEdit(profile.id)"' in response.text
        assert '@keydown.space.prevent="openEdit(profile.id)"' in response.text
        assert 'data-testid="settings-resolver-modal"' in response.text
        assert 'data-testid="settings-resolver-modal-backdrop"' in response.text
        assert 'data-testid="settings-resolver-modal-enabled"' in response.text
        assert 'data-testid="settings-resolver-modal-private-http"' in response.text
        assert 'data-testid="settings-resolver-type-select"' in response.text
        assert 'data-testid="settings-resolver-type-panel"' in response.text
        assert 'data-dropdown-select-contract="v1"' in response.text
        assert 'data-dropdown-select-mode="local"' in response.text
        assert 'local_model="form.resolver_kind"' not in response.text
        assert '<select id="resolver-profile-kind"' not in response.text
        assert 'data-testid="settings-resolver-modal-test"' in response.text
        assert 'data-testid="settings-resolver-modal-remove"' in response.text
        assert 'data-testid="settings-resolver-modal-actions"' in response.text
        assert "settings-resolver-test-${profile.id}" not in response.text
        assert '<template x-for="profile in profiles"' in response.text
        assert "Challenge resolvers" in response.text
        assert "Shared acquisition infrastructure" in response.text
        assert "Lower values are tried first" in response.text
        assert "does not guarantee CAPTCHA" in response.text
        assert "DataNodes account login requires TRAWL" in response.text
        assert "TRAWL native browser mode is used only for approved resolver flows" in response.text
        assert "credentials are never sent to TRAWL" in response.text
        assert "TRAWL MITM mode is not supported" in response.text
        assert "Prowlarr keeps its own resolver configuration" in response.text
        assert "Last connection test" in response.text
        assert "Last healthy response" in response.text
        assert "Last classified error" in response.text
        assert "Authorization" in response.text
        assert "encrypted-resolver-secret-must-not-render" not in response.text
        assert "const authenticationHeaders = this.headerUpdates();" in response.text
        assert (
            "if (Object.keys(authenticationHeaders).length) "
            "payload.authentication_headers = authenticationHeaders;"
        ) in response.text
        assert "form: {}," not in response.text
        assert "form: { auth_headers: [] }," in response.text

    async def test_search_and_indexer_settings_do_not_duplicate_resolver_management(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        direct = await authenticated_client.get("/settings?tab=direct")
        indexers = await authenticated_client.get("/settings?tab=indexers")
        search = await authenticated_client.get("/settings?tab=search")

        assert direct.status_code == 200
        assert indexers.status_code == 200
        assert search.status_code == 200
        assert 'data-testid="settings-direct-resolver-summary"' not in direct.text
        assert 'data-testid="settings-indexers-resolver-summary"' not in indexers.text
        assert 'data-testid="settings-resolvers-card"' not in indexers.text
        assert 'data-testid="settings-resolvers-card"' not in search.text
        assert indexers.text.count('href="/settings?tab=resolvers"') == 1
        assert 'href="/settings?tab=search#browser-resolvers"' not in indexers.text
        assert "Manage in Search" not in indexers.text
        assert 'data-testid="settings-indexers-manual-torznab-resolver"' in indexers.text
        assert "Ranked browser resolver chain" in indexers.text
        assert (
            "Only available for manually added torznab providers. Pullbox tries ordinary HTTP "
            "first and never sends the API key or search query to a resolver."
        ) in indexers.text
        assert 'class="peer toggle-input"' in indexers.text
        assert ':disabled="!resolverChainAvailable"' in indexers.text
        assert "resolverChainAvailable: false" in indexers.text
        assert "Try the ranked browser resolver chain" not in indexers.text
        assert "Manual Torznab only" not in indexers.text
        assert 'data-testid="settings-resolver-endpoint"' not in direct.text
        assert 'data-testid="settings-resolver-endpoint"' not in indexers.text
        assert 'data-testid="settings-resolver-endpoint"' not in search.text

    async def test_direct_download_settings_render_native_host_registry_without_secrets(
        self,
        authenticated_client,
        sec_db,
    ) -> None:  # type: ignore[no-untyped-def]
        async with sec_db() as session:
            session.add_all(
                [
                    DirectProviderConfig(
                        provider_id="community.host-test",
                        display_name="Host Test Provider",
                        endpoint="http://host-test-provider:8780",
                        enabled=True,
                        state=DirectProviderState.HEALTHY,
                        trust_level=DirectProviderTrustLevel.CUSTOM,
                        negotiated_protocol="direct-download-provider/v1",
                        encrypted_bearer_token="encrypted-provider-token",
                        manifest_snapshot=_direct_manifest(
                            "community.host-test",
                            artifact_hosts=[
                                "generic_https",
                                "pixeldrain",
                                "mega",
                                "rootz",
                                "mediafire",
                                "terabox",
                                "datanodes",
                            ],
                        ),
                    ),
                    DirectHostConfig(
                        host_kind=DirectArtifactHostKind.PIXELDRAIN,
                        enabled=True,
                        preference=10,
                        account_state=DirectHostAccountState.HEALTHY,
                        encrypted_credentials={
                            "api_key": encrypt_secret("pixeldrain-secret-must-not-render")
                        },
                        account_metadata={"configured_credential_fields": ["api_key"]},
                    ),
                ]
            )
            await session.commit()

        response = await authenticated_client.get("/settings?tab=direct")

        assert response.status_code == 200
        assert 'data-testid="settings-direct-hosts-card"' in response.text
        assert 'data-testid="settings-direct-host-pixeldrain"' in response.text
        assert "@click=\"openHostConfigure('pixeldrain')\"" in response.text
        assert "@keydown.enter.prevent=\"openHostConfigure('pixeldrain')\"" in response.text
        assert "@keydown.space.prevent=\"openHostConfigure('pixeldrain')\"" in response.text
        assert 'data-testid="settings-direct-host-configure-pixeldrain"' not in response.text
        assert 'data-testid="settings-direct-host-toggle-pixeldrain"' not in response.text
        assert 'data-testid="settings-direct-host-modal-enabled"' in response.text
        assert 'data-testid="settings-direct-host-modal-actions"' in response.text
        assert 'data-testid="settings-direct-host-modal-test"' in response.text
        assert "Last reachable" in response.text
        assert "Last operational use" in response.text
        assert "Not Checked" in response.text
        assert "Last account check" not in response.text
        assert "testHost()" in response.text
        assert "const shouldRefreshHostStatus = Boolean(this.hostTestState);" in response.text
        assert "if (shouldRefreshHostStatus) this.refresh();" in response.text
        assert "refresh() {" in response.text
        assert "window.location.reload();" in response.text
        assert 'x-model="hostForm.enabled"' in response.text
        assert "hostForm.clearCredentials" not in response.text
        assert "clearCredentials:" not in response.text
        assert "Clear the saved" not in response.text
        assert "setHostEnabled(" not in response.text
        host_card_position = response.text.index('data-testid="settings-direct-host-pixeldrain"')
        host_card_markup = response.text[host_card_position - 500 : host_card_position + 300]
        assert 'role="button"' in host_card_markup
        assert 'tabindex="0"' in host_card_markup
        host_toggle_position = response.text.index(
            'data-testid="settings-direct-host-modal-enabled"'
        )
        host_toggle_markup = response.text[host_toggle_position - 300 : host_toggle_position + 500]
        assert 'class="peer toggle-input"' in host_toggle_markup
        assert 'class="toggle-switch"' in host_toggle_markup
        host_actions_position = response.text.index(
            'data-testid="settings-direct-host-modal-actions"'
        )
        host_actions_markup = response.text[
            host_actions_position - 100 : host_actions_position + 500
        ]
        assert 'class="flex items-center gap-2"' in host_actions_markup
        generic_host_position = response.text.index(
            'data-testid="settings-direct-host-generic_https"'
        )
        pixeldrain_host_position = response.text.index(
            'data-testid="settings-direct-host-pixeldrain"'
        )
        generic_host_end = response.text.index("</article>", generic_host_position)
        generic_host_markup = response.text[generic_host_position:generic_host_end]
        assert "None required" in generic_host_markup
        assert "Not Configured" not in generic_host_markup
        assert pixeldrain_host_position < generic_host_position
        tied_default_host_positions = [
            response.text.index(f'data-testid="settings-direct-host-{host_kind}"')
            for host_kind in (
                "datanodes",
                "generic_https",
                "mediafire",
                "mega",
                "rootz",
                "terabox",
            )
        ]
        assert tied_default_host_positions == sorted(tied_default_host_positions)
        assert 'x-if="activeHost.allowed_credential_fields.length > 0"' in response.text
        assert "Artifact hosts" in response.text
        assert "PixelDrain" in response.text
        assert "MEGA" in response.text
        assert "TeraBox" in response.text
        assert "API key" in response.text
        assert (
            "Public links work anonymously. Account-backed access uses a revocable session."
            in response.text
        )
        assert "developer application key" not in response.text
        assert (
            "Requires a free registered or Premium account plus a healthy TRAWL resolver. "
            "Anonymous downloads are not supported." in response.text
        )
        assert "Username" in response.text
        assert "Password" in response.text
        assert "Account required" in response.text
        assert "pixeldrain-secret-must-not-render" not in response.text

    async def test_general_settings_renders_https_controls(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/settings?tab=general")

        assert response.status_code == 200
        assert 'data-testid="settings-general-https-card"' in response.text
        assert "Native HTTPS" in response.text
        assert "Enable HTTPS" in response.text
        assert 'data-testid="settings-general-https-enabled"' in response.text
        assert 'data-testid="settings-general-https-cert-path"' in response.text
        assert 'data-testid="settings-general-https-key-path"' in response.text
        assert 'data-testid="settings-general-https-cert-browse"' in response.text
        assert 'data-testid="settings-general-https-key-browse"' in response.text
        assert "PULLBOX_HTTPS_CERT_ROOT" in response.text
        assert "httpsRestartRequired" in response.text
        assert "Restart Pullbox" in response.text
        assert "pem,crt,cer,key" in response.text
        assert "browseHttpsCertificate" in response.text
        assert "browseHttpsPrivateKey" in response.text

    async def test_settings_htmx_tab_returns_content_bundle(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/htmx/settings/utilities",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'id="page-footer-dock"' in response.text
        assert 'hx-swap-oob="innerHTML"' in response.text
        assert 'data-testid="settings-footer-dock"' in response.text
        assert 'data-testid="page-dock-inner"' in response.text
        assert 'data-testid="settings-content"' in response.text
        assert 'data-testid="settings-panel-utilities"' in response.text
        assert 'data-testid="settings-body"' not in response.text
        assert 'data-testid="settings-tabs"' not in response.text
        assert 'data-testid="settings-page"' not in response.text

    async def test_settings_utilities_browse_buttons_use_resolved_start_paths(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/settings?tab=utilities")

        settings = get_settings()
        expected_trash = str(
            resolve_utility_directory(
                db_value="",
                default_parent=settings.library_root,
                default_subdir=".trash",
                library_root=settings.library_root,
                data_dir=settings.data_dir,
            )
        )
        expected_export = str(
            resolve_utility_directory(
                db_value="",
                default_parent=settings.data_dir,
                default_subdir="exports",
                library_root=settings.library_root,
                data_dir=settings.data_dir,
            )
        )

        assert response.status_code == 200
        assert 'data-testid="settings-utilities-trash-folder-browse"' in response.text
        assert 'data-testid="settings-utilities-trash-retention-days"' in response.text
        assert 'data-testid="settings-utilities-empty-trash-now"' in response.text
        assert 'data-testid="settings-utilities-export-folder-browse"' in response.text
        assert f"trashFolder: {json.dumps(expected_trash)}" in response.text
        assert f"exportFolder: {json.dumps(expected_export)}" in response.text
        assert "trashRetentionDays: '30'" in response.text
        assert 'class="btn-ghost btn-sm !min-h-10 !w-10 !px-0 !py-0 shrink-0"' in response.text
        assert 'class="btn-danger btn-sm self-start sm:self-auto"' in response.text

    @pytest.mark.parametrize(
        ("tab", "expected_ids", "absent_snippets"),
        [
            (
                "general",
                ["settings-general-log-level-select"],
                ['<select x-model="logging.logLevel"'],
            ),
            (
                "utilities",
                ["settings-utilities-log-level-select"],
                ['<select x-model="form.logLevel"'],
            ),
            (
                "clients",
                [
                    "settings-clients-sab-priority-select",
                    "settings-clients-sab-post-processing-select",
                    "settings-clients-qbt-content-layout-select",
                    "settings-clients-nzbget-priority-select",
                    "settings-clients-nzbget-post-processing-select",
                    "settings-clients-transmission-bandwidth-priority-select",
                ],
                [
                    '<select x-model="form.sab_priority"',
                    '<select x-model="form.sab_post_processing"',
                    '<select x-model="form.qbt_content_layout"',
                    '<select x-model="form.nzbget_priority"',
                    '<select x-model="form.nzbget_post_processing"',
                    '<select x-model="form.transmission_bandwidth_priority"',
                ],
            ),
            (
                "ui",
                ["settings-ui-timezone-select", "settings-ui-date-format-select"],
                ['<select x-model="timezone"', '<select x-model="dateFormat"'],
            ),
            (
                "media",
                [
                    "settings-media-preferred-format-select",
                    "settings-media-colon-replacement-select",
                    "settings-media-post-processing-select",
                    "settings-media-torrent-import-strategy-select",
                ],
                [
                    '<select name="preferred_format"',
                    '<select name="colon_replacement"',
                    '<select name="post_processing_method"',
                    '<select name="torrent_import_strategy"',
                ],
            ),
            (
                "search",
                [
                    "settings-search-preferred-language-select",
                    "settings-search-threshold-issue-select",
                    "settings-search-threshold-tpb-select",
                    "settings-search-threshold-compendium-select",
                ],
                [
                    '<select name="preferred_language"',
                    '<select x-model="thresholds.issue"',
                    '<select x-model="thresholds.tpb"',
                ],
            ),
        ],
    )
    async def test_settings_tabs_use_shared_dropdown_contract(
        self,
        authenticated_client,
        tab: str,
        expected_ids: list[str],
        absent_snippets: list[str],
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(f"/settings?tab={tab}")

        assert response.status_code == 200
        for expected_id in expected_ids:
            assert f'data-testid="{expected_id}"' in response.text
        assert 'data-dropdown-select-contract="v1"' in response.text
        for snippet in absent_snippets:
            assert snippet not in response.text
        if tab == "general":
            assert (
                "Control the network-facing identity and address Pullbox presents"
                not in response.text
            )
            assert "Tune default verbosity and storage" not in response.text
            assert "Decide where backups live and how frequently" not in response.text
            assert 'class="settings-rows settings-rows-align-end"' in response.text
            assert 'class="settings-footer-actions ml-auto justify-end"' in response.text
        if tab == "ui":
            assert (
                "Choose how timestamps appear across tables, cards, and detail views."
                not in response.text
            )
            assert (
                "Choose whether Pullbox follows the system theme or stays pinned to one appearance."
                not in response.text
            )
            assert (
                "All timestamps are stored in UTC and converted only for display."
                not in response.text
            )
            assert "Controls how dates appear in tables and cards." not in response.text
            assert "Choose a 24-hour clock or a 12-hour display with AM/PM." not in response.text
            assert "Include seconds in displayed timestamps." not in response.text
            assert (
                "Append the timezone abbreviation after times when available." not in response.text
            )
            assert "Keep the AM/PM marker visible in 12-hour mode." not in response.text
            assert (
                "This live preview reflects the current combination of timezone, "
                "date, and clock settings before you save." not in response.text
            )
            assert (
                "Pullbox follows your operating system setting and opens in the matching theme."
                not in response.text
            )
            assert 'class="selection-card settings-theme-choice h-full w-full"' in response.text
            assert '@click="reset()"' in response.text
            assert "Save display settings" in response.text
        if tab == "media":
            assert 'data-testid="settings-media-preferred-format-select"' in response.text
            assert 'data-testid="settings-media-preferred-format-card"' not in response.text
            assert "section-card-visible" in response.text
            assert "Normalize Imported Archives to CBZ" in response.text
            assert "Search on Add" in response.text
            assert 'name="search_on_add_default"' in response.text
            assert "convertToPreferredFormat ? 'Enabled' : 'Disabled'" in response.text
            assert response.text.count('class="settings-footer-actions ml-auto justify-end"') >= 3
            assert "peer sr-only" not in response.text
            assert "peer toggle-input" in response.text
            assert "{Title}" in response.text
            assert "{Volume:02d}" in response.text
            assert "{Edition}" not in response.text
            assert 'name="non_standard_file_template"' in response.text
            assert 'name="single_non_standard_file_template"' in response.text
            assert "Collection Non-Standard Format" in response.text
            assert "Single-Release Non-Standard Format" in response.text
        if tab == "search":
            assert "Search on Add" not in response.text
            assert 'name="search_on_add_default"' not in response.text
        if tab == "clients":
            assert 'data-testid="settings-clients-registry-card"' in response.text
            assert 'data-testid="settings-clients-registry-actions"' in response.text
            assert 'data-testid="settings-clients-test-all"' in response.text
            assert 'data-testid="settings-clients-add-client"' in response.text
            assert 'data-testid="settings-clients-modal-form"' in response.text
            assert 'data-testid="settings-clients-modal"' in response.text
            assert 'data-testid="settings-clients-modal-backdrop"' in response.text
            assert '@submit.prevent="saveClient()"' in response.text
            assert 'name="client_password_username"' in response.text
            assert 'placeholder="My Client Name"' in response.text
            assert 'autocomplete="nickname"' in response.text
            assert 'placeholder="http://localhost:8080"' in response.text
            assert 'autocomplete="url"' in response.text
            assert 'data-testid="settings-clients-registry-list"' in response.text
            assert 'data-testid="settings-clients-import-card"' in response.text
            assert 'data-testid="settings-clients-failure-card"' in response.text
            assert 'data-testid="settings-clients-history-card"' in response.text
            assert (
                "Open this connection to update credentials, path mapping, or routing details."
                not in response.text
            )
            assert "Registry notes" not in response.text
            assert "Path mapping help" not in response.text
            assert "Add another client" not in response.text
            assert "xl:grid-cols-[minmax(0,1.55fr)_minmax(280px,0.7fr)]" not in response.text
            assert 'class="grid gap-6 xl:grid-cols-2"' not in response.text
            assert "pill-danger" not in response.text
        if tab == "indexers":
            assert 'data-testid="settings-indexers-prowlarr-card"' in response.text
            assert 'data-testid="settings-indexers-prowlarr-sync"' in response.text
            assert 'data-testid="settings-indexers-prowlarr-url"' in response.text
            assert 'data-testid="settings-indexers-prowlarr-api-key"' in response.text
            assert 'data-testid="settings-indexers-prowlarr-test"' in response.text
            assert 'data-testid="settings-indexers-prowlarr-save-sync"' in response.text
            assert 'data-testid="settings-indexers-jackett-card"' in response.text
            assert 'data-testid="settings-indexers-jackett-sync"' in response.text
            assert 'data-testid="settings-indexers-jackett-url"' in response.text
            assert 'data-testid="settings-indexers-jackett-api-key"' in response.text
            assert 'data-testid="settings-indexers-jackett-test"' in response.text
            assert 'data-testid="settings-indexers-jackett-save-sync"' in response.text
            assert 'data-testid="settings-indexers-registry-card"' in response.text
            assert 'data-testid="settings-indexers-test-all"' in response.text
            assert 'data-testid="settings-indexers-add-indexer"' in response.text
            assert 'data-testid="settings-indexers-modal"' in response.text
            assert 'data-testid="settings-indexers-modal-backdrop"' in response.text
            assert 'data-testid="settings-indexers-registry-list"' in response.text
            assert 'data-testid="settings-indexers-priority-card"' in response.text
            assert 'data-testid="settings-indexers-failure-card"' in response.text
            assert 'data-testid="settings-indexers-blocklist-card"' in response.text
            assert "Registry rules" not in response.text
            assert "Registry snapshot" not in response.text
            assert "Add another source" not in response.text
            assert "Before you raise the threshold" not in response.text
            assert "When this helps most" not in response.text
            assert "xl:grid-cols-[minmax(0,1.55fr)_minmax(280px,0.7fr)]" not in response.text
            assert "xl:grid-cols-[minmax(0,1.3fr)_minmax(280px,0.85fr)]" not in response.text
            assert "Save & Sync" in response.text
            assert "Save priority" in response.text
            assert "Save Changes" in response.text
            assert (
                ":placeholder=\"form.indexer_type === 'torznab' ? 'Torznab' : 'NZBgeek'\""
                in response.text
            )
            assert (
                ":placeholder=\"form.indexer_type === 'torznab' ? "
                "'https://api.torznab.com' : 'https://api.nzbgeek.info'\"" in response.text
            )
            assert 'autocomplete="nickname"' in response.text
            assert 'autocomplete="url"' in response.text
            assert response.text.count('@click="reset()"') >= 2
            assert "Mark an indexer unhealthy after this many failed checks." in response.text
            assert (
                "Remove blocklist entries after this many days. Use 0 to keep them."
                in response.text
            )
        if tab == "search":
            assert (
                "Decide how often Pullbox searches for wanted issues and whether new "
                "series should search immediately." not in response.text
            )
            assert (
                "Filter out releases that are too small, too large, or too weakly "
                "scored before ranking gets more detailed." not in response.text
            )
            assert (
                "Set the relative importance of quality factors and the extra bonuses "
                "or penalties that push a release up or down." not in response.text
            )
            assert "Automatic wanted-issue search cadence." in response.text
            assert "Typical good releases score around 70." in response.text
            assert "0% = quality only, 100% = confidence only." in response.text
            assert "Generic fallback routes candidates to intervention." in response.text
            assert (
                "Comma-separated and case-insensitive. Any match rejects the release."
                in response.text
            )
            assert "info-panel" not in response.text
