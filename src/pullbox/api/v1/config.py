"""Configuration API routes — get/update system config and naming preview."""

from pathlib import Path

import structlog
from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from pullbox.api.deps import DbSession, InteractiveOperatorUser
from pullbox.core.config_resolver import (
    get_runtime_settings,
    load_all_system_config_values,
    normalize_base_url,
)
from pullbox.core.https_runtime import (
    HTTPS_CONFIG_KEYS,
    https_runtime_config_values,
    validate_https_config_values,
)
from pullbox.core.library_permissions import PermissionPolicyError, parse_permission_mode
from pullbox.core.local_auth_bypass import normalize_local_bypass_addresses
from pullbox.core.naming import format_filename, get_naming_preview
from pullbox.models.config import DEFAULT_SYSTEM_CONFIG, SystemConfig
from pullbox.models.user import User
from pullbox.schemas.config import (
    ConfigResponse,
    ConfigUpdate,
    LibraryRootCreate,
    LibraryRootPolicyClear,
    LibraryRootPolicyPreviewRequest,
    LibraryRootPolicyPreviewResponse,
    LibraryRootPolicyState,
    LibraryRootPolicyUpdate,
    LibraryRootPreviewResponse,
    LibraryRootRebindConfirmRequest,
    LibraryRootRebindPreviewRequest,
    LibraryRootRebindPreviewResponse,
    LibraryRootState,
    LibraryRootUpdate,
    NamingPreview,
    NamingPreviewEntry,
    NamingPreviewGrouped,
)
from pullbox.services.library_root_management import (
    create_library_root,
    list_library_roots,
    preview_library_root,
    preview_library_root_rebind,
    rebind_library_root,
    update_library_root,
)
from pullbox.services.library_root_policy_service import (
    clear_library_root_policy,
    get_library_root_policy_state,
    preview_library_root_policy,
    update_library_root_policy,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/config", tags=["config"], include_in_schema=False)


def _validate_library_permission_setting(key: str, value: str) -> None:
    """Validate library permission config before it reaches import workflows."""
    from pullbox.core.exceptions import ValidationError

    if key == "library_permissions_folder_mode":
        try:
            parse_permission_mode(value, target_kind="folder")
        except PermissionPolicyError as exc:
            raise ValidationError(str(exc)) from exc
    elif key == "library_permissions_file_mode":
        try:
            parse_permission_mode(value, target_kind="file")
        except PermissionPolicyError as exc:
            raise ValidationError(str(exc)) from exc
    elif key == "library_permissions_hardlink_behavior" and value != "skip":
        raise ValidationError(
            f"Library permission hardlink behavior must be one of: skip; got: {value}"
        )
    elif key == "library_permissions_symlink_behavior" and value != "skip":
        raise ValidationError(
            f"Library permission symlink behavior must be one of: skip; got: {value}"
        )


async def _effective_config_values(
    session: DbSession,
    incoming_values: dict[str, str],
    keys: tuple[str, ...],
) -> dict[str, str]:
    """Return effective config values after applying the incoming update body."""
    values: dict[str, str] = {}
    for key in keys:
        if key in incoming_values:
            values[key] = incoming_values[key]
            continue
        config = await session.get(SystemConfig, key)
        values[key] = config.value if config is not None else str(DEFAULT_SYSTEM_CONFIG[key][0])
    return values


# ── Get Config ───────────────────────────────────────────────────────


@router.get("", response_model=list[ConfigResponse])
async def get_config(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> list[ConfigResponse]:
    """Get all system configuration values.

    Secret-type values (e.g. comicvine_api_key) are obfuscated — only the
    last 4 characters are visible. The full/encrypted value is never returned.
    """
    from pullbox.core.comicvine_key import obfuscate_api_key
    from pullbox.core.encryption import decrypt_secret

    responses: list[ConfigResponse] = []

    configs = await load_all_system_config_values(session)
    for key in sorted(DEFAULT_SYSTEM_CONFIG.keys()):
        default_value, default_type = DEFAULT_SYSTEM_CONFIG[key]
        config = configs.get(key)
        value = default_value if config is None else config.value
        value_type = default_type if config is None else config.value_type
        description = None if config is None else config.description

        if value_type == "secret" and value:
            try:
                plaintext = decrypt_secret(value)
                obfuscated = obfuscate_api_key(plaintext)
            except Exception:
                obfuscated = "••••••••"
            responses.append(
                ConfigResponse(
                    key=key,
                    value=obfuscated,
                    value_type=value_type,
                    description=description,
                )
            )
        else:
            responses.append(
                ConfigResponse(
                    key=key,
                    value=value,
                    value_type=value_type,
                    description=description,
                )
            )
    return responses


# ── Update Config ────────────────────────────────────────────────────


@router.put("")
async def update_config(
    request: Request,
    body: ConfigUpdate,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> dict[str, object]:
    """Update one or more system configuration values."""
    from pullbox.core.exceptions import ValidationError

    valid_keys = set(DEFAULT_SYSTEM_CONFIG.keys())
    # Secrets must be saved through their dedicated endpoints (encrypted)
    secret_keys = {"comicvine_api_key"}
    runtime_managed_keys = {"logs_dir", "backup_dir"}
    runtime_managed_https = https_runtime_config_values()

    actually_changed: set[str] = set()
    old_values: dict[str, str] = {}

    for key, value in body.values.items():
        if key in secret_keys:
            raise ValidationError(
                f"'{key}' must be saved through its dedicated endpoint, not the generic config API."
            )
        if key not in valid_keys:
            raise ValidationError(f"Unknown configuration key: {key}")
        if key in runtime_managed_keys:
            raise ValidationError(
                f"'{key}' is runtime-managed and must be configured at startup, not from the app."
            )
        if key in runtime_managed_https:
            raise ValidationError(
                f"'{key}' is runtime-managed and must be configured at startup, not from the app."
            )

        config = await session.get(SystemConfig, key)
        if not config:
            # Auto-create from defaults if key is valid but missing from DB
            default_value, default_type = DEFAULT_SYSTEM_CONFIG[key]
            config = SystemConfig(key=key, value=default_value, value_type=default_type)
            session.add(config)

        # Type validation
        if config.value_type == "int":
            try:
                int(value)
            except ValueError:
                raise ValidationError(
                    f"Value for '{key}' must be an integer, got: {value}"
                ) from None
        elif config.value_type == "bool" and value.lower() not in (
            "true",
            "false",
            "1",
            "0",
        ):
            raise ValidationError(f"Value for '{key}' must be a boolean, got: {value}")

        # Enumerated value validation
        if key == "post_processing_method" and value not in (
            "move",
            "copy",
            "hardlink",
            "symlink",
        ):
            raise ValidationError(
                f"Value for 'post_processing_method' must be one of: "
                f"move, copy, hardlink, symlink; got: {value}"
            )

        if key == "torrent_import_strategy" and value not in ("standard", "seed_safe"):
            raise ValidationError(
                f"Value for 'torrent_import_strategy' must be one of: "
                f"standard, seed_safe; got: {value}"
            )

        if key == "preferred_format" and value not in (
            "cbz",
            "cbr",
            "cb7",
            "pdf",
            "epub",
        ):
            raise ValidationError(
                f"Value for 'preferred_format' must be one of: "
                f"cbz, cbr, cb7, pdf, epub; got: {value}"
            )

        if key == "colon_replacement" and value not in (
            "dash",
            "space",
            "empty",
            "smart",
        ):
            raise ValidationError(
                f"Value for 'colon_replacement' must be one of: "
                f"dash, space, empty, smart; got: {value}"
            )

        if key == "allowed_import_extensions":
            from pullbox.core.file_safety import DANGEROUS_EXTENSIONS

            for ext in value.split(","):
                ext = ext.strip().lower()
                if ext and not ext.startswith("."):
                    ext = "." + ext
                if ext in DANGEROUS_EXTENSIONS:
                    raise ValidationError(f"Extension '{ext}' is blocked for security reasons")

        if key == "session_lifetime_hours":
            hours = int(value)
            if hours < 1 or hours > 720:
                raise ValidationError(
                    f"Session lifetime must be between 1 and 720 hours, got: {hours}"
                )
        if key == "local_auth_bypass_addresses":
            try:
                value = normalize_local_bypass_addresses(value)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        if key == "local_auth_bypass_username":
            value = value.strip()
        if key == "process_completed_interval_seconds":
            seconds = int(value)
            if seconds < 300 or seconds > 600:
                raise ValidationError(
                    "Process-completed recovery sweep interval must be between "
                    f"300 and 600 seconds, got: {seconds}"
                )
        if key in {
            "health_scheduler_interval_minutes",
            "health_database_interval_minutes",
            "health_filesystem_interval_minutes",
            "health_system_interval_minutes",
        }:
            minutes = int(value)
            if minutes < 1 or minutes > 1440:
                raise ValidationError(
                    f"Health check interval must be between 1 and 1440 minutes, got: {minutes}"
                )
        if key in {
            "health_download_clients_interval_hours",
            "health_indexers_interval_hours",
            "health_comicvine_interval_hours",
        }:
            hours = int(value)
            if hours < 1 or hours > 168:
                raise ValidationError(
                    f"Health check interval must be between 1 and 168 hours, got: {hours}"
                )
        if key == "health_history_retention_days":
            days = int(value)
            if days < 1 or days > 30:
                raise ValidationError(
                    f"Health history retention must be between 1 and 30 days, got: {days}"
                )
        if key == "instance_name":
            value = value.strip()
            if not value:
                raise ValidationError("Instance name cannot be empty.")
        if key == "base_url":
            try:
                value = normalize_base_url(value)
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        body.values[key] = value
        _validate_library_permission_setting(key, value)

        # Utility settings validation (worker count, retention days, log level)
        from pullbox.utilities.settings import validate_utility_setting

        validate_utility_setting(key, value)

        if config.value != value:
            actually_changed.add(key)
            old_values[key] = config.value
            config.value = value

    if "comics_directory" in body.values and body.values["comics_directory"].strip():
        from pullbox.core.exceptions import ValidationError
        from pullbox.services.library_service import set_comics_directory

        try:
            root = await set_comics_directory(
                session,
                Path(body.values["comics_directory"]),
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        body.values["comics_directory"] = root.path

    https_keys = set(HTTPS_CONFIG_KEYS)
    if https_keys & body.values.keys():
        effective_https = await _effective_config_values(session, body.values, HTTPS_CONFIG_KEYS)
        effective_https.update(runtime_managed_https)
        try:
            validate_https_config_values(
                enabled=effective_https["https_enabled"],
                cert_path=effective_https["https_cert_path"],
                key_path=effective_https["https_key_path"],
                cert_root=get_runtime_settings().https_cert_root,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    local_bypass_keys = (
        "local_auth_bypass_enabled",
        "local_auth_bypass_addresses",
        "local_auth_bypass_username",
    )
    effective_local_bypass = await _effective_config_values(
        session,
        body.values,
        local_bypass_keys,
    )
    effective_local_bypass_enabled = effective_local_bypass["local_auth_bypass_enabled"]
    effective_local_bypass_addresses = effective_local_bypass["local_auth_bypass_addresses"]
    effective_local_bypass_username = effective_local_bypass["local_auth_bypass_username"].strip()

    if str(effective_local_bypass_enabled).lower() == "true":
        if not str(effective_local_bypass_addresses).strip():
            raise ValidationError(
                "Local auth bypass requires at least one trusted local address or CIDR."
            )

        if effective_local_bypass_username:
            user_result = await session.execute(
                select(User).where(
                    User.username == effective_local_bypass_username,
                    User.is_active.is_(True),
                )
            )
            if user_result.scalar_one_or_none() is None:
                raise ValidationError("Local bypass account must match an active username.")
        else:
            active_user_result = await session.execute(
                select(User).where(User.is_active.is_(True)).order_by(User.id.asc()).limit(2)
            )
            active_users = list(active_user_result.scalars().all())
            if len(active_users) != 1:
                raise ValidationError(
                    "Local auth bypass must target a specific active username when more than "
                    "one active account exists."
                )

    effective_transfer_method = body.values.get("post_processing_method")
    if effective_transfer_method is None:
        existing_transfer = await session.get(SystemConfig, "post_processing_method")
        effective_transfer_method = (
            existing_transfer.value if existing_transfer is not None else "move"
        )

    effective_convert_on_import = body.values.get("convert_to_preferred_format_on_import")
    if effective_convert_on_import is None:
        existing_convert = await session.get(SystemConfig, "convert_to_preferred_format_on_import")
        effective_convert_on_import = (
            existing_convert.value if existing_convert is not None else "false"
        )

    effective_update_embedded = body.values.get("update_embedded_comicinfo_from_match_on_import")
    if effective_update_embedded is None:
        existing_update = await session.get(
            SystemConfig, "update_embedded_comicinfo_from_match_on_import"
        )
        effective_update_embedded = (
            existing_update.value if existing_update is not None else "false"
        )

    if str(effective_convert_on_import).lower() == "true" and effective_transfer_method in {
        "hardlink",
        "symlink",
    }:
        raise ValidationError(
            "Normalize Imported Archives to CBZ requires the transfer method to be Move or Copy."
        )
    if str(effective_update_embedded).lower() == "true" and effective_transfer_method not in {
        "move",
        "copy",
    }:
        raise ValidationError(
            "Updating embedded ComicInfo.xml on import requires the transfer "
            "method to be Move or Copy."
        )

    await session.flush()

    logging_keys = {"log_level", "log_size_limit_mb", "log_backup_count"}
    utility_logging_keys = {"utility_log_level"}
    network_keys = {"instance_name", "base_url"}
    scheduler_keys = {
        "search_interval_hours",
        "download_poll_interval_seconds",
        "process_completed_interval_seconds",
        "health_scheduler_interval_minutes",
        "health_database_interval_minutes",
        "health_filesystem_interval_minutes",
        "health_system_interval_minutes",
        "health_download_clients_interval_hours",
        "health_indexers_interval_hours",
        "health_comicvine_interval_hours",
    }
    import_policy_keys = {
        "post_processing_method",
        "torrent_import_strategy",
        "rename_on_import",
        "search_on_add",
        "skip_existing_files",
        "replace_illegal_characters",
        "colon_replacement",
        "series_folder_template",
        "issue_filename_template",
        "nonstandard_filename_template",
        "convert_to_preferred_format_on_import",
        "update_embedded_comicinfo_from_match_on_import",
        "library_permissions_enabled",
        "library_permissions_folder_mode",
        "library_permissions_file_mode",
        "library_permissions_apply_to_created_folders",
        "library_permissions_apply_to_materialized_files",
        "library_permissions_hardlink_behavior",
        "library_permissions_symlink_behavior",
    }

    if actually_changed:
        for key in sorted(actually_changed):
            is_secret = key in secret_keys
            logger.debug(
                "config_changed",
                key=key,
                old_value="[REDACTED]" if is_secret else old_values[key],
                new_value="[REDACTED]" if is_secret else body.values[key],
                store="database",
            )

        if actually_changed & logging_keys:
            logger.info(
                "logging_config_updated",
                changed_keys=sorted(actually_changed & logging_keys),
                store="database",
            )
        if actually_changed & utility_logging_keys:
            logger.info(
                "utility_logging_config_updated",
                changed_keys=sorted(actually_changed & utility_logging_keys),
                store="database",
            )
        if actually_changed & network_keys:
            logger.info(
                "network_config_updated",
                changed_keys=sorted(actually_changed & network_keys),
                store="database",
            )
        if actually_changed & https_keys:
            logger.info(
                "https_config_updated",
                changed_keys=sorted(actually_changed & https_keys),
                store="database",
            )
        if actually_changed & scheduler_keys:
            logger.info(
                "scheduler_config_updated",
                changed_keys=sorted(actually_changed & scheduler_keys),
                store="database",
            )
        if actually_changed & import_policy_keys:
            logger.info(
                "import_policy_updated",
                changed_keys=sorted(actually_changed & import_policy_keys),
                store="database",
            )

    # Invalidate display settings cache if any display.* keys changed
    if any(k.startswith("display.") for k in actually_changed):
        from pullbox.core.display_time import invalidate_display_cache

        invalidate_display_cache()

    # Update instance name cache for <title> tags
    if "instance_name" in actually_changed:
        import pullbox.ui.routes as _ui_routes

        _ui_routes._cached_instance_name = body.values["instance_name"].strip()

    # Update base URL cache
    if "base_url" in actually_changed:
        import pullbox.ui.routes as _ui_routes

        _ui_routes._cached_base_url = normalize_base_url(body.values["base_url"])

    # Audit security-related config changes
    security_keys = {
        "local_auth_bypass_enabled",
        "local_auth_bypass_addresses",
        "local_auth_bypass_username",
        "session_lifetime_hours",
        "allowed_import_extensions",
        "block_dangerous_files",
        "https_enabled",
        "https_cert_path",
        "https_key_path",
    }
    changed_security = security_keys & actually_changed
    if changed_security:
        from pullbox.models.audit_log import AuditEventType
        from pullbox.services.audit_service import record_audit_event, source_ip_from_request

        await record_audit_event(
            session,
            AuditEventType.SECURITY_CONFIG_CHANGED,
            source_ip=source_ip_from_request(request),
            user_id=_user.id,
            username=_user.username,
            detail=f"Security config updated: {', '.join(sorted(changed_security))}",
        )

        # Specific audit for local bypass toggle
        if "local_auth_bypass_enabled" in changed_security:
            new_state = body.values["local_auth_bypass_enabled"].lower() == "true"
            await record_audit_event(
                session,
                AuditEventType.LOCAL_BYPASS_TOGGLED,
                source_ip=source_ip_from_request(request),
                user_id=_user.id,
                username=_user.username,
                detail=f"Local auth bypass {'enabled' if new_state else 'disabled'}",
            )

    # Apply logging changes at runtime (level, size, backup count)
    # Log directory changes still require a restart.
    if logging_keys & body.values.keys():
        from pullbox.logging import reconfigure_logging_runtime

        # Read the current effective values from DB
        log_cfg_result = await session.execute(
            select(SystemConfig).where(SystemConfig.key.in_(logging_keys))
        )
        log_cfg = {c.key: c.value for c in log_cfg_result.scalars().all()}

        reconfigure_logging_runtime(
            log_level=log_cfg.get("log_level", "info"),
            log_size_limit_mb=int(log_cfg["log_size_limit_mb"])
            if "log_size_limit_mb" in log_cfg
            else None,
            log_backup_count=int(log_cfg["log_backup_count"])
            if "log_backup_count" in log_cfg
            else None,
        )

    if utility_logging_keys & body.values.keys():
        from pullbox.utilities.logging_config import configure_utility_logging_runtime

        utility_cfg_result = await session.execute(
            select(SystemConfig).where(SystemConfig.key.in_(utility_logging_keys))
        )
        utility_cfg = {c.key: c.value for c in utility_cfg_result.scalars().all()}
        effective_level = utility_cfg.get(
            "utility_log_level",
            str(DEFAULT_SYSTEM_CONFIG["utility_log_level"][0]),
        )
        settings = get_runtime_settings()

        try:
            configure_utility_logging_runtime(
                log_dir=settings.logs_dir,
                level=effective_level,
            )
        except OSError:
            logger.warning(
                "utility_logging_runtime_reconfigure_failed",
                logs_dir=str(settings.logs_dir),
                utility_log_level=effective_level,
                exc_info=True,
            )

    # Build response — merge DB settings with defaults
    response_configs: list[dict[str, object]] = []
    configs = await load_all_system_config_values(session)
    for key in sorted(DEFAULT_SYSTEM_CONFIG.keys()):
        default_value, default_type = DEFAULT_SYSTEM_CONFIG[key]
        row = configs.get(key)
        response_configs.append(
            {
                "key": key,
                "value": default_value if row is None else row.value,
                "value_type": default_type if row is None else row.value_type,
            }
        )

    restart_required_keys = sorted(actually_changed & https_keys)
    response: dict[str, object] = {"configs": response_configs}
    if restart_required_keys:
        response["restart_required"] = True
        response["restart_required_keys"] = restart_required_keys
    return response


# ── ComicVine API Key ────────────────────────────────────────────────


@router.post("/comicvine/test")
async def test_comicvine_key(
    _user: InteractiveOperatorUser,
    session: DbSession,
    body: dict[str, str],
) -> dict[str, object]:
    """Test a ComicVine API key without saving it."""
    from pullbox.providers.metadata.comicvine import ComicVineProvider

    api_key = body.get("api_key", "").strip()
    if not api_key:
        return {"healthy": False, "message": "No API key provided."}

    provider = ComicVineProvider(api_key=api_key)
    try:
        result = await provider.test_connection()
        return {
            "healthy": result.healthy,
            "message": result.message,
            "response_time_ms": result.response_time_ms,
        }
    except Exception as exc:
        logger.warning("comicvine_key_test_failed", exc_info=exc)
        return {
            "healthy": False,
            "message": "Connection failed. Check Pullbox logs for details.",
        }
    finally:
        await provider.close()


@router.post("/comicvine/save")
async def save_comicvine_key(
    _user: InteractiveOperatorUser,
    session: DbSession,
    body: dict[str, str],
) -> dict[str, object]:
    """Save (encrypted) the ComicVine API key to the database."""
    from pullbox.core.comicvine_key import obfuscate_api_key, save_comicvine_api_key

    api_key = body.get("api_key", "").strip()
    if not api_key:
        return {"saved": False, "message": "No API key provided."}

    await save_comicvine_api_key(session, api_key)
    return {
        "saved": True,
        "message": "API key saved.",
        "obfuscated": obfuscate_api_key(api_key),
    }


# ── Library Root Management ────────────────────────────────────────


@router.get("/library-roots", response_model=list[LibraryRootState])
async def get_library_roots(
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> list[LibraryRootState]:
    """List configured roots with live read/write/capacity state."""
    states = await list_library_roots(session)
    return [LibraryRootState.model_validate(state) for state in states]


@router.post(
    "/library-roots/preview",
    response_model=LibraryRootPreviewResponse,
)
async def preview_new_library_root(
    body: LibraryRootCreate,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> LibraryRootPreviewResponse:
    """Validate a proposed root without persisting it."""
    preview = await preview_library_root(session, **body.model_dump())
    return LibraryRootPreviewResponse.model_validate(preview)


@router.post(
    "/library-roots",
    response_model=LibraryRootState,
    status_code=201,
)
async def post_library_root(
    body: LibraryRootCreate,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> LibraryRootState:
    """Add an existing persistent container directory as a root."""
    state = await create_library_root(session, **body.model_dump())
    logger.info("library_root_created", library_root_id=state["id"])
    return LibraryRootState.model_validate(state)


@router.patch(
    "/library-roots/{library_root_id}",
    response_model=LibraryRootState,
)
async def patch_library_root(
    library_root_id: int,
    body: LibraryRootUpdate,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> LibraryRootState:
    """Update root roles/default state while preserving immutable path identity."""
    state = await update_library_root(
        session,
        library_root_id,
        body.model_dump(exclude_unset=True),
    )
    logger.info("library_root_updated", library_root_id=library_root_id)
    return LibraryRootState.model_validate(state)


@router.post(
    "/library-roots/{library_root_id}/rebind/preview",
    response_model=LibraryRootRebindPreviewResponse,
)
async def preview_existing_library_root_rebind(
    library_root_id: int,
    body: LibraryRootRebindPreviewRequest,
    user: InteractiveOperatorUser,
    session: DbSession,
) -> LibraryRootRebindPreviewResponse:
    """Preview a path rebind and its persisted association impact without writing."""
    preview = await preview_library_root_rebind(
        session,
        library_root_id,
        replacement_path=body.replacement_path,
        actor_id=user.id,
    )
    return LibraryRootRebindPreviewResponse.model_validate(preview)


@router.post(
    "/library-roots/{library_root_id}/rebind",
    response_model=LibraryRootState,
)
async def confirm_existing_library_root_rebind(
    library_root_id: int,
    body: LibraryRootRebindConfirmRequest,
    user: InteractiveOperatorUser,
    session: DbSession,
) -> LibraryRootState:
    """Apply one explicitly confirmed, signed and drift-checked root path rebind."""
    state = await rebind_library_root(
        session,
        library_root_id,
        replacement_path=body.replacement_path,
        preview_token=body.preview_token,
        actor_id=user.id,
    )
    logger.info("library_root_rebound", library_root_id=library_root_id)
    return LibraryRootState.model_validate(state)


# ── Per-root Naming Policy ──────────────────────────────────────────


@router.get(
    "/library-roots/{library_root_id}/naming-policy",
    response_model=LibraryRootPolicyState,
)
async def get_root_naming_policy(
    library_root_id: int,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> LibraryRootPolicyState:
    """Return a library root's effective policy and inheritance scope."""
    state = await get_library_root_policy_state(session, library_root_id)
    return LibraryRootPolicyState.model_validate(state)


@router.put(
    "/library-roots/{library_root_id}/naming-policy",
    response_model=LibraryRootPolicyState,
)
async def put_root_naming_policy(
    library_root_id: int,
    body: LibraryRootPolicyUpdate,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> LibraryRootPolicyState:
    """Create or update one root's explicit policy with optimistic locking."""
    state = await update_library_root_policy(
        session,
        library_root_id,
        expected_revision=body.expected_revision,
        definition=body.policy.model_dump(),
    )
    logger.info(
        "library_root_policy_updated",
        library_root_id=library_root_id,
        revision=state["revision"],
        source="manual",
    )
    return LibraryRootPolicyState.model_validate(state)


@router.delete(
    "/library-roots/{library_root_id}/naming-policy",
    response_model=LibraryRootPolicyState,
)
async def delete_root_naming_policy(
    library_root_id: int,
    body: LibraryRootPolicyClear,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> LibraryRootPolicyState:
    """Clear an explicit policy so the root inherits global defaults again."""
    state = await clear_library_root_policy(
        session,
        library_root_id,
        expected_revision=body.expected_revision,
    )
    logger.info(
        "library_root_policy_cleared",
        library_root_id=library_root_id,
        source="manual",
    )
    return LibraryRootPolicyState.model_validate(state)


@router.post(
    "/library-roots/{library_root_id}/naming-policy/preview",
    response_model=LibraryRootPolicyPreviewResponse,
)
async def preview_root_naming_policy(
    library_root_id: int,
    body: LibraryRootPolicyPreviewRequest,
    _user: InteractiveOperatorUser,
    session: DbSession,
) -> LibraryRootPolicyPreviewResponse:
    """Preview a complete proposal without writing a root policy."""
    preview = await preview_library_root_policy(
        session,
        library_root_id,
        definition=body.policy.model_dump(),
        examples=[example.model_dump() for example in body.examples],
    )
    return LibraryRootPolicyPreviewResponse.model_validate(preview)


# ── Naming Preview ───────────────────────────────────────────────────

_SAMPLE_DATA = [
    ("Batman", 1, 2016),
    ("The Amazing Spider-Man", 42, 1963),
    ("Saga", 7, 2012),
]


@router.get("/naming/test", response_model=NamingPreview)
async def naming_preview(
    _user: InteractiveOperatorUser,
    template: str = Query(
        "{series} ({year}) #{issue:03d}",
        description="Naming template with {series}, {year}, {issue} placeholders",
    ),
) -> NamingPreview:
    """Preview file naming convention with sample data (legacy endpoint)."""
    examples = [
        format_filename(series=s, issue_number=float(i), year=y, template=template)
        for s, i, y in _SAMPLE_DATA
    ]
    return NamingPreview(template=template, examples=examples)


@router.get("/naming/preview", response_model=NamingPreviewGrouped)
async def naming_preview_grouped(
    _user: InteractiveOperatorUser,
    template: str = Query(
        ...,
        description="Naming template string with token placeholders",
    ),
    template_type: str = Query(
        "standard",
        description=(
            "Template type: folder, standard, annual, non_standard, "
            "non_standard_collection, non_standard_single"
        ),
        pattern="^(folder|standard|annual|non_standard|non_standard_collection|non_standard_single)$",
    ),
) -> NamingPreviewGrouped:
    """Preview naming convention with curated real-world examples.

    Returns grouped results filtered by template type, using comic metadata
    derived from NZBGeek test fixtures.
    """
    raw_examples = get_naming_preview(template=template, template_type=template_type)
    entries = [NamingPreviewEntry(**ex) for ex in raw_examples]
    return NamingPreviewGrouped(
        template=template,
        template_type=template_type,
        examples=entries,
    )
