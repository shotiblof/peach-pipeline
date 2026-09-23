"""Apply pipeline SQLite schema (idempotent)."""
from __future__ import annotations

from pathlib import Path

from peach_db import SCHEMA_PATH, db_conn


def apply_migrations() -> None:
    if not SCHEMA_PATH.is_file():
        raise RuntimeError(f"missing schema: {SCHEMA_PATH}")
    with db_conn() as conn:
        # schema applied inside db_conn/_ensure_schema; force ebalka-only setting
        conn.execute(
            """
            INSERT INTO app_settings (key, value, description, updated_at)
            VALUES (
              'parser.primary_source',
              'ebalka',
              'Catalog source (ebalka only)',
              datetime('now')
            )
            ON CONFLICT (key) DO UPDATE SET
              value = 'ebalka',
              updated_at = datetime('now')
            """
        )
        conn.execute(
            """
            UPDATE videos
            SET status = 'skipped',
                error_message = 'skipped: hiden disabled (ebalka-only pipeline)',
                updated_at = datetime('now')
            WHERE status IN ('parsed', 'failed', 'uploading')
              AND id LIKE 'h%'
            """
        )
        row = conn.execute(
            "SELECT count(*) AS n FROM sqlite_master WHERE type='table'"
        ).fetchone()
        print(f"migrations: sqlite schema ready ({(row or {}).get('n', 0)} tables), source=ebalka")
        conn.commit()


if __name__ == "__main__":
    apply_migrations()
