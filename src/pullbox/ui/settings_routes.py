"""Settings UI routes and tab loading."""

import json
import os
from collections.abc import Callable, Mapping, Sequence

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from starlette.responses import Response

from pullbox.api.deps import AuthenticatedUser, DbSession
from pullbox.config import get_settings
from pullbox.core.naming import (
    resolve_collection_non_standard_file_template,
    resolve_single_non_standard_file_template,
)
from pullbox.models.client import DownloadClientConfig
from pullbox.models.config import SystemConfig
from pullbox.models.indexer import IndexerConfig

page_router = APIRouter()
htmx_router = APIRouter()

_CLIENT_STATUS_CACHE_COOKIE = "pb_client_status_cache"
_INDEXER_STATUS_CACHE_COOKIE = "pb_indexer_status_cache"

SETTINGS_TABS: tuple[dict[str, str], ...] = (
    {
        "key": "general",
        "label": "General",
        "icon": "M10.5 6h9.75M10.5 6a1.5 1.5 0 11-3 0m3 0a1.5 1.5 0 10-3 0M3.75 6H7.5m3 12h9.75m-9.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-3.75 0H7.5m9-6h3.75m-3.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-9.75 0h9.75",  # noqa: E501
    },
    {
        "key": "media",
        "label": "Media Management",
        "icon": "M3.75 9.776c.112-.017.227-.026.344-.026h15.812c.117 0 .232.009.344.026m-16.5 0a2.25 2.25 0 00-1.883 2.542l.857 6a2.25 2.25 0 002.227 1.932H19.05a2.25 2.25 0 002.227-1.932l.857-6a2.25 2.25 0 00-1.883-2.542m-16.5 0V6A2.25 2.25 0 016 3.75h3.879a1.5 1.5 0 011.06.44l2.122 2.12a1.5 1.5 0 001.06.44H18A2.25 2.25 0 0120.25 9v.776",  # noqa: E501
    },
    {
        "key": "clients",
        "label": "Download Clients",
        "icon": "M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3",  # noqa: E501
    },
    {
        "key": "indexers",
        "label": "Indexers",
        "icon": "M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z",
    },
    {
        "key": "resolvers",
        "label": "Challenge Resolvers",
        "icon": "M9 12.75 11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.249-8.25-3.285z",  # noqa: E501
    },
    {
        "key": "direct",
        "label": "Direct Downloads",
        "icon": "M12 3v12m0 0 4.5-4.5M12 15l-4.5-4.5M4.5 18.75h15",
    },
    {
        "key": "metadata",
        "label": "Metadata",
        "icon": "M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z",  # noqa: E501
    },
    {
        "key": "search",
        "label": "Search",
        "icon": "M12 3c2.755 0 5.455.232 8.083.678.533.09.917.556.917 1.096v1.044a2.25 2.25 0 01-.659 1.591l-5.432 5.432a2.25 2.25 0 00-.659 1.591v2.927a2.25 2.25 0 01-1.244 2.013L9.75 21v-6.568a2.25 2.25 0 00-.659-1.591L3.659 7.409A2.25 2.25 0 013 5.818V4.774c0-.54.384-1.006.917-1.096A48.32 48.32 0 0112 3z",  # noqa: E501
    },
    {
        "key": "utilities",
        "label": "Utilities",
        "icon": "M11.42 15.17L17.25 21A2.652 2.652 0 0021 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 11-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 004.486-6.336l-3.276 3.277a3.004 3.004 0 01-2.25-2.25l3.276-3.276a4.5 4.5 0 00-6.336 4.486c.091 1.076-.071 2.264-.904 2.95l-.102.085",  # noqa: E501
    },
    {
        "key": "ui",
        "label": "UI",
        "icon": "M4.098 19.902a3.75 3.75 0 005.304 0l6.401-6.402M6.75 21A3.75 3.75 0 013 17.25V4.125C3 3.504 3.504 3 4.125 3h5.25c.621 0 1.125.504 1.125 1.125v4.072M6.75 21a3.75 3.75 0 003.75-3.75V8.197M6.75 21h13.125c.621 0 1.125-.504 1.125-1.125v-5.25c0-.621-.504-1.125-1.125-1.125h-4.072M10.5 8.197l2.88-2.88c.438-.439 1.15-.439 1.59 0l3.712 3.713c.44.44.44 1.152 0 1.59l-2.879 2.88M6.75 17.25h.008v.008H6.75v-.008z",  # noqa: E501
    },
)

_SETTINGS_TABS = (
    "general",
    "media",
    "clients",
    "indexers",
    "resolvers",
    "direct",
    "metadata",
    "search",
    "utilities",
    "ui",
)

_GetTemplates = Callable[[], Jinja2Templates]
_BuildContext = Callable[..., dict[str, object]]
_ResolveUtilityBrowsePaths = Callable[[dict[str, str]], dict[str, str]]

_get_templates: _GetTemplates | None = None
_build_context: _BuildContext | None = None
_resolve_utility_browse_paths: _ResolveUtilityBrowsePaths | None = None


def configure_settings_routes(
    *,
    get_templates: _GetTemplates,
    build_context: _BuildContext,
    resolve_utility_browse_paths: _ResolveUtilityBrowsePaths,
) -> None:
    """Provide shared UI runtime dependencies from the facade module."""
    global _get_templates, _build_context, _resolve_utility_browse_paths
    _get_templates = get_templates
    _build_context = build_context
    _resolve_utility_browse_paths = resolve_utility_browse_paths


def _templates() -> Jinja2Templates:
    if _get_templates is None:
        msg = "settings routes have not been configured with templates"
        raise RuntimeError(msg)
    return _get_templates()


def _ctx(request: Request, user: object | None = None, **kwargs: object) -> dict[str, object]:
    if _build_context is None:
        msg = "settings routes have not been configured with a context builder"
        raise RuntimeError(msg)
    context: Mapping[str, object] = _build_context(request, user, **kwargs)
    return dict(context)


def _utility_browse_paths(configs: dict[str, str]) -> dict[str, str]:
    if _resolve_utility_browse_paths is None:
        msg = "settings routes have not been configured with utility browse paths"
        raise RuntimeError(msg)
    return _resolve_utility_browse_paths(configs)


def load_client_status_seed(
    request: Request,
    clients: Sequence[DownloadClientConfig],
) -> dict[int, dict[str, object]]:
    """Build a stable initial client-status mapping for settings clients cards."""
    cached_seed: dict[str, object] = {}
    cached_cookie = request.cookies.get(_CLIENT_STATUS_CACHE_COOKIE)
    if cached_cookie:
        try:
            parsed = json.loads(cached_cookie)
            if isinstance(parsed, dict):
                cached_seed = parsed
        except json.JSONDecodeError:
            cached_seed = {}

    seed: dict[int, dict[str, object]] = {}
    for client in clients:
        if client.last_success_at and (
            not client.last_failure_at or client.last_success_at >= client.last_failure_at
        ):
            seed[client.id] = {
                "healthy": True,
                "message": (client.last_test_message or "Connection healthy.").strip(),
            }
        elif client.last_failure_at:
            seed[client.id] = {
                "healthy": False,
                "message": (
                    client.last_error
                    or client.last_test_message
                    or "Latest connection test failed."
                ).strip(),
            }
        else:
            cached_entry = cached_seed.get(str(client.id))
            if isinstance(cached_entry, dict):
                healthy = cached_entry.get("healthy")
                message = cached_entry.get("message")
                if isinstance(healthy, bool) and isinstance(message, str) and message.strip():
                    seed[client.id] = {"healthy": healthy, "message": message.strip()}

    return seed


def load_indexer_status_seed(
    request: Request,
    indexers: Sequence[IndexerConfig],
) -> dict[int, bool]:
    """Build a stable initial indexer-status mapping for settings indexer cards."""
    cached_seed: dict[str, object] = {}
    cached_cookie = request.cookies.get(_INDEXER_STATUS_CACHE_COOKIE)
    if cached_cookie:
        try:
            parsed = json.loads(cached_cookie)
            if isinstance(parsed, dict):
                cached_seed = parsed
        except json.JSONDecodeError:
            cached_seed = {}

    seed: dict[int, bool] = {}
    for indexer in indexers:
        cached_entry = cached_seed.get(str(indexer.id))
        if isinstance(cached_entry, bool):
            seed[indexer.id] = cached_entry
            continue

        if indexer.last_success_at and (
            not indexer.last_failure_at or indexer.last_success_at >= indexer.last_failure_at
        ):
            seed[indexer.id] = True
        elif indexer.last_failure_at:
            seed[indexer.id] = False

    return seed


async def load_settings_tab(request: Request, session: DbSession, tab: str) -> dict[str, object]:
    """Load data needed for a settings tab."""
    ctx: dict[str, object] = {}

    if tab == "general":
        from pullbox.core.config_resolver import (
            get_runtime_status_snapshot,
            https_settings_snapshot_to_dict,
            load_app_identity_settings,
            load_https_settings_snapshot,
            runtime_snapshot_to_dict,
        )

        # DB-backed settings (logging, backup, etc.)
        result = await session.execute(select(SystemConfig).order_by(SystemConfig.key))
        ctx["configs"] = {c.key: c.value for c in result.scalars().all()}

        identity = await load_app_identity_settings(session)
        ctx["identity"] = {
            "instance_name": identity.instance_name.value,
            "base_url": identity.base_url.value,
            "instance_name_source": identity.instance_name.source,
            "base_url_source": identity.base_url.source,
        }
        ctx["runtime_info"] = runtime_snapshot_to_dict(get_runtime_status_snapshot())
        ctx["https_settings"] = https_settings_snapshot_to_dict(
            await load_https_settings_snapshot(session)
        )
    elif tab == "media":
        from pullbox.core.config_resolver import (
            get_runtime_status_snapshot,
            runtime_snapshot_to_dict,
        )

        result = await session.execute(select(SystemConfig).order_by(SystemConfig.key))
        configs = {c.key: c.value for c in result.scalars().all()}
        configs["non_standard_file_template"] = resolve_collection_non_standard_file_template(
            configs.get("non_standard_file_template")
        )
        configs["single_non_standard_file_template"] = resolve_single_non_standard_file_template(
            configs.get("single_non_standard_file_template")
        )
        ctx["configs"] = configs
        runtime = runtime_snapshot_to_dict(get_runtime_status_snapshot())
        ctx["host_info"] = {"library_root": runtime["library_root"]["value"]}
    elif tab == "metadata":
        result = await session.execute(select(SystemConfig).order_by(SystemConfig.key))
        ctx["configs"] = {c.key: c.value for c in result.scalars().all()}
        # Resolve and obfuscate the ComicVine API key for display
        from pullbox.core.comicvine_key import get_comicvine_api_key, obfuscate_api_key

        active_key = await get_comicvine_api_key(session)
        ctx["has_comicvine_key"] = bool(active_key)
        ctx["obfuscated_key"] = obfuscate_api_key(active_key)
    elif tab == "search":
        result = await session.execute(select(SystemConfig).order_by(SystemConfig.key))
        ctx["configs"] = {c.key: c.value for c in result.scalars().all()}
    elif tab == "resolvers":
        from pullbox.schemas.direct_resolver import DirectResolverResponse
        from pullbox.services.direct_resolver_service import list_direct_resolvers

        resolvers = await list_direct_resolvers(session)
        ctx["direct_resolvers"] = resolvers
        ctx["direct_resolver_seed"] = [
            DirectResolverResponse.model_validate(resolver).model_dump(mode="json")
            for resolver in resolvers
        ]
    elif tab == "clients":
        client_result = await session.execute(
            select(DownloadClientConfig)
            .options(selectinload(DownloadClientConfig.airdcpp_settings))
            .order_by(DownloadClientConfig.priority, DownloadClientConfig.name)
        )
        clients: list[DownloadClientConfig] = list(client_result.scalars().all())
        ctx["clients"] = clients
        ctx["airdcpp_enabled"] = get_settings().airdcpp_enabled
        ctx["client_status_seed"] = load_client_status_seed(request, clients)
        cfg_result = await session.execute(
            select(SystemConfig).where(
                SystemConfig.key.in_(
                    [
                        "download_poll_interval_seconds",
                        "process_completed_interval_seconds",
                        "max_download_retries",
                        "stall_timeout_hours",
                        "history_retention_days",
                    ]
                )
            )
        )
        ctx["configs"] = {c.key: c.value for c in cfg_result.scalars().all()}
    elif tab == "indexers":
        from pullbox.models.direct_acquisition import DirectResolverConfig

        indexer_result = await session.execute(
            select(IndexerConfig).order_by(IndexerConfig.priority, IndexerConfig.name)
        )
        indexers: list[IndexerConfig] = list(indexer_result.scalars().all())
        ctx["indexers"] = indexers
        ctx["browser_resolver_available"] = (
            await session.scalar(select(DirectResolverConfig.id).limit(1)) is not None
        )
        manager_sources_by_name: dict[str, set[str]] = {}
        manager_display_names: dict[str, str] = {}
        for indexer in indexers:
            source = str(indexer.source)
            if source not in {"prowlarr", "jackett"} or not indexer.manager_available:
                continue
            suffix = f" ({source.title()})"
            display_name = indexer.name
            if display_name.casefold().endswith(suffix.casefold()):
                display_name = display_name[: -len(suffix)]
            normalized_name = " ".join(display_name.casefold().split())
            manager_sources_by_name.setdefault(normalized_name, set()).add(source)
            manager_display_names.setdefault(normalized_name, display_name)
        ctx["indexer_manager_duplicates"] = sorted(
            manager_display_names[name]
            for name, sources in manager_sources_by_name.items()
            if {"prowlarr", "jackett"}.issubset(sources)
        )
        ctx["indexer_status_seed"] = load_indexer_status_seed(request, indexers)
        cfg_result = await session.execute(
            select(SystemConfig).where(
                SystemConfig.key.in_(
                    [
                        "indexer_failure_threshold",
                        "prowlarr_url",
                        "prowlarr_api_key",
                        "jackett_url",
                        "jackett_api_key",
                        "source_priority",
                    ]
                )
            )
        )
        configs = {c.key: c.value for c in cfg_result.scalars().all()}
        # Redact prowlarr_api_key: only expose whether it is set plus an obfuscated preview.
        from pullbox.core.comicvine_key import obfuscate_api_key
        from pullbox.core.encryption import decrypt_secret

        has_prowlarr_key = bool(configs.get("prowlarr_api_key", ""))
        obfuscated_prowlarr_key = ""
        if has_prowlarr_key:
            try:
                obfuscated_prowlarr_key = obfuscate_api_key(
                    decrypt_secret(configs["prowlarr_api_key"])
                )
            except (ValueError, Exception):
                obfuscated_prowlarr_key = ""
        configs["prowlarr_api_key"] = ""  # never send to browser
        configs["has_prowlarr_api_key"] = "true" if has_prowlarr_key else ""
        configs["obfuscated_prowlarr_api_key"] = obfuscated_prowlarr_key

        has_jackett_key = bool(configs.get("jackett_api_key", ""))
        obfuscated_jackett_key = ""
        if has_jackett_key:
            try:
                obfuscated_jackett_key = obfuscate_api_key(
                    decrypt_secret(configs["jackett_api_key"])
                )
            except (ValueError, Exception):
                obfuscated_jackett_key = ""
        configs["jackett_api_key"] = ""  # never send to browser
        configs["has_jackett_api_key"] = "true" if has_jackett_key else ""
        configs["obfuscated_jackett_api_key"] = obfuscated_jackett_key
        ctx["configs"] = configs
        # Load blocklist config for the blocklist section
        bl_row = await session.get(SystemConfig, "blocklist.release_groups")
        ctx["blocked_groups"] = (
            [g.strip() for g in bl_row.value.split(",") if g.strip()]
            if bl_row and bl_row.value
            else []
        )
        bl_expiry = await session.get(SystemConfig, "blocklist.expiry_days")
        ctx["blocklist_expiry_days"] = bl_expiry.value if bl_expiry else "90"
        bl_auto = await session.get(SystemConfig, "blocklist.auto_add_on_failure")
        ctx["blocklist_auto_add"] = (bl_auto.value.lower() == "true") if bl_auto else True
    elif tab == "direct":
        from pullbox.schemas.direct_host import DirectHostResponse
        from pullbox.schemas.direct_provider import DirectProviderResponse
        from pullbox.services.direct_host_settings import list_direct_host_settings
        from pullbox.services.direct_provider_capabilities import visible_artifact_host_kinds
        from pullbox.services.direct_provider_registration import list_direct_providers

        providers = await list_direct_providers(session)
        visible_host_kinds = visible_artifact_host_kinds(
            provider.artifact_host_patterns for provider in providers if provider.enabled
        )
        hosts = (
            [
                host
                for host in await list_direct_host_settings(session)
                if host.host_kind in visible_host_kinds
            ]
            if visible_host_kinds
            else []
        )
        ctx["direct_providers"] = providers
        ctx["direct_provider_seed"] = [
            DirectProviderResponse.model_validate(provider).model_dump(mode="json")
            for provider in providers
        ]
        ctx["direct_hosts"] = hosts
        ctx["direct_host_seed"] = [
            DirectHostResponse.model_validate(host).model_dump(mode="json") for host in hosts
        ]
    elif tab == "utilities":
        result = await session.execute(
            select(SystemConfig).where(
                SystemConfig.key.in_(
                    [
                        "utility_worker_count",
                        "utility_trash_folder",
                        "utility_trash_retention_days",
                        "utility_export_folder",
                        "utility_job_retention_days",
                        "utility_log_level",
                    ]
                )
            )
        )
        configs = {c.key: c.value for c in result.scalars().all()}
        ctx["configs"] = configs
        ctx["utility_browse_paths"] = _utility_browse_paths(configs)
    return ctx


def _normalize_settings_tab(tab: str) -> str:
    if tab not in _SETTINGS_TABS:
        return "general"
    return tab


async def _load_settings_response_context(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    tab: str,
) -> dict[str, object]:
    tab = _normalize_settings_tab(tab)
    tab_data = await load_settings_tab(request, session, tab)
    extra: dict[str, object] = dict(tab_data)
    if tab == "ui":
        from pullbox.core.display_time import load_display_settings

        extra["ui_settings"] = await load_display_settings(session)
        extra["tz_env"] = os.environ.get("TZ", "")
    return _ctx(request, user, tab=tab, settings_tabs=SETTINGS_TABS, **extra)


@page_router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings(
    request: Request,
    user: AuthenticatedUser,
    session: DbSession,
    tab: str = Query("general"),
) -> Response:
    """Render the settings page with tabbed interface."""
    return _templates().TemplateResponse(
        request,
        "pages/settings.html",
        await _load_settings_response_context(request, user, session, tab),
    )


@htmx_router.get("/htmx/settings/{tab}", response_class=HTMLResponse, include_in_schema=False)
async def htmx_settings_tab(
    request: Request,
    tab: str,
    user: AuthenticatedUser,
    session: DbSession,
) -> Response:
    """Load a settings tab partial via HTMX."""
    return _templates().TemplateResponse(
        request,
        "partials/settings_content_bundle.html",
        await _load_settings_response_context(request, user, session, tab),
    )
