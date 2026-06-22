"""Refresh or skip videos with stale ebalka/hiden preview URLs."""
from __future__ import annotations

import time
from typing import Any

import httpx

from ebalka_parser import HEADERS, _fetch as fetch_ebalka_html, extract_preview_from_video_page
from hiden_parser import (
    fetch_preview_clip_api,
    is_hiden_video_id,
    _fetch as fetch_hiden_html,
    extract_preview_clip,
    resolve_preview_clip,
)
from peach_db import abs_url, db_conn, get_setting

HTTP_TIMEOUT = httpx.Timeout(30.0, read=120.0)
PERMANENT_FAIL_MARKERS = (
    "too small",
    "expired",
    "20 files",
    "too fast",
    "description_invalid",
    "no preview_path",
)


def is_permanent_upload_error(message: str | None) -> bool:
    text = (message or "").lower()
    return any(marker in text for marker in PERMANENT_FAIL_MARKERS)


def _refresh_row_preview(
    conn,
    row: dict[str, Any],
    *,
    client: httpx.Client,
) -> str | None:
    vid = str(row["id"])
    if is_hiden_video_id(vid):
        origin = get_setting(conn, "hiden.source_origin", "https://hiden.live").rstrip("/")
        numeric_id = vid[1:] if vid.startswith("h") else vid
        clip = fetch_preview_clip_api(client, origin, numeric_id)
        if not clip:
            video_path = str(row.get("video_path") or "").strip()
            if video_path:
                html = fetch_hiden_html(client, abs_url(origin, video_path))
                clip = resolve_preview_clip(client, origin, numeric_id, html)
        if not clip:
            return None
        preview_path = clip if clip.startswith("http") else clip
    else:
        origin = get_setting(conn, "ebalka.source_origin", "https://a.ebalka.love").rstrip("/")
        video_path = str(row.get("video_path") or "").strip()
        if not video_path:
            return None
        try:
            html = fetch_ebalka_html(client, abs_url(origin, video_path))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 410):
                return None
            raise
        preview_path = extract_preview_from_video_page(html, origin)
        if not preview_path:
            return None

    conn.execute(
        """
        UPDATE videos
        SET preview_path = %s,
            error_message = NULL,
            updated_at = now()
        WHERE id = %s
        """,
        (preview_path, vid),
    )
    return preview_path


def purge_stale_previews(*, limit: int = 250, delay_sec: float = 0.35) -> dict[str, int]:
    """Skip dead rows; refresh preview_path for parsed/failed with stale URLs."""
    stats = {"skipped": 0, "refreshed": 0, "unchanged": 0, "scanned": 0}

    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, video_path, preview_path, status, error_message
            FROM videos
            WHERE status IN ('parsed', 'failed')
              AND (
                error_message ILIKE '%%too small%%'
                OR error_message ILIKE '%%expired%%'
                OR error_message ILIKE '%%20 files%%'
                OR error_message ILIKE '%%too fast%%'
                OR error_message ILIKE '%%namevids%%'
                OR (status = 'parsed' AND error_message IS NOT NULL AND error_message <> '')
              )
            ORDER BY
              CASE WHEN error_message ILIKE '%%too small%%' THEN 0 ELSE 1 END,
              updated_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()

        with httpx.Client(headers=HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            for row in rows:
                stats["scanned"] += 1
                vid = str(row["id"])
                err = str(row.get("error_message") or "")
                try:
                    preview = _refresh_row_preview(conn, dict(row), client=client)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (404, 410):
                        preview = None
                    else:
                        print(f"preview_refresh: {vid} fetch error {exc}")
                        stats["unchanged"] += 1
                        time.sleep(delay_sec)
                        continue
                except Exception as exc:
                    print(f"preview_refresh: {vid} error {exc}")
                    stats["unchanged"] += 1
                    time.sleep(delay_sec)
                    continue

                if preview:
                    conn.execute(
                        """
                        UPDATE videos
                        SET status = 'parsed',
                            error_message = NULL,
                            updated_at = now()
                        WHERE id = %s
                          AND status = 'failed'
                        """,
                        (vid,),
                    )
                    stats["refreshed"] += 1
                    print(f"preview_refresh: {vid} refreshed")
                else:
                    conn.execute(
                        """
                        UPDATE videos
                        SET status = 'skipped',
                            error_message = %s,
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (
                            err[:200]
                            if err
                            else "preview URL expired — source page has no preview",
                            vid,
                        ),
                    )
                    stats["skipped"] += 1
                    print(f"preview_refresh: {vid} skipped (no fresh preview)")

                time.sleep(delay_sec)

        conn.commit()

    print(f"preview_refresh: {stats}")
    return stats
