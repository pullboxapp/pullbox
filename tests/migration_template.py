"""Real Alembic head snapshots for independent post-migration schema checks.

Never use this helper for tests of the migration path itself. The template is
private to one pytest worker/invocation; each caller owns a new database file.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command

if TYPE_CHECKING:
    from pathlib import Path


class MigratedHeadTemplate:
    """Build with real migrations once, then copy a consistent SQLite snapshot."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "head.db"
        self._ready = False

    def copy_to(self, destination: Path, config: Config) -> None:
        """Populate only a new file; never overwrite or share caller state."""
        destination.touch(exist_ok=False)
        if not self._ready:
            if self.path.exists():
                raise RuntimeError("Previous migration template build did not complete")
            template_config = Config(config.config_file_name)
            template_config.set_main_option(
                "script_location", config.get_main_option("script_location")
            )
            url = f"sqlite+aiosqlite:///{self.path}"
            template_config.set_main_option("sqlalchemy.url", url)
            # Alembic's environment reads this override. Restore the caller's
            # test database immediately; no engine or connection is retained.
            with pytest.MonkeyPatch.context() as patch:
                patch.setenv("PULLBOX_DB_URL", url)
                command.upgrade(template_config, "head")
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as source:
                version = source.execute("SELECT version_num FROM alembic_version").fetchone()
            assert version == (ScriptDirectory.from_config(template_config).get_current_head(),)
            self._ready = True

        # SQLite backup includes committed WAL pages. Copying only the main
        # file would not be safe if a journal still contains committed data.
        with (
            closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(destination)) as target,
        ):
            source.backup(target)
