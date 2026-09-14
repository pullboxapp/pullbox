"""Sanitized SQLite snapshot helpers for diagnostic packages."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import structlog

from pullbox.services.diagnostic_sanitizer import REDACTED, redact_value

logger = structlog.get_logger(__name__)


def create_sanitized_db_copy(db_path: Path) -> bytes | None:
    """Create a sanitized copy of the SQLite database."""
    if not db_path.is_file():
        return None

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    try:
        os.close(tmp_fd)

        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(tmp_path)
        try:
            src.backup(dst)
            src.close()

            tables = {
                str(row[0])
                for row in dst.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                ).fetchall()
            }
            if "audit_logs" in tables:
                # Keep operational evidence while removing its private account link.
                dst.execute("UPDATE audit_logs SET user_id = NULL")
            if "issue_reader_states" in tables:
                # Per-user reading state has no meaning once users are removed.
                dst.execute("DELETE FROM issue_reader_states")
            dst.execute("DELETE FROM users")
            dst.execute("DELETE FROM api_keys")

            rows = dst.execute("SELECT key, value FROM system_config").fetchall()
            for key, value in rows:
                redacted = redact_value(key, value)
                if redacted != value:
                    dst.execute(
                        "UPDATE system_config SET value = ? WHERE key = ?",
                        (redacted, key),
                    )

            dst.execute(
                "UPDATE download_client_configs SET api_key = ?, password = ? "
                "WHERE api_key IS NOT NULL OR password IS NOT NULL",
                (REDACTED, REDACTED),
            )

            dst.execute(
                "UPDATE indexer_configs SET api_key = ?",
                (REDACTED,),
            )

            dst.commit()
            dst.execute("VACUUM")
            dst.close()
        except Exception:
            src.close()
            dst.close()
            raise

        return Path(tmp_path).read_bytes()
    except Exception:
        logger.warning("diagnostic_db_copy_failed", exc_info=True)
        return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)
