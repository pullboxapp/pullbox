"""Route-contract tests for the rewritten system shell."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

pytest_plugins = ["conftest_security"]

os.environ.setdefault("PULLBOX_SECRET_KEY", "test-secret-key-for-system-ui")


@pytest.mark.asyncio
class TestSystemRouteContracts:
    """Verify the system area renders a stable mounted shell."""

    async def test_system_renders_standardized_shell(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/system")

        assert response.status_code == 200
        assert 'data-testid="system-page"' in response.text
        assert 'data-admin-workspace-contract="v1"' in response.text
        assert 'data-testid="system-header"' in response.text
        assert 'data-admin-workspace-header="v1"' in response.text
        assert 'data-testid="system-page-title"' in response.text
        assert ">SYS<span>TEM</span><" in response.text
        assert 'data-testid="system-page-subtitle"' in response.text
        assert 'class="series-registry-title"' in response.text
        assert 'class="series-registry-subtitle"' in response.text
        assert 'data-testid="system-body"' in response.text
        assert 'data-testid="system-tabs"' in response.text
        assert 'data-admin-workspace-rail="v1"' in response.text
        assert 'data-testid="system-content"' in response.text
        assert 'data-testid="page-footer-dock"' in response.text
        assert 'data-testid="system-footer-dock"' in response.text
        assert 'data-testid="system-tab-about"' in response.text
        assert 'data-testid="system-panel-about"' in response.text
        assert 'data-testid="system-about-version-banner"' in response.text
        assert 'class="system-version-banner"' in response.text
        assert "Released <span x-text=\"info.release_date || '—'\"></span>" in response.text
        assert "Branch <span x-text=\"info.branch || '—'\"></span>" in response.text
        assert "Commit <span x-text=\"info.commit || '—'\"></span>" in response.text
        assert "return 'not checked yet';" in response.text
        assert "Hostname" in response.text
        assert "Runtime mode" not in response.text
        assert ">Port<" not in response.text
        assert ">Application<" in response.text
        assert ">Configuration<" in response.text
        assert ">Database<" in response.text
        assert ">Logs<" in response.text
        assert ">Library root<" in response.text
        assert ">Backups<" in response.text
        assert "Database size" not in response.text
        assert 'class="pill pill-purple"' in response.text
        assert "aboutManager(" in response.text
        assert "https://pullbox.app/docs" in response.text
        assert "https://pullbox.app/docs/reference/troubleshooting" in response.text
        assert ':href="info.docs_url"' in response.text
        assert ">Docs<" in response.text
        assert "https://discord.gg/mg6GQkATaA" in response.text
        assert "https://bsky.app/profile/pullboxapp.bsky.social" in response.text
        assert "https://x.com/PullboxApp" in response.text
        assert "https://mastodon.social/@PullboxApp" in response.text
        assert "https://www.reddit.com/r/Pullbox/" in response.text
        assert ">Discord<" in response.text
        assert ">Bluesky<" in response.text
        assert ">Mastodon<" in response.text
        assert ">Reddit<" in response.text

    async def test_system_support_links_to_docs(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/htmx/system/support")

        assert response.status_code == 200
        assert ">Documentation<" in response.text
        assert "https://pullbox.app/docs/reference/troubleshooting" in response.text
        assert "https://pullbox.app/docs" in response.text
        assert ">Troubleshooting guide<" in response.text
        assert ">Browse all docs<" in response.text

    async def test_system_htmx_tab_returns_body_bundle(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get(
            "/htmx/system/tasks",
            headers={"HX-Request": "true"},
        )

        assert response.status_code == 200
        assert 'id="page-footer-dock"' in response.text
        assert 'hx-swap-oob="innerHTML"' in response.text
        assert 'data-testid="system-footer-dock"' in response.text
        assert 'data-testid="system-content"' in response.text
        assert 'data-testid="system-panel-tasks"' in response.text
        assert 'data-testid="system-body"' not in response.text
        assert 'data-testid="system-tabs"' not in response.text
        assert 'data-testid="system-page"' not in response.text

    async def test_system_logs_tab_uses_shared_local_search_field(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/system?tab=logs")

        assert response.status_code == 200
        assert 'data-testid="system-panel-logs"' in response.text
        assert 'data-log-viewer-contract="v1"' in response.text
        assert 'data-testid="system-log-viewer"' in response.text
        assert 'data-testid="system-log-search-field"' in response.text
        assert 'data-search-field-contract="baseline-v2"' in response.text
        assert 'data-search-field-mode="local"' in response.text
        assert 'data-testid="system-log-search-input"' in response.text
        assert 'data-testid="system-log-search-history-panel"' in response.text
        assert 'data-search-history-key="pullbox.searchHistory.systemLogs"' in response.text
        assert 'x-model="searchQuery"' in response.text
        assert 'data-testid="system-log-lines-select"' in response.text
        assert 'data-dropdown-select-contract="v1"' in response.text
        assert 'data-dropdown-select-mode="local"' in response.text
        assert 'data-testid="system-log-download"' in response.text
        assert 'data-testid="system-log-refresh"' in response.text
        assert 'data-tip="Download"' in response.text
        assert 'hx-boost="false"' in response.text
        assert 'data-tip="Refresh"' in response.text
        assert 'data-tip="Close"' in response.text
        assert 'data-testid="system-log-auto-scroll"' not in response.text
        assert 'class="btn-ghost btn-sm !min-h-8 !w-8 !px-0 !py-0"' in response.text
        assert (
            'class="w-full min-w-0 max-w-full rounded-xl border border-pb-border overflow-hidden'
            in response.text
        )
        assert (
            'class="log-terminal w-full min-w-0 max-w-full overflow-y-auto overflow-x-auto'
            in response.text
        )
        assert 'class="log-detail min-w-0 max-w-full overflow-x-auto' in response.text

    async def test_system_logs_tab_accepts_server_rendered_log_rows(
        self,
        authenticated_client,
        monkeypatch,
    ) -> None:  # type: ignore[no-untyped-def]
        from pullbox.api.v1 import system as system_api

        async def fake_list_log_files(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            return [
                system_api.LogFileResponse(
                    filename="pullbox.log",
                    size_bytes=2048,
                    modified_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC).isoformat(),
                )
            ]

        monkeypatch.setattr(system_api, "list_log_files", fake_list_log_files)

        response = await authenticated_client.get("/system?tab=logs")

        assert response.status_code == 200
        assert "pullbox.log" in response.text
        assert 'logFilesManager([{"filename": "pullbox.log"' in response.text

    async def test_system_support_tab_uses_shared_local_dropdown(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        response = await authenticated_client.get("/system?tab=support")

        assert response.status_code == 200
        assert 'data-testid="system-support-debug-duration-select"' in response.text
        assert 'data-testid="system-support-debug-card"' in response.text
        assert 'data-dropdown-select-contract="v1"' in response.text
        assert 'data-dropdown-select-mode="local"' in response.text
        assert "debugDuration: '15'" in response.text
        assert 'value="15"' in response.text
        assert '<select x-model="debugDuration"' not in response.text
        assert 'class="section-card overflow-visible"' in response.text
        assert 'class="step-badge shrink-0">1</span>' in response.text
        assert 'class="step-badge shrink-0">2</span>' in response.text
        assert 'class="step-badge shrink-0">3</span>' in response.text
        assert 'class="step-badge shrink-0">4</span>' in response.text
        assert "btn-primary btn-md gap-2 shrink-0" in response.text
        assert (
            'class="list-disc space-y-2 pl-5 text-sm leading-6 text-pb-text-sec"' in response.text
        )
        assert "Last 5 days of log files" in response.text
        assert "Sanitized database copy" in response.text
        assert "Download and search history" in response.text
        assert "Scheduler and disk status" in response.text
        assert "Health check results" in response.text
        assert "Import job history" in response.text
        assert "https://discord.gg/mg6GQkATaA" in response.text
        assert 'class="flex flex-col gap-3 border-t border-pb-border pt-4' in response.text
        assert "Privacy safe - keys, passwords &amp; tokens stripped" in response.text

    async def test_system_registry_tabs_use_shared_table_contracts(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        tasks = await authenticated_client.get("/system?tab=tasks")
        backups = await authenticated_client.get("/system?tab=backup")
        logs = await authenticated_client.get("/system?tab=logs")

        assert tasks.status_code == 200
        assert backups.status_code == 200
        assert logs.status_code == 200

        assert 'data-testid="system-tasks-table"' in tasks.text
        assert 'class="downloads-table-wrap"' in tasks.text
        assert 'class="downloads-table min-w-[860px]"' in tasks.text
        assert 'class="downloads-action-group is-hover-reveal justify-end"' in tasks.text
        assert 'data-tip="Run now"' in tasks.text
        assert "<th>Last execution</th>" in tasks.text
        assert "<th>Duration</th>" in tasks.text
        assert "<th>Next execution</th>" in tasks.text

        assert 'data-testid="system-backups-table"' in backups.text
        assert 'class="downloads-table-wrap"' in backups.text
        assert 'class="downloads-table min-w-[880px]"' in backups.text
        assert 'class="downloads-action-group is-hover-reveal justify-end"' in backups.text
        assert 'data-tip="Download"' in backups.text
        assert 'data-tip="Restore"' in backups.text
        assert 'data-tip="Delete"' in backups.text
        assert "database restore points" in backups.text
        assert "They do not include comics or downloaded media files." in backups.text

        assert 'data-testid="system-logs-table"' in logs.text
        assert 'class="downloads-table-wrap"' in logs.text
        assert 'class="downloads-table min-w-[720px]"' in logs.text
        assert 'class="downloads-action-group is-hover-reveal justify-end"' in logs.text

    async def test_system_pills_use_shared_semantic_contracts(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        about = await authenticated_client.get("/system?tab=about")
        backup = await authenticated_client.get("/system?tab=backup")
        tasks = await authenticated_client.get("/system?tab=tasks")

        assert about.status_code == 200
        assert backup.status_code == 200
        assert tasks.status_code == 200

        assert "statusPillClass()" in about.text
        assert "if (!this.checked) return 'pill-neutral';" in about.text
        assert "return this.updateAvailable ? 'pill-warning' : 'pill-success';" in about.text
        assert "backupTypePillClass(b.backup_type)" in backup.text
        assert "if (type === 'manual') return 'pill-info';" in backup.text
        assert "if (type === 'scheduled') return 'pill-success';" in backup.text
        assert "return 'pill-neutral';" in backup.text
        assert "if (state === 'running') return 'pill-info';" in tasks.text
        assert "if (state === 'failed') return 'pill-error';" in tasks.text
        assert "if (state === 'idle') return 'pill-neutral';" in tasks.text
        assert "return 'pill-success';" in tasks.text

    async def test_system_actions_use_compact_button_contracts(
        self,
        authenticated_client,
    ) -> None:  # type: ignore[no-untyped-def]
        backup = await authenticated_client.get("/system?tab=backup")
        tasks = await authenticated_client.get("/system?tab=tasks")
        logs = await authenticated_client.get("/system?tab=logs")
        support = await authenticated_client.get("/system?tab=support")

        assert backup.status_code == 200
        assert tasks.status_code == 200
        assert logs.status_code == 200
        assert support.status_code == 200

        assert 'class="btn-primary btn-sm gap-2"' in backup.text
        assert 'class="btn-primary btn-sm gap-2"' in tasks.text
        assert 'class="btn-primary btn-sm gap-2"' in logs.text
        assert 'class="btn-danger btn-sm gap-2"' in logs.text
        assert "btn-primary btn-md gap-2 shrink-0" in support.text
        assert 'class="btn-primary btn-sm gap-2"' in support.text
