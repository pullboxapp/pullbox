"""Durable progress projection shared by user-visible background operations."""

from __future__ import annotations

import enum
from datetime import datetime  # noqa: TC003 - SQLAlchemy needs this at runtime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SQLAlchemyEnum
from sqlalchemy.orm import Mapped, mapped_column

from pullbox.models.base import Base, IdentityMixin, TimestampMixin, UTCDateTime


def _enum_values(enum_class: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_class]


def _enum_type(enum_class: type[enum.Enum], name: str) -> SQLAlchemyEnum:
    return SQLAlchemyEnum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=_enum_values,
    )


class OperationProgressType(enum.StrEnum):
    """Background operation families rendered by the shared progress UI."""

    IMPORT = "import"
    DOWNLOAD = "download"
    POST_PROCESSING = "post_processing"
    ISSUE_IMPORT = "issue_import"
    ORPHAN_RECOVERY = "orphan_recovery"
    UTILITY = "utility"


class OperationProgressState(enum.StrEnum):
    """Small shared lifecycle vocabulary for progress projection rows."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OperationProgressState.COMPLETED,
            OperationProgressState.FAILED,
            OperationProgressState.CANCELLED,
        }


class OperationProgressVisibility(enum.StrEnum):
    """How strongly an operation should be promoted in the app shell."""

    PROMINENT = "prominent"
    PASSIVE = "passive"
    QUIET = "quiet"


class OperationProgressTone(enum.StrEnum):
    """Semantic visual treatment for an operation."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"


class OperationProgress(Base, IdentityMixin, TimestampMixin):
    """Latest durable, presentation-ready snapshot for one background operation."""

    __tablename__ = "operation_progress"
    __table_args__ = (
        UniqueConstraint(
            "operation_type",
            "operation_key",
            name="uq_operation_progress_identity",
        ),
        CheckConstraint("revision >= 0", name="ck_operation_progress_revision_nonnegative"),
        CheckConstraint(
            "overall_percent IS NULL OR (overall_percent >= 0 AND overall_percent <= 100)",
            name="ck_operation_progress_overall_percent",
        ),
        CheckConstraint(
            "item_percent IS NULL OR (item_percent >= 0 AND item_percent <= 100)",
            name="ck_operation_progress_item_percent",
        ),
        CheckConstraint(
            "overall_current IS NULL OR overall_current >= 0",
            name="ck_operation_progress_overall_current",
        ),
        CheckConstraint(
            "overall_total IS NULL OR overall_total >= 0",
            name="ck_operation_progress_overall_total",
        ),
        CheckConstraint(
            "item_current IS NULL OR item_current >= 0",
            name="ck_operation_progress_item_current",
        ),
        CheckConstraint(
            "item_total IS NULL OR item_total >= 0",
            name="ck_operation_progress_item_total",
        ),
        Index(
            "ix_operation_progress_activity",
            "visibility",
            "state",
            "attention_required",
            "updated_at",
        ),
        Index("ix_operation_progress_group", "group_key", "updated_at"),
    )

    operation_type: Mapped[OperationProgressType] = mapped_column(
        _enum_type(OperationProgressType, "operationprogresstype"),
        nullable=False,
    )
    operation_key: Mapped[str] = mapped_column(String(160), nullable=False)
    group_key: Mapped[str | None] = mapped_column(String(160))
    state: Mapped[OperationProgressState] = mapped_column(
        _enum_type(OperationProgressState, "operationprogressstate"),
        nullable=False,
    )
    phase: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    source_label: Mapped[str | None] = mapped_column(String(255))
    detail_url: Mapped[str | None] = mapped_column(String(1000))
    visibility: Mapped[OperationProgressVisibility] = mapped_column(
        _enum_type(OperationProgressVisibility, "operationprogressvisibility"),
        default=OperationProgressVisibility.PROMINENT,
        server_default=OperationProgressVisibility.PROMINENT.value,
        nullable=False,
    )
    tone: Mapped[OperationProgressTone] = mapped_column(
        _enum_type(OperationProgressTone, "operationprogresstone"),
        default=OperationProgressTone.INFO,
        server_default=OperationProgressTone.INFO.value,
        nullable=False,
    )
    attention_required: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
        nullable=False,
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    overall_current: Mapped[int | None] = mapped_column(BigInteger)
    overall_total: Mapped[int | None] = mapped_column(BigInteger)
    overall_percent: Mapped[float | None] = mapped_column(Float)
    overall_unit: Mapped[str | None] = mapped_column(String(40))
    overall_indeterminate: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
        nullable=False,
    )

    item_key: Mapped[str | None] = mapped_column(String(255))
    item_label: Mapped[str | None] = mapped_column(String(500))
    item_phase: Mapped[str | None] = mapped_column(String(100))
    item_message: Mapped[str | None] = mapped_column(Text)
    item_current: Mapped[int | None] = mapped_column(BigInteger)
    item_total: Mapped[int | None] = mapped_column(BigInteger)
    item_percent: Mapped[float | None] = mapped_column(Float)
    item_unit: Mapped[str | None] = mapped_column(String(40))
    item_indeterminate: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
        nullable=False,
    )

    rate: Mapped[float | None] = mapped_column(Float)
    rate_unit: Mapped[str | None] = mapped_column(String(40))
    eta_seconds: Mapped[int | None] = mapped_column(Integer)
    detail_snapshot: Mapped[dict[str, object]] = mapped_column(
        JSON,
        default=dict,
        server_default="{}",
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_event_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
