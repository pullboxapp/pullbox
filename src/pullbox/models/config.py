"""System configuration key-value store ORM model."""

from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from pullbox.models.base import Base, UTCDateTime

DEFAULT_SYSTEM_CONFIG: dict[str, tuple[str, str]] = {
    # Application Identity
    "instance_name": ("Pullbox", "string"),
    "base_url": ("http://localhost:8585", "string"),
    "https_enabled": ("false", "bool"),
    "https_cert_path": ("", "string"),
    "https_key_path": ("", "string"),
    "usage_stats_consent": ("unknown", "string"),
    "usage_stats_instance_id": ("", "string"),
    # Search & Download
    "source_priority": ('["usenet", "torrent", "direct", "dc"]', "string"),
    "search_on_add_default": ("false", "bool"),
    "search_interval_hours": ("6", "int"),
    "auto_import_enabled": ("true", "bool"),
    "download_poll_interval_seconds": ("3", "int"),
    "process_completed_interval_seconds": ("300", "int"),
    "redownload_on_failure": ("true", "bool"),
    "max_download_retries": ("3", "int"),
    "stall_timeout_hours": ("1", "int"),
    # Metadata
    "metadata_refresh_days": ("7", "int"),
    "comicvine_rate_limit_per_second": ("1", "int"),
    "comicvine_api_key": ("", "secret"),
    # Library
    "comics_directory": ("", "string"),
    "library_permissions_enabled": ("false", "bool"),
    "library_permissions_folder_mode": ("755", "string"),
    "library_permissions_file_mode": ("644", "string"),
    "library_permissions_apply_to_created_folders": ("true", "bool"),
    "library_permissions_apply_to_materialized_files": ("true", "bool"),
    "library_permissions_hardlink_behavior": ("skip", "string"),
    "library_permissions_symlink_behavior": ("skip", "string"),
    # History & Cleanup
    "history_retention_days": ("90", "int"),
    "search_log_retention_days": ("7", "int"),
    "health_history_retention_days": ("1", "int"),
    "health_scheduler_interval_minutes": ("30", "int"),
    "health_database_interval_minutes": ("15", "int"),
    "health_filesystem_interval_minutes": ("15", "int"),
    "health_system_interval_minutes": ("15", "int"),
    "health_download_clients_interval_hours": ("4", "int"),
    "health_indexers_interval_hours": ("8", "int"),
    "health_comicvine_interval_hours": ("8", "int"),
    # Indexer Health
    "indexer_failure_threshold": ("3", "int"),
    # Quality Preferences (search scoring)
    "min_release_size_mb": ("30", "int"),
    "max_release_size_mb": ("2000", "int"),
    "min_quality_score": ("20", "int"),
    "preferred_format": ("cbz", "string"),
    # Naming — Series Folders
    "series_folder_template": ("{Series} ({Year})", "string"),
    # Naming — Comic Files
    "comic_file_template": ("{Series} ({Year}) #{Issue:03d}", "string"),
    "annual_file_template": ("{Series} ({Year}) Annual #{Issue:03d}", "string"),
    "non_standard_file_template": ("{Series} ({Year}) {Type} {Volume:02d} - {Title}", "string"),
    "single_non_standard_file_template": ("{Series} ({Year}) {Type} - {Title}", "string"),
    "rename_on_import": ("true", "bool"),
    "replace_illegal_characters": ("true", "bool"),
    "colon_replacement": ("dash", "string"),
    # Naming — Folder Management
    "create_empty_series_folders": ("false", "bool"),
    "delete_empty_folders": ("true", "bool"),
    # Post-Processing
    "post_processing_method": ("move", "string"),
    "torrent_import_strategy": ("standard", "string"),
    "convert_to_preferred_format_on_import": ("false", "bool"),
    "skip_existing_files": ("false", "bool"),
    "update_embedded_comicinfo_from_match_on_import": ("false", "bool"),
    # Logging
    "log_level": ("info", "string"),
    "log_size_limit_mb": ("1", "int"),
    "log_backup_count": ("5", "int"),
    # Backup
    "backup_interval_days": ("7", "int"),
    "backup_retention_days": ("28", "int"),
    # Search — Ignore Words
    "search_ignore_words": (
        "covers only,cover only,preview,sampler,ashcan,sketch,"
        "virgin,incentive,poster,print,blank cover",
        "string",
    ),
    # Scoring Weights (must sum to 100)
    "score_weight_priority": ("40", "int"),
    "score_weight_age": ("30", "int"),
    "score_weight_size": ("20", "int"),
    "score_weight_format": ("10", "int"),
    # Confidence Blend — quality vs match-confidence split (0-100)
    "score_confidence_blend": ("40", "int"),
    # Matching Thresholds
    "match_fuzzy_high_threshold": ("85", "int"),
    "match_fuzzy_low_threshold": ("70", "int"),
    "match_year_tolerance": ("1", "int"),
    # Torrent Seeder Scoring Tiers (thresholds for bonus points)
    "score_seeder_tier1": ("10", "int"),  # 1-N seeders → +4 points
    "score_seeder_tier2": ("30", "int"),  # N+1-M seeders → +8 points
    "score_seeder_tier3": ("50", "int"),  # M+1-P seeders → +12 points, P+ → +15
    # Phase E — Additional Scoring Factors
    "score_weight_grabs": ("0", "int"),
    "score_penalty_pack": ("-20", "int"),
    "preferred_language": ("en", "string"),
    "score_bonus_digital": ("10", "int"),
    "max_file_count": ("5", "int"),
    # Non-Standard Type Settings
    "search_type_thresholds": ("{}", "string"),
    "search_size_warn_issue_mb": ("750", "int"),
    "search_size_warn_collection_mb": ("50", "int"),
    "search_two_pass_enabled": ("true", "bool"),
    "search_intervention_expiry_days": ("10", "int"),
    # Security — File Safety
    "allowed_import_extensions": (".cbr,.cbz,.cb7,.cbt,.pdf,.epub", "string"),
    "block_dangerous_files": ("true", "bool"),
    "archive_size_limit_mb": ("2000", "int"),
    # Security — Authentication
    "local_auth_bypass_enabled": ("false", "bool"),
    "local_auth_bypass_addresses": ("127.0.0.1, ::1", "string"),
    "local_auth_bypass_username": ("", "string"),
    "session_lifetime_hours": ("24", "int"),
    # Utilities
    "utility_worker_count": ("4", "int"),
    "utility_trash_folder": ("", "string"),
    "utility_trash_retention_days": ("30", "int"),
    "utility_export_folder": ("", "string"),
    "utility_job_retention_days": ("30", "int"),
    "utility_log_level": ("info", "string"),
    # Display
    "display.timezone": ("browser", "string"),
    "display.date_format": ("MMM DD, YYYY", "string"),
    "display.time_format": ("24h", "string"),
    "display.show_seconds": ("false", "bool"),
    "display.show_timezone": ("true", "bool"),
    "display.show_ampm": ("true", "bool"),
    # Blocklist
    "blocklist.release_groups": ("", "string"),
    "blocklist.expiry_days": ("90", "int"),
    "blocklist.auto_add_on_failure": ("true", "bool"),
}


class SystemConfig(Base):
    """Key-value store for system configuration."""

    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    value_type: Mapped[str] = mapped_column(String(20), default="string")
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), onupdate=func.now()
    )
