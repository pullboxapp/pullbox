"""Regression tests for the one-time semantic series-type repair."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa

from pullbox.core.naming import classify_series_type, detect_issue_type_from_metadata_title

if TYPE_CHECKING:
    from types import ModuleType


def _load_migration() -> ModuleType:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic/versions/n9o0p1q2r345_repair_series_type_classification.py"
    )
    spec = importlib.util.spec_from_file_location("series_type_repair_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Spawn", "An ongoing series about Al Simmons."),
        ("True One-Shot", "A one-shot story about Gotham."),
        ("Crossed: Patient Zero Ashcan", "Limited edition ashcan preview."),
        ("Immortal Thor", "Series of trade paperbacks collecting Immortal Thor."),
        ("Batman", "Series of hardcovers/paperbacks collecting Batman."),
        ("Batman: Officer Down", "This collection includes Batman #587-590."),
        ("The Replacer", "Graphic novella."),
        ("Batman Special Edition", "A story about Gotham."),
    ],
)
def test_migration_classifier_snapshot_matches_release_classifier(
    title: str,
    description: str,
) -> None:
    migration = _load_migration()

    assert (
        migration._classify_series_type(title, description)
        == classify_series_type(
            title,
            description=description,
        ).upper()
    )


@pytest.mark.parametrize(
    "title",
    ["Volume 1", "HC/TPB", "Holiday Special", "One-Shot", "Ordinary issue title"],
)
def test_migration_issue_title_snapshot_matches_release_classifier(title: str) -> None:
    migration = _load_migration()

    assert migration._detect_issue_type_from_metadata_title(title) == (
        detect_issue_type_from_metadata_title(title).upper()
    )


def test_classification_repair_updates_only_heuristic_and_inherited_types(
    request: pytest.FixtureRequest,
) -> None:
    migration = _load_migration()
    engine = sa.create_engine("sqlite:///:memory:")
    request.addfinalizer(engine.dispose)
    metadata = sa.MetaData()
    series = sa.Table(
        "series",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("issue_count", sa.Integer, nullable=False),
        sa.Column("year_start", sa.Integer),
        sa.Column("series_type", sa.String, nullable=False),
        sa.Column("parent_series_id", sa.Integer),
    )
    issues = sa.Table(
        "issues",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("series_id", sa.Integer, nullable=False),
        sa.Column("title", sa.String),
        sa.Column("issue_type", sa.String, nullable=False),
        sa.Column("metadata_source", sa.String),
    )
    metadata.create_all(engine)

    officer_down_description = (
        "Officer Down crosses through the Batman family. " * 15
        + "This collection includes stories from Batman #587-590."
    )
    with engine.begin() as connection:
        connection.execute(
            series.insert(),
            [
                {
                    "id": 1,
                    "title": "Spawn",
                    "description": "An ongoing series about Al Simmons.",
                    "issue_count": 2,
                    "year_start": 1992,
                    "series_type": "SPECIAL",
                    "parent_series_id": 99,
                },
                {
                    "id": 2,
                    "title": "Batman: Officer Down",
                    "description": officer_down_description,
                    "issue_count": 1,
                    "year_start": 2001,
                    "series_type": "ONE_SHOT",
                    "parent_series_id": None,
                },
                {
                    "id": 3,
                    "title": "True One-Shot",
                    "description": "A one-shot story about Gotham.",
                    "issue_count": 1,
                    "year_start": 2000,
                    "series_type": "ONE_SHOT",
                    "parent_series_id": None,
                },
                {
                    "id": 4,
                    "title": "Imported Collection",
                    "description": "A story without format metadata.",
                    "issue_count": 1,
                    "year_start": 2020,
                    "series_type": "ONE_SHOT",
                    "parent_series_id": None,
                },
                {
                    "id": 5,
                    "title": "The Chair",
                    "description": None,
                    "issue_count": 1,
                    "year_start": 2008,
                    "series_type": "GRAPHIC_NOVEL",
                    "parent_series_id": None,
                },
                {
                    "id": 6,
                    "title": "Collected Stories",
                    "description": None,
                    "issue_count": 2,
                    "year_start": 2020,
                    "series_type": "TPB",
                    "parent_series_id": None,
                },
            ],
        )
        connection.execute(
            issues.insert(),
            [
                {
                    "id": 1,
                    "series_id": 1,
                    "title": "Questions",
                    "issue_type": "SPECIAL",
                    "metadata_source": "comicvine",
                },
                {
                    "id": 2,
                    "series_id": 1,
                    "title": "Holiday Special",
                    "issue_type": "SPECIAL",
                    "metadata_source": "comicvine",
                },
                {
                    "id": 3,
                    "series_id": 2,
                    "title": "Batman: Officer Down",
                    "issue_type": "ONE_SHOT",
                    "metadata_source": "comicvine",
                },
                {
                    "id": 4,
                    "series_id": 3,
                    "title": "One-Shot",
                    "issue_type": "ONE_SHOT",
                    "metadata_source": "comicvine",
                },
                {
                    "id": 5,
                    "series_id": 4,
                    "title": None,
                    "issue_type": "TPB",
                    "metadata_source": "provisional_import",
                },
                {
                    "id": 6,
                    "series_id": 5,
                    "title": None,
                    "issue_type": "GN",
                    "metadata_source": "comicvine",
                },
                {
                    "id": 7,
                    "series_id": 6,
                    "title": "Volume 1",
                    "issue_type": "TPB",
                    "metadata_source": "comicvine",
                },
                {
                    "id": 8,
                    "series_id": 6,
                    "title": "Volume 2",
                    "issue_type": "TPB",
                    "metadata_source": "comicvine",
                },
            ],
        )

        migration._repair_connection(connection)

        repaired_series = dict(
            connection.execute(sa.select(series.c.id, series.c.series_type)).all()
        )
        repaired_issues = dict(
            connection.execute(sa.select(issues.c.id, issues.c.issue_type)).all()
        )
        spawn_parent = connection.scalar(
            sa.select(series.c.parent_series_id).where(series.c.id == 1)
        )

    assert repaired_series == {
        1: "STANDARD",
        2: "VOLUME",
        3: "ONE_SHOT",
        4: "STANDARD",
        5: "GRAPHIC_NOVEL",
        6: "VOLUME",
    }
    assert repaired_issues == {
        1: "ISSUE",
        2: "SPECIAL",
        3: "VOLUME",
        4: "ONE_SHOT",
        5: "TPB",
        6: "GN",
        7: "VOLUME",
        8: "VOLUME",
    }
    assert spawn_parent is None
