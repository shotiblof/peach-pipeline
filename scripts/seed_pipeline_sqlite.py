#!/usr/bin/env python3
"""Seed pipeline SQLite (Turso or GitHub-hosted) with namevids account + settings.

Tries Neon export when DATABASE_URL still works; otherwise uses .env NAMEVIDS_*.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT / "scripts"))

from load_env import load_dotenv  # noqa: E402

load_dotenv()

from namevids_client import generate_fa, resolve_user_agent  # noqa: E402
from peach_db import db_conn  # noqa: E402


def _try_neon_snapshot() -> dict | None:
    if (os.environ.get("SEED_SKIP_NEON") or "").strip() in ("1", "true", "yes"):
        return None
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url or url.startswith("libsql:") or url.endswith(".db"):
        return None
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        return None
    try:
        with psycopg.connect(
            url,
            row_factory=dict_row,
            connect_timeout=5,
            options="-c statement_timeout=5000",
        ) as conn:
            acc = conn.execute(
                """
                SELECT login, secret, metadata, name, priority
                FROM upload_accounts
                WHERE provider = 'namevids' AND is_enabled = true
                ORDER BY priority ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
            settings = conn.execute(
                "SELECT key, value FROM app_settings WHERE key LIKE %s OR key LIKE %s OR key LIKE %s",
                ("namevids.%", "ebalka.%", "hiden.%"),
            ).fetchall()
            parsed = conn.execute(
                """
                SELECT id, video_path, preview_path, poster_path, duration_seconds,
                       title_ru, description_ru, title_en, description_en,
                       category, category_en, tags, tags_en, status, error_message,
                       parsed_at, updated_at
                FROM videos
                WHERE status = 'parsed'
                ORDER BY parsed_at DESC
                LIMIT 200
                """
            ).fetchall()
            return {
                "account": dict(acc) if acc else None,
                "settings": [dict(r) for r in settings],
                "parsed": [dict(r) for r in parsed],
            }
    except Exception as exc:
        print(f"seed: neon export skipped: {exc}")
        return None


def _upsert_account(conn, login: str, password: str, metadata: dict, name: str = "main") -> None:
    conn.execute(
        """
        INSERT INTO upload_accounts (provider, name, login, secret, is_enabled, priority, metadata, updated_at)
        VALUES ('namevids', ?, ?, ?, 1, 10, ?, datetime('now'))
        ON CONFLICT (provider, name) DO UPDATE SET
          login = excluded.login,
          secret = excluded.secret,
          is_enabled = 1,
          metadata = excluded.metadata,
          updated_at = datetime('now')
        """,
        (name, login, password, json.dumps(metadata, ensure_ascii=False)),
    )


def main() -> None:
    snap = _try_neon_snapshot()

    login = (os.environ.get("NAMEVIDS_LOGIN") or "").strip()
    password = (os.environ.get("NAMEVIDS_PASSWORD") or "").strip()
    metadata: dict = {"fa_seed": secrets.token_hex(16)}
    metadata["user_agent"] = resolve_user_agent(metadata, login_name=login or "pipeline")

    with db_conn() as conn:
        if snap and snap.get("account"):
            acc = snap["account"]
            login = str(acc.get("login") or login)
            password = str(acc.get("secret") or password)
            meta = acc.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            if isinstance(meta, dict) and meta:
                metadata = meta
            print(f"seed: imported namevids account {login!r} from neon")
        elif login and password:
            print(f"seed: using NAMEVIDS_* from .env ({login!r})")
        else:
            raise SystemExit("No namevids credentials (Neon down and NAMEVIDS_* missing)")

        _upsert_account(conn, login, password, metadata)

        # settings
        defaults = {
            "namevids.caption_link": os.environ.get(
                "NAMEVIDS_CAPTION_LINK", "https://telegram.dog/jesovixxx"
            ),
            "ebalka.source_origin": os.environ.get("EBALKA_SOURCE_ORIGIN", "https://a.ebalka.love"),
            "parser.primary_source": "ebalka",
        }
        # Force ebalka-only even if Neon/old settings said hiden.
        defaults["parser.primary_source"] = "ebalka"
        if snap and snap.get("settings"):
            for row in snap["settings"]:
                defaults[str(row["key"])] = str(row["value"] or "")
        for key, value in defaults.items():
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')
                """,
                (key, value),
            )

        # Drop leftover hiden queue items — pipeline is ebalka-only.
        skipped = conn.execute(
            """
            UPDATE videos
            SET status = 'skipped',
                error_message = 'skipped: hiden disabled (ebalka-only pipeline)',
                updated_at = datetime('now')
            WHERE status IN ('parsed', 'failed', 'uploading')
              AND id LIKE 'h%'
            """
        )
        print(f"seed: skipped leftover hiden queue rows")

        imported = 0
        if snap and snap.get("parsed"):
            for row in snap["parsed"]:
                tags = row.get("tags")
                tags_en = row.get("tags_en")
                if not isinstance(tags, str):
                    tags = json.dumps(tags or [], ensure_ascii=False)
                if not isinstance(tags_en, str):
                    tags_en = json.dumps(tags_en or [], ensure_ascii=False)
                parsed_at = str(row["parsed_at"]) if row.get("parsed_at") else None
                conn.execute(
                    """
                    INSERT INTO videos (
                      id, video_path, preview_path, poster_path, duration_seconds,
                      title_ru, description_ru, title_en, description_en,
                      category, category_en, tags, tags_en, status, error_message,
                      parsed_at, updated_at
                    ) VALUES (
                      ?,?,?,?,?,?,?,?,?,?,?,?,?, 'parsed', ?, ?, datetime('now')
                    )
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        row["id"],
                        row["video_path"],
                        row.get("preview_path"),
                        row.get("poster_path"),
                        row.get("duration_seconds"),
                        row.get("title_ru") or "",
                        row.get("description_ru") or "",
                        row.get("title_en") or "",
                        row.get("description_en") or "",
                        row.get("category") or "",
                        row.get("category_en") or "",
                        tags,
                        tags_en,
                        row.get("error_message"),
                        parsed_at,
                    ),
                )
                imported += 1

        conn.commit()
        parsed_n = conn.execute(
            "SELECT count(*) AS n FROM videos WHERE status = 'parsed'"
        ).fetchone()
        print(f"seed: parsed queue={(parsed_n or {}).get('n')} (imported {imported})")
        print("seed: OK")


if __name__ == "__main__":
    main()
