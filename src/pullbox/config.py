"""Bootstrap/runtime settings loaded from environment variables.

``PullboxSettings`` is the environment-backed bootstrap layer used for runtime
paths, database wiring, bind host/port, and other startup-only concerns.

The active feature-level configuration model is:
1. Environment/bootstrap settings via this module
2. Persistent host secret in ``config.xml``
3. Editable application settings in ``system_config``

``.env`` exists as a local-development convenience only. Feature code should
prefer the shared resolver in ``pullbox.core.config_resolver`` rather than
reading from ``PullboxSettings`` directly unless it truly needs bootstrap
runtime values.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PullboxSettings(BaseSettings):
    """Application configuration loaded from environment variables.

    All settings use the PULLBOX_ prefix. For example, the ``port`` field
    is set via the ``PULLBOX_PORT`` environment variable.
    """

    model_config = SettingsConfigDict(
        env_prefix="PULLBOX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ── Runtime / Bootstrap ───────────────────────────────────────────
    instance_name: str = "Pullbox"
    # Docker deployments need the app to bind the container interface by default.
    bind_address: str = "0.0.0.0"  # nosec B104
    base_url: str = "http://localhost:8585"
    port: int = 8585
    https_enabled: bool = False
    https_cert_path: str = ""
    https_key_path: str = ""
    https_cert_root: Path = Path("/config/certs")
    log_level: str = "INFO"
    log_size_limit_mb: int = 1  # Max log file size before rotation
    log_backup_count: int = 5  # Number of rotated log files to keep
    debug: bool = False
    startup_update_check_enabled: bool = True
    airdcpp_enabled: bool = False

    # ── Database ───────────────────────────────────────────────────────
    db_url: str = "sqlite+aiosqlite:////data/pullbox.db"
    sqlite_journal_mode: str = "WAL"

    # ── Security ───────────────────────────────────────────────────────
    secret_key: str = ""  # Override only — config.xml auto-generates on first run
    session_lifetime_hours: int = 24
    local_addresses: str = ""  # Legacy bootstrap setting — DB config is authoritative
    trusted_proxies: str = ""  # Comma-separated IPs of trusted reverse proxies

    # ── Rate Limiting ───────────────────────────────────────────────────
    rate_limit_enabled: bool = True
    rate_limit_tier1: int = 60  # Expensive ops (external API, backup, scan) per min per IP
    rate_limit_tier2: int = 120  # Write ops (POST/PUT/PATCH/DELETE) per min per IP
    rate_limit_tier3: int = 300  # Read ops (GET) per min per IP

    # ── Paths ──────────────────────────────────────────────────────────
    data_dir: Path = Path("/data")
    covers_dir: Path = Path("/comics/.covers")
    logs_dir: Path = Path("/data/logs")
    temp_dir: Path = Path("/data/tmp")
    backup_dir: Path = Path("/data/backups")

    # ── Embedded Reader Resource Budgets ─────────────────────────────
    reader_enabled: bool = True
    reader_cache_max_mb: int = 512
    reader_open_source_cache_size: int = 8
    reader_max_entries: int = 10_000
    reader_max_page_mb: int = 128
    reader_max_expanded_mb: int = 4096
    reader_max_compression_ratio: int = 250
    reader_compression_ratio_min_mb: int = 4
    reader_max_image_pixels: int = 80_000_000
    reader_max_image_entries: int = 5_000
    reader_max_member_path_chars: int = 1_024
    reader_max_member_depth: int = 32
    reader_max_rendition_width: int = 2_560
    reader_max_rendition_height: int = 4_096
    reader_pdf_dpi: int = 160
    reader_render_timeout_seconds: int = 30
    reader_worker_count: int = 2
    reader_worker_wait_seconds: float = 2.0

    # ── Backup ─────────────────────────────────────────────────────────
    backup_interval_days: int = 7
    backup_retention_days: int = 28

    # ── ComicVine ──────────────────────────────────────────────────────
    comicvine_api_key: str = ""
    comicvine_rate_limit: int = 200  # Requests per hour

    # ── Import Debug ───────────────────────────────────────────────────
    import_debug_slow_mode: bool = False
    import_debug_phase_delay_seconds: float = 1.25
    import_debug_item_delay_seconds: float = 0.4
    import_file_worker_count: int = 2

    # ── Scheduler ──────────────────────────────────────────────────────
    search_interval_hours: int = 6
    scan_interval_hours: int = 24
    download_poll_seconds: int = 3
    process_completed_interval_seconds: int = 300
    health_check_interval_minutes: int = 5
    health_scheduler_interval_minutes: int = 30
    health_database_interval_minutes: int = 15
    health_filesystem_interval_minutes: int = 15
    health_system_interval_minutes: int = 15
    health_download_clients_interval_hours: int = 4
    health_indexers_interval_hours: int = 8
    health_comicvine_interval_hours: int = 8
    health_history_retention_days: int = 1
    metadata_refresh_days: int = 30
    history_retention_days: int = 90
    pullbox_data_api_base_url: str = Field(
        default="https://api.pullbox.app",
        validation_alias="PULLBOX_DATA_API_BASE_URL",
    )

    # ── Library ────────────────────────────────────────────────────────
    library_root: Path = Path("/comics")
    naming_series_format: str = "{series} ({year})"
    naming_issue_format: str = "{series} ({year}) #{issue:03d}"


@lru_cache(maxsize=1)
def get_settings() -> PullboxSettings:
    """Return the application settings singleton.

    Uses ``@lru_cache`` so the settings object is created once and reused
    for all subsequent calls. In production, config.xml provides the
    secret key — the env var is optional.
    """
    return PullboxSettings()
