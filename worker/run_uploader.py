"""Upload worker: namevids (default) or Vidara/DoodStream + namevids."""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ebalka_parser import HEADERS, extract_mp4_url, extract_preview_from_video_page, _fetch as fetch_html
from hiden_parser import extract_hiden_mp4, is_hiden_video_id, _fetch as fetch_hiden_html
from namevids_client import (
    build_caption,
    build_namevids_caption_link,
    clear_pending_drafts,
    delete_draft_fid,
    fetch_api_key,
    login,
    parse_account_metadata,
    publish,
    sanitize_namevids_title,
    upload_stream,
)
from peach_db import abs_url, db_conn, get_setting, get_upload_account
from pipeline_config import MAX_PER_RUN, NAMEVIDS_DAILY_CAP, NAMEVIDS_ONLY, PRE_CLIP_SEC, PRE_MAX_SEC, VIDARA_POLL_SEC
from upload_recovery import recover_pending_uploads
from host_embed import embed_url, is_vidara_rate_or_hard_error
from vidara_client import (
    full_wait_timeout_sec,
    log_account_info,
    pre_wait_timeout_sec,
    probe_upload_server,
    upload_file as upload_vidara_file,
    wait_for_active,
)
from doodstream_client import log_account_info as log_dood_info, upload_file as upload_dood_file
FFMPEG_TIMEOUT_SEC = 180
HTTP_TIMEOUT = httpx.Timeout(30.0, read=120.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=900.0)
STUCK_UPLOADING_MINUTES = int(os.environ.get("STUCK_UPLOADING_MINUTES", "15"))
HARD_STUCK_UPLOADING_MINUTES = int(os.environ.get("HARD_STUCK_UPLOADING_MINUTES", "75"))


def _prefer_dood_enabled() -> bool:
    return os.environ.get("PREFER_DOOD", "").strip().lower() in ("1", "true", "yes", "on")


def _reset_stuck_uploading(conn) -> int:
    cur = conn.execute(
        """
        UPDATE videos
        SET status = 'parsed',
            error_message = 'reset: previous upload timed out',
            updated_at = now()
        WHERE status = 'uploading'
          AND (pre_filecode IS NULL OR pre_filecode = '')
          AND updated_at < now() - make_interval(mins => %s)
        RETURNING id
        """,
        (STUCK_UPLOADING_MINUTES,),
    )
    ids = [str(row["id"]) for row in cur.fetchall()]
    cur = conn.execute(
        """
        UPDATE videos
        SET status = 'parsed',
            pre_filecode = NULL,
            full_filecode = NULL,
            namevids_file_id = NULL,
            error_message = 'reset: upload run exceeded time limit',
            updated_at = now()
        WHERE status = 'uploading'
          AND updated_at < now() - make_interval(mins => %s)
        RETURNING id
        """,
        (HARD_STUCK_UPLOADING_MINUTES,),
    )
    ids.extend(str(row["id"]) for row in cur.fetchall())
    if ids:
        print(f"uploader: reset stuck uploading: {', '.join(ids)}")
    return len(ids)


def _load_settings(conn) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {str(r["key"]): str(r["value"] or "") for r in rows}


def _recent_rate_limit_count(conn) -> int:
    row = conn.execute(
        """
        SELECT count(*) AS cnt
        FROM videos
        WHERE error_message LIKE '%%429%%'
          AND updated_at > now() - interval '30 minutes'
        """
    ).fetchone()
    return int(row["cnt"] if row else 0)


def _effective_batch_size(conn, *, dood_available: bool) -> int:
    if dood_available:
        return MAX_PER_RUN
    recent = _recent_rate_limit_count(conn)
    if recent >= 3:
        return 1
    if recent >= 1:
        return min(1, MAX_PER_RUN)
    return MAX_PER_RUN


def _release_unprocessed(queue: list[dict[str, Any]], processed: int) -> None:
    remaining = [str(row["id"]) for row in queue[processed:]]
    if not remaining:
        return
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE videos
            SET status = 'parsed',
                updated_at = now()
            WHERE id = ANY(%s)
              AND status = 'uploading'
            """,
            (remaining,),
        )
        conn.commit()
    print(f"uploader: released {len(remaining)} unprocessed claim(s)")


def _claim_videos(conn, limit: int) -> list[dict[str, Any]]:
    """Atomically claim parsed rows so parallel uploader runs do not double-upload."""
    rows = conn.execute(
        """
        UPDATE videos
        SET status = 'uploading', updated_at = now()
        WHERE id IN (
            SELECT id FROM videos
            WHERE status = 'parsed'
              AND (
                error_message IS NULL
                OR error_message NOT LIKE '%%429%%'
                OR updated_at < now() - interval '60 minutes'
              )
            ORDER BY
              CASE WHEN id LIKE 'h%%' THEN 0 ELSE 1 END,
              CASE
                WHEN error_message IS NULL OR error_message = '' THEN 0
                WHEN error_message LIKE '%%429%%' THEN 2
                ELSE 1
              END,
              duration_seconds ASC NULLS LAST,
              parsed_at ASC,
              id ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _mark_published(
    video_id: str,
    *,
    pre_fc: str,
    full_fc: str,
    file_id: str,
    namevids_account_id: int,
    vidara_account_id: int | None,
    host_provider: str = "vidara",
) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE videos SET
              status = 'published',
              pre_filecode = %s,
              full_filecode = %s,
              namevids_file_id = %s,
              namevids_account_id = %s,
              vidara_account_id = %s,
              host_provider = %s,
              dood_expires_at = CASE
                WHEN %s = 'doodstream' THEN now() + interval '60 days'
                ELSE NULL
              END,
              published_at = now(),
              updated_at = now(),
              error_message = NULL
            WHERE id = %s
            """,
            (
                pre_fc,
                full_fc,
                file_id,
                namevids_account_id,
                vidara_account_id,
                host_provider,
                host_provider,
                video_id,
            ),
        )
        conn.commit()


def _mark_skipped(video_id: str, error: str) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE videos
            SET status = 'skipped',
                error_message = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (error[:500], video_id),
        )
        conn.commit()


def _mark_failed(video_id: str, error: str) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE videos
            SET status = 'failed',
                error_message = %s,
                updated_at = now()
            WHERE id = %s
              AND status = 'uploading'
            """,
            (error[:500], video_id),
        )
        conn.commit()


def _mark_rate_limited(video_id: str, error: str) -> None:
    """Keep parsed status so other queue items can proceed; retry after cooldown."""
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE videos
            SET status = 'parsed',
                error_message = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (error[:500], video_id),
        )
        conn.commit()


def _save_upload_progress(
    video_id: str,
    *,
    pre_fc: str,
    full_fc: str,
    file_id: str,
    namevids_account_id: int,
    vidara_account_id: int | None,
    host_provider: str = "vidara",
) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE videos SET
              pre_filecode = %s,
              full_filecode = %s,
              namevids_file_id = %s,
              namevids_account_id = %s,
              vidara_account_id = %s,
              host_provider = %s,
              updated_at = now()
            WHERE id = %s
            """,
            (
                pre_fc,
                full_fc,
                file_id,
                namevids_account_id,
                vidara_account_id,
                host_provider,
                video_id,
            ),
        )
        conn.commit()


def _upload_host_file(
    *,
    vidara_key: str,
    dood_key: str | None,
    path: str,
    prefer_dood: bool,
) -> tuple[str, str, int | None]:
    """Returns (filecode, host_provider, account_id)."""
    if prefer_dood:
        if not dood_key:
            raise RuntimeError("DoodStream fallback requested but no API key configured")
        return upload_dood_file(dood_key, path), "doodstream", None

    try:
        return upload_vidara_file(vidara_key, path), "vidara", None
    except RuntimeError as exc:
        if dood_key and is_vidara_rate_or_hard_error(exc):
            print(f"uploader: Vidara failed ({exc}), falling back to DoodStream")
            return upload_dood_file(dood_key, path), "doodstream", None
        raise


def _make_pre_loop(source_path: Path, out_path: Path, max_sec: int = PRE_MAX_SEC) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-stream_loop",
            "2",
            "-i",
            str(source_path),
            "-t",
            str(max_sec),
            "-c",
            "copy",
            str(out_path),
        ],
        check=True,
        capture_output=True,
        timeout=FFMPEG_TIMEOUT_SEC,
    )


def _make_pre_clip(source: Path, clip_path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-t",
            str(PRE_CLIP_SEC),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(clip_path),
        ],
        check=True,
        capture_output=True,
        timeout=FFMPEG_TIMEOUT_SEC,
    )


def _make_pre_from_file(source_path: Path, out_path: Path, max_sec: int = PRE_MAX_SEC) -> None:
    clip_path = out_path.with_name(f"{out_path.stem}-clip.mp4")
    _make_pre_clip(source_path, clip_path)
    _make_pre_loop(clip_path, out_path, max_sec=max_sec)
    clip_path.unlink(missing_ok=True)


def _file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def _hiden_proxy_get(url: str) -> tuple[str, dict[str, str]] | None:
    if "hiden.live" not in url:
        return None
    proxy_base = os.environ.get("HIDEN_FETCH_PROXY", "").strip().rstrip("/")
    secret = os.environ.get("PARSER_FETCH_SECRET", "").strip()
    if not proxy_base:
        return None
    headers = {"X-Cron-Secret": secret} if secret else {}
    return f"{proxy_base}?url={quote(url, safe='')}", headers


def _download_mp4(url: str, dest: Path) -> None:
    proxied = _hiden_proxy_get(url)
    with httpx.Client(headers=HEADERS, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        request_url = proxied[0] if proxied else url
        request_headers = proxied[1] if proxied else HEADERS
        with client.stream("GET", request_url, headers=request_headers) as res:
            res.raise_for_status()
            total = int(res.headers.get("content-length") or 0)
            written = 0
            with dest.open("wb") as handle:
                for chunk in res.iter_bytes():
                    handle.write(chunk)
                    written += len(chunk)
                    if total and written % (5 * 1024 * 1024) < len(chunk):
                        print(f"uploader: download {written / (1024 * 1024):.0f}/{total / (1024 * 1024):.0f} MB")
            if written < 1024 * 100:
                raise RuntimeError(f"Download too small ({written} bytes) — URL likely expired")


def _source_origin(conn, row: dict[str, Any]) -> str:
    if is_hiden_video_id(str(row.get("id") or "")):
        return get_setting(conn, "hiden.source_origin", "https://hiden.live").rstrip("/")
    return get_setting(conn, "ebalka.source_origin", "https://a.ebalka.love").rstrip("/")


def _extract_full_mp4(page_html: str, video_id: str) -> str | None:
    if is_hiden_video_id(video_id):
        return extract_hiden_mp4(page_html)
    return extract_mp4_url(page_html)


def _refresh_ebalka_preview(row: dict[str, Any], origin: str) -> str | None:
    video_path = str(row.get("video_path") or "").strip()
    if not video_path or is_hiden_video_id(str(row.get("id") or "")):
        return None
    with httpx.Client(headers=HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        page_html = fetch_html(client, abs_url(origin, video_path))
    preview_path = extract_preview_from_video_page(page_html, origin)
    if not preview_path:
        return None
    vid = str(row["id"])
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE videos
            SET preview_path = %s, updated_at = now()
            WHERE id = %s
            """,
            (preview_path, vid),
        )
        conn.commit()
    print(f"uploader: {vid} refreshed preview_path")
    row["preview_path"] = preview_path
    return preview_path


def _download_namevids_preview(origin: str, row: dict[str, Any], dest: Path) -> None:
    """Short swipe/preview clip for namevids — ebalka data-preview or hiden preview_clip URL."""
    preview_path = str(row.get("preview_path") or "").strip()
    if not preview_path:
        raise RuntimeError("No preview_path for namevids")
    url = abs_url(origin, preview_path)
    label = "hiden preview clip" if is_hiden_video_id(str(row.get("id") or "")) else "ebalka swipe preview"
    print(f"uploader: download {label} for namevids")
    try:
        _download_mp4(url, dest)
    except RuntimeError as exc:
        if "too small" not in str(exc).lower():
            raise
        refreshed = _refresh_ebalka_preview(dict(row), origin)
        if not refreshed:
            raise RuntimeError(f"{exc} — preview expired, refresh failed") from exc
        url = abs_url(origin, refreshed)
        print(f"uploader: retry preview download after refresh")
        _download_mp4(url, dest)
    print(f"uploader: namevids clip size {_file_size_mb(dest):.1f} MB")


def _publish_namevids_clip(
    *,
    row: dict[str, Any],
    settings: dict[str, str],
    namevids_acc: dict[str, Any],
    preview_path: Path,
) -> str:
    vid = str(row["id"])
    nv_client = login(
        str(namevids_acc["login"]),
        str(namevids_acc["secret"]),
        metadata=parse_account_metadata(namevids_acc.get("metadata")),
    )
    file_id = ""
    api_key = ""
    try:
        api_key = fetch_api_key(nv_client)
        file_id = upload_stream(nv_client, api_key, str(preview_path), f"{vid}.mp4")
        link = build_namevids_caption_link(settings, vid)
        caption = build_caption(settings, dict(row), link)
        title = sanitize_namevids_title(
            title_ru=str(row.get("title_ru") or ""),
            title_en=str(row.get("title_en") or ""),
            video_id=vid,
        )
        fallback_caption = f"full video\n{link}\n"
        print(f"uploader: {vid} namevids title={title!r} link={link}")
        publish(
            nv_client,
            api_key,
            file_id,
            title,
            caption,
            fallback_caption=fallback_caption,
            fallback_title=vid,
        )
        return file_id
    except Exception:
        if file_id and api_key:
            try:
                delete_draft_fid(nv_client, api_key, file_id)
            except Exception as cleanup_exc:
                print(f"uploader: {vid} draft cleanup failed: {cleanup_exc}")
        raise
    finally:
        nv_client.close()


def _upload_namevids_only(
    row: dict[str, Any],
    *,
    settings: dict[str, str],
    namevids_acc: dict[str, Any],
) -> None:
    vid = str(row["id"])
    preview_path = str(row.get("preview_path") or "").strip()
    if not preview_path:
        raise RuntimeError("No preview_path for namevids")

    with db_conn() as conn:
        origin = _source_origin(conn, row)

    with tempfile.TemporaryDirectory() as tmp:
        nv_preview_path = Path(tmp) / f"{vid}-nv-preview.mp4"
        _download_namevids_preview(origin, dict(row), nv_preview_path)
        file_id = _publish_namevids_clip(
            row=dict(row),
            settings=settings,
            namevids_acc=namevids_acc,
            preview_path=nv_preview_path,
        )

    _mark_published(
        vid,
        pre_fc="",
        full_fc="",
        file_id=file_id,
        namevids_account_id=int(namevids_acc["id"]),
        vidara_account_id=None,
        host_provider="vidara",
    )
    print(f"uploader: published {vid} (namevids-only)")


def _upload_one(
    row: dict[str, Any],
    *,
    settings: dict[str, str],
    vidara_acc: dict[str, Any],
    namevids_acc: dict[str, Any],
    dood_acc: dict[str, Any] | None,
    prefer_dood: bool,
) -> None:
    vid = str(row["id"])
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
            raise RuntimeError("No mp4 URL on video page")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            full_path = tmp_dir / f"{vid}-full.mp4"
            pre_path = tmp_dir / f"{vid}-pre.mp4"
            vidara_key = str(vidara_acc.get("secret") or "")
            dood_key = str(dood_acc["secret"]) if dood_acc else None

            if not prefer_dood and vidara_key:
                try:
                    probe_upload_server(vidara_key)
                except RuntimeError as exc:
                    if dood_key and is_vidara_rate_or_hard_error(exc):
                        prefer_dood = True
                        print(f"uploader: {vid} Vidara probe failed, DoodStream fallback: {exc}")
                    else:
                        raise

            print(f"uploader: {vid} download full (token URL, must be first)")
            _download_mp4(full_mp4, full_path)
            print(f"uploader: {vid} full size {_file_size_mb(full_path):.1f} MB")

            host_label = "DoodStream" if prefer_dood else "Vidara"
            print(f"uploader: {vid} upload full (file) -> {host_label}")
            full_fc, host_provider, _ = _upload_host_file(
                vidara_key=vidara_key,
                dood_key=dood_key,
                path=str(full_path),
                prefer_dood=prefer_dood,
            )
            print(f"uploader: {vid} full filecode={full_fc} host={host_provider}")

            print(f"uploader: {vid} build pre ({PRE_MAX_SEC}s loop) from local full")
            _make_pre_from_file(full_path, pre_path)
            print(f"uploader: {vid} pre size {_file_size_mb(pre_path):.1f} MB")

            print(f"uploader: {vid} upload pre -> {host_label}")
            pre_fc, pre_host, _ = _upload_host_file(
                vidara_key=vidara_key,
                dood_key=dood_key,
                path=str(pre_path),
                prefer_dood=prefer_dood or host_provider == "doodstream",
            )
            if pre_host != host_provider:
                host_provider = pre_host

            full_mb = _file_size_mb(full_path)
            file_id = ""
            vidara_account_id = int(vidara_acc["id"]) if host_provider == "vidara" else None
            _save_upload_progress(
                vid,
                pre_fc=pre_fc,
                full_fc=full_fc,
                file_id=file_id,
                namevids_account_id=int(namevids_acc["id"]),
                vidara_account_id=vidara_account_id,
                host_provider=host_provider,
            )

            print(f"uploader: {vid} upload ebalka swipe preview -> namevids")
            try:
                nv_client = login(
                    str(namevids_acc["login"]),
                    str(namevids_acc["secret"]),
                    metadata=parse_account_metadata(namevids_acc.get("metadata")),
                )
                api_key = fetch_api_key(nv_client)
                nv_preview_path = tmp_dir / f"{vid}-nv-preview.mp4"
                _download_namevids_preview(origin, dict(row), nv_preview_path)
                file_id = upload_stream(nv_client, api_key, str(nv_preview_path), f"{vid}.mp4")
                time.sleep(3)
                link = build_namevids_caption_link(settings, vid)
                caption = build_caption(settings, dict(row), link)
                title = sanitize_namevids_title(
                    title_ru=str(row.get("title_ru") or ""),
                    title_en=str(row.get("title_en") or ""),
                    video_id=vid,
                )
                print(f"uploader: {vid} namevids title={title!r} link={link}")
                for attempt in range(2):
                    try:
                        publish(
                            nv_client,
                            api_key,
                            file_id,
                            title,
                            caption,
                            fallback_title=vid,
                        )
                        break
                    except RuntimeError as exc:
                        if attempt == 0 and "temporary table" in str(exc).lower():
                            print(f"uploader: {vid} namevids publish retry after delay")
                            time.sleep(8)
                            continue
                        raise
                nv_client.close()
                _save_upload_progress(
                    vid,
                    pre_fc=pre_fc,
                    full_fc=full_fc,
                    file_id=file_id,
                    namevids_account_id=int(namevids_acc["id"]),
                    vidara_account_id=vidara_account_id,
                    host_provider=host_provider,
                )
            except Exception as exc:
                print(f"uploader: {vid} namevids skipped (Vidara publish continues): {exc}")

    if host_provider == "vidara" and vidara_key and not prefer_dood:
        print(f"uploader: {vid} wait Vidara pre (best effort)")
        try:
            wait_for_active(
                vidara_key,
                pre_fc,
                label="pre",
                timeout_sec=pre_wait_timeout_sec(),
                poll_sec=VIDARA_POLL_SEC,
            )
        except RuntimeError as exc:
            if "pre" in str(exc).lower():
                print(f"uploader: {vid} pre not ready yet, publishing anyway: {exc}")
            else:
                raise

        print(f"uploader: {vid} wait Vidara full (best effort, {full_mb:.0f} MB)")
        try:
            wait_for_active(
                vidara_key,
                full_fc,
                label="full",
                timeout_sec=full_wait_timeout_sec(full_mb),
                poll_sec=max(VIDARA_POLL_SEC, 8),
            )
        except RuntimeError as exc:
            if "full" in str(exc).lower():
                print(f"uploader: {vid} full not ready yet, publishing with pre only: {exc}")
            else:
                raise

    _mark_published(
        vid,
        pre_fc=pre_fc,
        full_fc=full_fc,
        file_id=file_id,
        namevids_account_id=int(namevids_acc["id"]),
        vidara_account_id=vidara_account_id,
        host_provider=host_provider,
    )
    print(f"uploader: published {vid} pre={embed_url(pre_fc, host_provider)}")


def _recent_namevids_rate_limits(conn) -> int:
    row = conn.execute(
        """
        SELECT count(*)::int AS n
        FROM videos
        WHERE error_message ILIKE '%%too fast%%'
          AND updated_at > now() - interval '30 minutes'
        """
    ).fetchone()
    return int(row["n"] if row else 0)


def _namevids_published_today(conn, namevids_account_id: int) -> int:
    row = conn.execute(
        """
        SELECT count(*)::int AS n
        FROM videos
        WHERE status = 'published'
          AND namevids_account_id = %s
          AND coalesce(published_at, updated_at) >= date_trunc('day', now() AT TIME ZONE 'UTC')
        """,
        (namevids_account_id,),
    ).fetchone()
    return int(row["n"] if row else 0)


def run_uploader() -> int:
    done = 0
    recovered = recover_pending_uploads()
    vidara_acc: dict[str, Any] | None = None
    dood_acc: dict[str, Any] | None = None
    prefer_dood = False
    with db_conn() as conn:
        settings = _load_settings(conn)
        namevids_acc = get_upload_account(conn, "namevids")
        if not namevids_acc:
            raise RuntimeError("No enabled namevids account in upload_accounts")

        if NAMEVIDS_ONLY:
            print("uploader: NAMEVIDS_ONLY=1 — skip Vidara/DoodStream host uploads")
            _reset_stuck_uploading(conn)
            published_today = _namevids_published_today(conn, int(namevids_acc["id"]))
            if published_today >= NAMEVIDS_DAILY_CAP:
                print(
                    f"uploader: namevids daily cap {NAMEVIDS_DAILY_CAP} reached "
                    f"({published_today} today on account {namevids_acc['login']}) — skip run"
                )
                return 0
            remaining_today = NAMEVIDS_DAILY_CAP - published_today
            recent_rl = _recent_namevids_rate_limits(conn)
            if recent_rl >= 3:
                print(
                    f"uploader: namevids rate-limited ({recent_rl} recent) — skip run, "
                    "wait for cooldown"
                )
                return 0
            batch_size = 1 if recent_rl >= 1 else MAX_PER_RUN
            batch_size = min(batch_size, remaining_today)
            if batch_size < 1:
                return 0
            if batch_size < MAX_PER_RUN:
                if recent_rl >= 1:
                    print(f"uploader: namevids rate-limit cooldown — batch {batch_size}/{MAX_PER_RUN}")
                else:
                    print(
                        f"uploader: namevids daily budget — batch {batch_size} "
                        f"({remaining_today} left today)"
                    )
            queue = _claim_videos(conn, batch_size)
            conn.commit()
        else:
            vidara_acc = get_upload_account(conn, "vidara")
            dood_acc = get_upload_account(conn, "doodstream")
            if not vidara_acc and not dood_acc:
                raise RuntimeError("No enabled vidara or doodstream account in upload_accounts")

            prefer_dood = _prefer_dood_enabled()
            if prefer_dood:
                print("uploader: PREFER_DOOD=1 — host uploads use DoodStream only")
            if vidara_acc and not prefer_dood:
                log_account_info(str(vidara_acc["secret"]))
                try:
                    probe_upload_server(str(vidara_acc["secret"]))
                except RuntimeError as exc:
                    if dood_acc and is_vidara_rate_or_hard_error(exc):
                        prefer_dood = True
                        print(f"uploader: Vidara unavailable, batch uses DoodStream: {exc}")
                    elif not dood_acc:
                        if "429" in str(exc):
                            print(f"uploader: Vidara rate-limited, skipping run: {exc}")
                            return 0
                        raise
            else:
                prefer_dood = True

            if dood_acc:
                log_dood_info(str(dood_acc["secret"]))

            _reset_stuck_uploading(conn)
            dood_available = bool(dood_acc)
            batch_size = _effective_batch_size(conn, dood_available=dood_available)
            if batch_size < MAX_PER_RUN:
                print(f"uploader: throttled batch {batch_size}/{MAX_PER_RUN} (recent 429s, no Dood)")
            queue = _claim_videos(conn, batch_size)
            conn.commit()

    processed = 0
    batch_prefer_dood = prefer_dood if not NAMEVIDS_ONLY else False
    try:
        if NAMEVIDS_ONLY and queue:
            prep_client = login(
                str(namevids_acc["login"]),
                str(namevids_acc["secret"]),
                metadata=parse_account_metadata(namevids_acc.get("metadata")),
            )
            try:
                prep_key = fetch_api_key(prep_client)
                clear_pending_drafts(prep_client, prep_key)
            finally:
                prep_client.close()
            with db_conn() as rl_conn:
                recent_rl = _recent_namevids_rate_limits(rl_conn)
            if recent_rl >= 1:
                cooldown = max(120.0, float(os.environ.get("NAMEVIDS_COOLDOWN_SEC", "180")))
                print(f"uploader: namevids cooldown {cooldown:.0f}s")
                time.sleep(cooldown)

        for row in queue:
            vid = str(row["id"])
            try:
                if NAMEVIDS_ONLY:
                    _upload_namevids_only(
                        row,
                        settings=settings,
                        namevids_acc=dict(namevids_acc),
                    )
                else:
                    row_prefer_dood = batch_prefer_dood or "429" in str(row.get("error_message") or "")
                    _upload_one(
                        row,
                        settings=settings,
                        vidara_acc=dict(vidara_acc or {}),
                        namevids_acc=dict(namevids_acc),
                        dood_acc=dict(dood_acc) if dood_acc else None,
                        prefer_dood=row_prefer_dood,
                    )
                done += 1
                processed += 1
                if processed < len(queue):
                    time.sleep(max(2.0, float(os.environ.get("UPLOADER_GAP_SEC", "4"))))
            except Exception as exc:
                if NAMEVIDS_ONLY:
                    err = str(exc)
                    if "too small" in err.lower() or "no preview_path" in err.lower():
                        _mark_skipped(vid, err)
                        print(f"uploader: skipped {vid}: {exc}")
                    else:
                        _mark_failed(vid, err)
                        print(f"uploader: failed {vid}: {exc}")
                    processed += 1
                    if "too fast" in err.lower():
                        print("uploader: namevids rate-limited — stop batch")
                        break
                    continue
                err = str(exc)
                if "429" in err and not dood_acc:
                    _mark_rate_limited(vid, err)
                    print(f"uploader: rate-limited {vid}, stopping batch: {exc}")
                    processed += 1
                    break
                if "429" in err and dood_acc:
                    print(f"uploader: rate-limited {vid}, DoodStream for rest of batch: {exc}")
                    _mark_rate_limited(vid, err)
                    batch_prefer_dood = True
                    processed += 1
                    continue
                _mark_failed(vid, err)
                print(f"uploader: failed {vid}: {exc}")
                processed += 1
    finally:
        if processed < len(queue):
            _release_unprocessed(queue, processed)

    print(f"uploader: done {done} video(s), recovered {recovered}")
    return done + recovered


if __name__ == "__main__":
    run_uploader()
