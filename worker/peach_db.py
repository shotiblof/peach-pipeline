"""Peach — shared DB access for parser and uploader."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row


def get_database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    return url


@contextmanager
def db_conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(get_database_url(), row_factory=dict_row) as conn:
        yield conn


def get_setting(conn: psycopg.Connection, key: str, default: str = "") -> str:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = %s",
        (key,),
    ).fetchone()
    if not row:
        return default
    return str(row["value"] or default)


def get_upload_account(conn: psycopg.Connection, provider: str) -> dict[str, Any] | None:
    return conn.execute(
        """
        SELECT id, provider, name, login, secret, metadata
        FROM upload_accounts
        WHERE provider = %s AND is_enabled = true
        ORDER BY priority ASC, id ASC
        LIMIT 1
        """,
        (provider,),
    ).fetchone()


def abs_url(origin: str, path: str) -> str:
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return origin.rstrip("/") + "/" + path.lstrip("/")


def to_storage_path(url: str, origin: str) -> str:
    if url.startswith("/"):
        return url
    parsed = urlparse(url)
    origin_host = urlparse(origin).netloc
    if parsed.netloc == origin_host:
        query = f"?{parsed.query}" if parsed.query else ""
        return parsed.path + query
    return url


def tags_to_hashtags(tags: list[str] | Any, limit: int = 5) -> str:
    if not isinstance(tags, list):
        return ""
    parts: list[str] = []
    for tag in tags[:limit]:
        if not isinstance(tag, str) or not tag.strip():
            continue
        slug = tag.strip().replace(" ", "_")
        parts.append(f"#{slug}")
    return " ".join(parts)
