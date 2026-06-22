"""Re-upload DoodStream videos before 60-day free-tier deletion."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import httpx

from doodstream_client import DoodStreamError, get_file_status, log_account_info, upload_file
from ebalka_parser import HEADERS, extract_mp4_url, _fetch as fetch_html
from hiden_parser import extract_hiden_mp4, is_hiden_video_id, _fetch as fetch_hiden_html
from host_embed import embed_url
from peach_db import abs_url, db_conn, get_setting, get_upload_account
from run_uploader import (
    HTTP_TIMEOUT,
    _download_mp4,
    _extract_full_mp4,
    _make_pre_from_file,
    _source_origin,
)
from pipeline_config import PRE_MAX_SEC

REFRESH_BEFORE_DAYS = int(os.environ.get("DOOD_REFRESH_BEFORE_DAYS", "55"))
BATCH_LIMIT = int(os.environ.get("DOOD_REFRESH_BATCH", "3"))


def _pick_refresh_rows(conn, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM videos
        WHERE status = 'published'
          AND host_provider = 'doodstream'
          AND dood_expires_at IS NOT NULL
          AND dood_expires_at <= now() + make_interval(days => %s)
        ORDER BY dood_expires_at ASC
        LIMIT %s
        """,
        (REFRESH_BEFORE_DAYS, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _mark_refreshed(video_id: str, *, pre_fc: str, full_fc: str) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE videos SET
              pre_filecode = %s,
              full_filecode = %s,
              dood_expires_at = now() + interval '60 days',
              updated_at = now(),
              error_message = NULL
            WHERE id = %s
            """,
            (pre_fc, full_fc, video_id),
        )
        conn.commit()


def _refresh_one(row: dict, *, dood_key: str) -> None:
    vid = str(row["id"])
    pre_fc = str(row.get("pre_filecode") or "")
    full_fc = str(row.get("full_filecode") or "")

    if pre_fc and get_file_status(dood_key, pre_fc) == "active":
        if full_fc and get_file_status(dood_key, full_fc) == "active":
            print(f"dood-refresh: {vid} still active, bump expiry")
            with db_conn() as conn:
                conn.execute(
                    """
                    UPDATE videos
                    SET dood_expires_at = now() + interval '60 days',
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (vid,),
                )
                conn.commit()
            return

    with db_conn() as conn:
        origin = _source_origin(conn, row)

    with httpx.Client(headers=HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        page_url = abs_url(origin, row["video_path"])
        if is_hiden_video_id(vid):
            page_html = fetch_hiden_html(client, page_url)
        else:
            page_html = fetch_html(client, page_url)
        full_mp4 = _extract_full_mp4(page_html, vid)
        if not full_mp4:
            raise RuntimeError("No mp4 URL on source page for refresh")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            full_path = tmp_dir / f"{vid}-full.mp4"
            pre_path = tmp_dir / f"{vid}-pre.mp4"
            print(f"dood-refresh: {vid} download full")
            _download_mp4(full_mp4, full_path)
            print(f"dood-refresh: {vid} upload full")
            new_full = upload_file(dood_key, str(full_path))
            print(f"dood-refresh: {vid} build pre")
            _make_pre_from_file(full_path, pre_path)
            print(f"dood-refresh: {vid} upload pre")
            new_pre = upload_file(dood_key, str(pre_path))

    _mark_refreshed(vid, pre_fc=new_pre, full_fc=new_full)
    print(f"dood-refresh: {vid} refreshed pre={embed_url(new_pre, 'doodstream')}")


def run_dood_refresh() -> int:
    with db_conn() as conn:
        dood_acc = get_upload_account(conn, "doodstream")
        if not dood_acc:
            print("dood-refresh: no doodstream account, skip")
            return 0
        rows = _pick_refresh_rows(conn, BATCH_LIMIT)

    if not rows:
        print("dood-refresh: nothing due")
        return 0

    dood_key = str(dood_acc["secret"])
    log_account_info(dood_key)
    done = 0
    for row in rows:
        vid = str(row["id"])
        try:
            _refresh_one(row, dood_key=dood_key)
            done += 1
            time.sleep(2.0)
        except (DoodStreamError, RuntimeError, httpx.HTTPError) as exc:
            print(f"dood-refresh: failed {vid}: {exc}")
    print(f"dood-refresh: done {done}")
    return done


if __name__ == "__main__":
    run_dood_refresh()
