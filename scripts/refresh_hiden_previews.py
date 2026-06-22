#!/usr/bin/env python3
"""Refresh hiden preview_path (and poster) for existing videos via hiden API."""
from __future__ import annotations

import os
from pathlib import Path

_dotenv = Path(__file__).resolve().parents[1].joinpath(".env")
if _dotenv.exists():
    for line in _dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k, v)

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

import httpx

from hiden_parser import (
    HEADERS,
    _fetch,
    _origin,
    fetch_preview_clip_api,
    parse_poster,
    resolve_preview_clip,
)
from peach_db import db_conn

LIMIT = int(os.environ.get("REFRESH_LIMIT", "50"))


def main() -> None:
    updated = 0
    with db_conn() as conn:
        origin = _origin(conn)
        rows = conn.execute(
            """
            SELECT id, video_path
            FROM videos
            WHERE id LIKE 'h%%'
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (LIMIT,),
        ).fetchall()

        with httpx.Client(headers=HEADERS, timeout=60.0, follow_redirects=True) as client:
            for row in rows:
                vid = str(row["id"])
                numeric_id = vid[1:]
                path = str(row["video_path"])
                page_url = path if path.startswith("http") else f"{origin}{path}"
                html = _fetch(client, page_url)
                poster = parse_poster(html)
                clip = resolve_preview_clip(client, origin, numeric_id, html)
                if not clip and not poster:
                    print(f"skip {vid}: no media")
                    continue
                conn.execute(
                    """
                    UPDATE videos
                    SET preview_path = COALESCE(%s, preview_path),
                        poster_path = COALESCE(NULLIF(%s, ''), poster_path),
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (clip, poster, vid),
                )
                updated += 1
                print(f"updated {vid}: clip={clip or '-'} poster={poster or '-'}")

        conn.commit()
    print(f"done: {updated} video(s)")


if __name__ == "__main__":
    main()
