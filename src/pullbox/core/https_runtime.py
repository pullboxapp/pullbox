"""Native HTTPS runtime resolution and validation."""

from __future__ import annotations

import os
import sqlite3
import ssl
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pullbox.config import PullboxSettings, get_settings

if TYPE_CHECKING:
    from collections.abc import Mapping

HTTPS_CONFIG_KEYS = ("https_enabled", "https_cert_path", "https_key_path")
_HTTPS_CONFIG_QUERY = "SELECT key, value FROM system_config WHERE key IN (?, ?, ?)"
HTTPS_ENV_KEYS = {
    "https_enabled": "PULLBOX_HTTPS_ENABLED",
    "https_cert_path": "PULLBOX_HTTPS_CERT_PATH",
    "https_key_path": "PULLBOX_HTTPS_KEY_PATH",
}
HTTPS_CERT_ROOT_ENV = "PULLBOX_HTTPS_CERT_ROOT"
_DEFAULT_CERT_ROOT = Path("/config/certs")


@dataclass(frozen=True, slots=True)
class HttpsRuntimeSettings:
    """Resolved HTTPS startup settings."""

    enabled: bool
    cert_path: str
    key_path: str
    cert_root: Path


def _parse_bool(value: str | bool | int | None) -> bool:
    """Parse bool-like config values from env or system_config."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _settings_bool(settings: object, name: str, default: bool) -> bool:
    value = getattr(settings, name, default)
    if isinstance(value, bool | str | int):
        return _parse_bool(value)
    return default


def _settings_str(settings: object, name: str, default: str = "") -> str:
    value = getattr(settings, name, default)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Path):
        return str(value)
    return default


def _settings_path(settings: object, name: str, default: Path) -> Path:
    value = getattr(settings, name, default)
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value.strip():
        return Path(value.strip())
    return default


def _sqlite_path_from_db_url(db_url: str) -> Path | None:
    """Extract a SQLite file path from the configured DB URL."""
    if not db_url or "sqlite" not in db_url or ":memory:" in db_url:
        return None
    if ":///" in db_url:
        raw = db_url.split(":///", 1)[1]
    elif "://" in db_url:
        raw = db_url.split("://", 1)[1]
    else:
        return None
    raw = raw.split("?", 1)[0]
    return Path(raw) if raw else None


def load_https_db_values(settings: PullboxSettings | object | None = None) -> dict[str, str]:
    """Best-effort sync read of persisted HTTPS config for startup."""
    runtime = settings or get_settings()
    db_path = _sqlite_path_from_db_url(_settings_str(runtime, "db_url"))
    if db_path is None or not db_path.exists():
        return {}

    try:
        with closing(
            sqlite3.connect(
                f"file:{db_path.as_posix()}?mode=ro",
                uri=True,
                timeout=1.0,
            )
        ) as conn:
            rows = conn.execute(
                _HTTPS_CONFIG_QUERY,
                HTTPS_CONFIG_KEYS,
            ).fetchall()
    except sqlite3.Error:
        return {}

    return {str(key): str(value) for key, value in rows}


def resolve_https_runtime_settings(
    *,
    settings: PullboxSettings | object | None = None,
    db_values: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> HttpsRuntimeSettings:
    """Resolve HTTPS settings with env values taking precedence over DB values."""
    runtime = settings or get_settings()
    env = os.environ if environ is None else environ
    db = load_https_db_values(runtime) if db_values is None else dict(db_values)

    cert_root = Path(
        env.get(
            HTTPS_CERT_ROOT_ENV,
            str(_settings_path(runtime, "https_cert_root", _DEFAULT_CERT_ROOT)),
        )
    )

    if "PULLBOX_HTTPS_ENABLED" in env:
        enabled = _parse_bool(env.get("PULLBOX_HTTPS_ENABLED"))
    elif "https_enabled" in db:
        enabled = _parse_bool(db.get("https_enabled"))
    else:
        enabled = _settings_bool(runtime, "https_enabled", False)

    if "PULLBOX_HTTPS_CERT_PATH" in env:
        cert_path = str(env.get("PULLBOX_HTTPS_CERT_PATH", "")).strip()
    elif "https_cert_path" in db:
        cert_path = str(db.get("https_cert_path", "")).strip()
    else:
        cert_path = _settings_str(runtime, "https_cert_path", "")

    if "PULLBOX_HTTPS_KEY_PATH" in env:
        key_path = str(env.get("PULLBOX_HTTPS_KEY_PATH", "")).strip()
    elif "https_key_path" in db:
        key_path = str(db.get("https_key_path", "")).strip()
    else:
        key_path = _settings_str(runtime, "https_key_path", "")

    return HttpsRuntimeSettings(
        enabled=enabled,
        cert_path=cert_path,
        key_path=key_path,
        cert_root=cert_root,
    )


def https_runtime_override_keys(
    *,
    settings: PullboxSettings | object | None = None,
    environ: Mapping[str, str] | None = None,
) -> set[str]:
    """Return HTTPS config keys controlled by runtime/env settings."""
    runtime = settings or get_settings()
    env = os.environ if environ is None else environ
    keys: set[str] = set()

    for key, env_name in HTTPS_ENV_KEYS.items():
        if env_name in env:
            keys.add(key)
            continue

        if key == "https_enabled":
            bool_default = _parse_bool(PullboxSettings.model_fields[key].default)
            if _settings_bool(runtime, key, bool_default) != bool_default:
                keys.add(key)
            continue

        default_value = PullboxSettings.model_fields[key].default
        str_default = str(default_value or "").strip()
        if _settings_str(runtime, key, str_default) != str_default:
            keys.add(key)

    return keys


def https_runtime_config_values(
    *,
    settings: PullboxSettings | object | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return effective HTTPS runtime override values keyed by DB config key."""
    runtime = settings or get_settings()
    keys = https_runtime_override_keys(settings=runtime, environ=environ)
    values: dict[str, str] = {}
    if "https_enabled" in keys:
        values["https_enabled"] = (
            "true" if _settings_bool(runtime, "https_enabled", False) else "false"
        )
    if "https_cert_path" in keys:
        values["https_cert_path"] = _settings_str(runtime, "https_cert_path", "")
    if "https_key_path" in keys:
        values["https_key_path"] = _settings_str(runtime, "https_key_path", "")
    return values


def _is_relative_to(path: Path, root: Path) -> bool:
    """Return whether ``path`` resolves inside ``root``."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_readable_file(path: Path, label: str) -> None:
    if not path.exists():
        raise ValueError(f"HTTPS {label} file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"HTTPS {label} path must point to a file: {path}")
    try:
        with path.open("rb"):
            return
    except OSError as exc:
        raise ValueError(f"HTTPS {label} file is not readable: {path}") from exc


def _load_cert_chain(certfile: str, keyfile: str) -> None:
    """Load a certificate/key pair using Python's TLS stack."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)


def validate_https_runtime_settings(settings: HttpsRuntimeSettings) -> None:
    """Validate enabled HTTPS settings before saving or starting Pullbox."""
    if not settings.enabled:
        return

    cert_path_raw = settings.cert_path.strip()
    key_path_raw = settings.key_path.strip()
    if not cert_path_raw:
        raise ValueError("Enabled HTTPS requires a certificate file path.")
    if not key_path_raw:
        raise ValueError("Enabled HTTPS requires a private key file path.")

    cert_path = Path(cert_path_raw)
    key_path = Path(key_path_raw)
    if not cert_path.is_absolute():
        raise ValueError("HTTPS certificate file path must be absolute.")
    if not key_path.is_absolute():
        raise ValueError("HTTPS private key file path must be absolute.")

    cert_root = settings.cert_root.resolve(strict=False)
    if not cert_root.exists() or not cert_root.is_dir():
        raise ValueError(f"HTTPS certificate root does not exist: {cert_root}")

    resolved_cert_path = cert_path.resolve(strict=False)
    resolved_key_path = key_path.resolve(strict=False)
    if not _is_relative_to(resolved_cert_path, cert_root):
        raise ValueError(
            f"HTTPS certificate file must be inside certificate root {cert_root}: "
            f"{resolved_cert_path}"
        )
    if not _is_relative_to(resolved_key_path, cert_root):
        raise ValueError(
            f"HTTPS private key file must be inside certificate root {cert_root}: "
            f"{resolved_key_path}"
        )

    _require_readable_file(resolved_cert_path, "certificate")
    _require_readable_file(resolved_key_path, "private key")

    try:
        _load_cert_chain(str(resolved_cert_path), str(resolved_key_path))
    except Exception as exc:
        raise ValueError(f"HTTPS certificate/key pair is invalid: {exc}") from exc


def validate_https_config_values(
    *,
    enabled: str | bool | int | None,
    cert_path: str,
    key_path: str,
    cert_root: Path,
) -> None:
    """Validate raw DB/API values with the same rules used at startup."""
    validate_https_runtime_settings(
        HttpsRuntimeSettings(
            enabled=_parse_bool(enabled),
            cert_path=cert_path,
            key_path=key_path,
            cert_root=cert_root,
        )
    )


def uvicorn_ssl_kwargs(settings: HttpsRuntimeSettings) -> dict[str, Any]:
    """Return uvicorn SSL kwargs for enabled HTTPS."""
    if not settings.enabled:
        return {}
    return {
        "ssl_certfile": settings.cert_path,
        "ssl_keyfile": settings.key_path,
    }
