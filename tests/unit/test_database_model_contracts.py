"""Architectural guardrails for ORM model conventions."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import DateTime, Integer
from sqlalchemy import Enum as SQLAlchemyEnum

from pullbox.models import Base
from pullbox.models.base import UTCDateTime
from pullbox.models.import_job import ImportJobStatus

MIGRATION_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

IDENTITY_EXCEPTIONS = {
    "IssueCreator",  # association table with composite primary key
    "IssueStoryArc",  # association table with composite primary key
    "ScheduledTaskStat",  # keyed by stable scheduler task id
    "SystemConfig",  # keyed by configuration key
    "UtilityJob",  # string id generated before enqueue persistence
    "UtilityJobItem",  # string id generated before worker item persistence
}

TIMESTAMP_EXCEPTIONS = {
    "AuditLog",  # immutable security event time is stored in timestamp
    "HealthCheckResult",  # health sample time is stored in checked_at
    "ImportJobLog",  # log event time is stored in logged_at
    "IssueCreator",  # association table
    "IssueStoryArc",  # association table
    "SystemConfig",  # key-value config tracks updated_at only
    "UtilityJob",  # utility schema stores lifecycle timestamps as TEXT
    "UtilityJobItem",  # utility schema stores item lifecycle timestamps as TEXT
    "UtilityJobLog",  # utility schema stores event time in timestamp TEXT
}


def _mapped_classes() -> list[type[object]]:
    return sorted(
        (
            mapper.class_
            for mapper in Base.registry.mappers
            if mapper.class_.__module__.startswith("pullbox.")
        ),
        key=lambda model: model.__name__,
    )


def test_ordinary_models_use_standard_identity_column() -> None:
    """Ordinary entity tables should use the shared integer identity contract."""
    offenders: list[str] = []
    for model in _mapped_classes():
        if model.__name__ in IDENTITY_EXCEPTIONS:
            continue
        table = model.__table__
        id_column = table.columns.get("id")
        if (
            id_column is None
            or not id_column.primary_key
            or not isinstance(id_column.type, Integer)
        ):
            offenders.append(model.__name__)

    assert offenders == []


def test_ordinary_models_use_standard_timestamp_columns() -> None:
    """Ordinary entity tables should expose created_at and updated_at."""
    offenders: list[str] = []
    for model in _mapped_classes():
        if model.__name__ in TIMESTAMP_EXCEPTIONS:
            continue
        table = model.__table__
        if "created_at" not in table.columns or "updated_at" not in table.columns:
            offenders.append(model.__name__)

    assert offenders == []


def test_persisted_datetime_columns_use_utc_type() -> None:
    """ORM DateTime columns should normalize through the shared UTC type."""
    offenders: list[str] = []
    for model in _mapped_classes():
        for column in model.__table__.columns:
            if isinstance(column.type, DateTime) and not isinstance(column.type, UTCDateTime):
                offenders.append(f"{model.__name__}.{column.name}")

    assert offenders == []


def test_runtime_enum_columns_use_python_enum_classes() -> None:
    """Runtime enum columns should store values through typed Python enum classes."""
    offenders: list[str] = []
    for model in _mapped_classes():
        for column in model.__table__.columns:
            if isinstance(column.type, SQLAlchemyEnum) and column.type.enum_class is None:
                offenders.append(f"{model.__name__}.{column.name}")

    assert offenders == []


def test_import_job_status_enum_values_are_covered_by_migrations() -> None:
    """PostgreSQL native enum migrations must include every ImportJobStatus member."""
    created_statuses = set()
    added_statuses = set()
    for path in sorted(MIGRATION_DIR.glob("*.py")):
        migration_text = path.read_text(encoding="utf-8")
        for enum_definition in re.findall(
            r"sa\.Enum\((.*?)name=\"importjobstatus\"",
            migration_text,
            flags=re.DOTALL,
        ):
            created_statuses.update(re.findall(r'"([A-Z_]+)"', enum_definition))
        added_statuses.update(
            re.findall(
                r"ALTER TYPE importjobstatus ADD VALUE IF NOT EXISTS '([^']+)'",
                migration_text,
            )
        )

    migration_statuses = created_statuses | added_statuses
    missing_statuses = {member.name for member in ImportJobStatus} - migration_statuses

    assert missing_statuses == set()


def test_foreign_keys_define_delete_behavior() -> None:
    """FKs should make delete behavior explicit instead of relying on DB defaults."""
    offenders: list[str] = []
    for model in _mapped_classes():
        for column in model.__table__.columns:
            for foreign_key in column.foreign_keys:
                if foreign_key.ondelete is None:
                    offenders.append(f"{model.__name__}.{column.name}")

    assert offenders == []


def test_set_null_foreign_keys_are_nullable() -> None:
    """SET NULL constraints only work safely when the FK column is nullable."""
    offenders: list[str] = []
    for model in _mapped_classes():
        for column in model.__table__.columns:
            for foreign_key in column.foreign_keys:
                if str(foreign_key.ondelete).upper() == "SET NULL" and not column.nullable:
                    offenders.append(f"{model.__name__}.{column.name}")

    assert offenders == []
