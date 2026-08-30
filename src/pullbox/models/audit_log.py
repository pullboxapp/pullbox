"""Security audit log model — tracks security-relevant events."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - SQLAlchemy needs this at runtime
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from pullbox.models.base import Base, IdentityMixin, UTCDateTime


class AuditEventType(StrEnum):
    """Categories of auditable security events."""

    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGIN_RATE_LIMITED = "login_rate_limited"
    PASSWORD_CHANGED = "password_changed"
    USERNAME_CHANGED = "username_changed"
    SESSION_INVALIDATED = "session_invalidated"
    API_KEY_CREATED = "api_key_created"
    API_KEY_REVOKED = "api_key_revoked"
    API_RATE_LIMITED = "api_rate_limited"
    SECURITY_CONFIG_CHANGED = "security_config_changed"
    LOCAL_BYPASS_TOGGLED = "local_bypass_toggled"
    IMPORT_SAFETY_BULK_OVERRIDE = "import_safety_bulk_override"


class AuditLog(Base, IdentityMixin):
    """Persistent audit log entry for security events."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_event_type_timestamp", "event_type", "timestamp"),
        Index("ix_audit_logs_user_id_timestamp", "user_id", "timestamp"),
        Index("ix_audit_logs_timestamp_desc", "timestamp"),
    )

    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    timestamp: Mapped[datetime] = mapped_column(
        UTCDateTime, server_default=func.now(), nullable=False
    )
    source_ip: Mapped[str | None] = mapped_column(String(45))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    username: Mapped[str | None] = mapped_column(String(100))
    detail: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)
