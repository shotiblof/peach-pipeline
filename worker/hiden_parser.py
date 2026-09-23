"""hiden.live catalog parser — HTML listing + /porno/{slug}/ detail pages."""
from __future__ import annotations

import html as html_lib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from description_clean import clean_description, filter_tags, is_usable_description
from ebalka_parser import (
    HEADERS,
    ParsedVideo,
    enrich_translations,
    save_parsed,
    synthesize_description,
)
from pipeline_config import (
    MAX_BACKLOG_PAGES,
    MAX_EMPTY_PAGES,
    MAX_NEW,
    MAX_VIDEOS,
    REQUEST_DELAY_SEC,
)

SITE = "https://hiden.live"
ID_PREFIX = "h"

CARD_BLOCK_RE = re.compile(r'<article class="card"[\s\S]*?</article>', re.I)
CARD_ID_RE = re.compile(r'data-video-id="(\d+)"', re.I)
CARD_HREF_RE = re.compile(r'href="(/porno/[^"#?]+/?)"', re.I)
CARD_TITLE_RE = re.compile(r'class="card-title[^"]*"[^>]*>([^<]+)', re.I)

TITLE_RE = re.compile(r'id="videoTitleText"[^>]*>([^<]+)', re.I)
CATEGORY_RE = re.compile(r'id="videoCategoryText"[^>]*>([^<]+)', re.I)
RAW_DESC_RE = re.compile(r'data-raw-description="([^"]+)"', re.I)
TAG_RE = re.compile(r'class="tag-chip"[^>]*>([^<]+)', re.I)
DURATION_RE = re.compile(r'<meta property="video:duration" content="(\d+)"', re.I)
OG_IMAGE_RE = re.compile(r'<meta property="og:image" content="([^"]+)"', re.I)
PREVIEW_CLIP_RE = re.compile(
    r'https://media\.hiden\.live/previews/clips/[^"\']+\.mp4',
    re.I,
)
FULL_MP4_RE = re.compile(
    r'https://media\.hiden\.live/videos/[^"\']+\.mp4',
    re.I,
)
PAGE_LINK_RE = re.compile(r'[?&]page=(\d+)', re.I)

RECOMMEND_TAIL_RE = re.compile(
    r"(?is)\s*рекомендуем\s+так\s+же\s+к\s+просмотру\s+наши\s+категории\s*:.*$",
)


@dataclass
class HidenCard:
    numeric_id: str
    video_path: str
    title: str


def hiden_video_id(numeric_id: str) -> str:
    return f"{ID_PREFIX}{numeric_id}"


def is_hiden_video_id(video_id: str) -> bool:
    return str(video_id).startswith(ID_PREFIX)


def clean_hiden_description(raw: str) -> str:
    text = html_lib.unescape(raw or "").strip()
    text = RECOMMEND_TAIL_RE.sub("", text).strip()
    text = clean_description(text)
    return text


def _sleep() -> None:
    time.sleep(REQUEST_DELAY_SEC)


def _fetch(client: httpx.Client, url: str) -> str:
    _sleep()
    proxy_base = os.environ.get("HIDEN_FETCH_PROXY", "").strip().rstrip("/")
    secret = os.environ.get("PARSER_FETCH_SECRET", "").strip()
    if proxy_base:
        from urllib.parse import quote

        headers = {"X-Cron-Secret": secret} if secret else {}
        res = client.get(f"{proxy_base}?url={quote(url, safe='')}", headers=headers, timeout=60.0)
    else:
        res = client.get(url, timeout=30.0)
    res.raise_for_status()
    return res.text


def _origin(conn: Any) -> str:
    from peach_db import get_setting

    return get_setting(conn, "hiden.source_origin", SITE).rstrip("/")


def parse_list_cards(html: str) -> list[HidenCard]:
    cards: list[HidenCard] = []
    seen: set[str] = set()
    for block in CARD_BLOCK_RE.finditer(html):
        chunk = block.group(0)
        id_match = CARD_ID_RE.search(chunk)
        href_match = CARD_HREF_RE.search(chunk)
        if not id_match or not href_match:
            continue
        numeric_id = id_match.group(1)
        if numeric_id in seen:
            continue
        seen.add(numeric_id)
        title_match = CARD_TITLE_RE.search(chunk)
        title = html_lib.unescape(title_match.group(1).strip()) if title_match else ""
        cards.append(
            HidenCard(
                numeric_id=numeric_id,
                video_path=href_match.group(1),
                title=title,
            )
        )
    return cards


def parse_total_pages(html: str) -> int | None:
    pages = [int(m.group(1)) for m in PAGE_LINK_RE.finditer(html)]
    if pages:
        return max(pages)
    return None


def fetch_total_pages(client: httpx.Client, origin: str, html: str) -> int:
    """Home pagination shows only neighbors — use sitemap chunks when hint is too low."""
    hinted = parse_total_pages(html)
    if hinted and hinted >= 20:
        return hinted
    try:
        sm = _fetch(client, f"{origin}/sitemap.xml")
        chunks = [
            u.replace("&amp;", "&")
            for u in re.findall(r"<loc>([^<]+)</loc>", sm)
            if "type=videos" in u and "lang=ru" in u
        ]
        if chunks:
            return max(58, (len(chunks) * 1000 + 49) // 50)
    except Exception:
        pass
    return 58


def parse_tags(html: str) -> list[str]:
    tags = [html_lib.unescape(m.group(1).strip()) for m in TAG_RE.finditer(html)]
    return filter_tags(list(dict.fromkeys(tags)))


def parse_description(html: str, *, title: str, tags: list[str], category: str) -> str:
    raw = RAW_DESC_RE.search(html)
    if raw:
        text = clean_hiden_description(raw.group(1))
        if is_usable_description(text):
            return text
    return synthesize_description(title, tags, category)


def extract_hiden_mp4(html: str) -> str | None:
    match = FULL_MP4_RE.search(html)
    return match.group(0) if match else None


def extract_preview_clip(html: str, numeric_id: str = "") -> str | None:
    """Pick preview clip from page HTML — prefer URLs tied to this video id."""
    clips = PREVIEW_CLIP_RE.findall(html)
    if not clips:
        return None
    if numeric_id:
        nid = numeric_id.strip()
        for clip in clips:
            name = clip.rsplit("/", 1)[-1].lower()
            if (
                f"clip_{nid}_" in name
                or f"clip_pad_{nid}" in name
                or name == f"clip_{nid}.mp4"
                or f"preview_clip_{nid}" in name
            ):
                return clip
    return clips[0]


def fetch_preview_clip_api(client: httpx.Client, origin: str, numeric_id: str) -> str | None:
    """Official hiden preview endpoint — correct clip, not sidebar noise."""
    try:
        payload = _fetch(client, f"{origin}/api/video/{numeric_id}/preview-clip/")
        data = json.loads(payload)
        clip = str(data.get("clip_url") or "").strip()
        if clip.startswith("http"):
            return clip
    except Exception:
        pass
    return None


def resolve_preview_clip(
    client: httpx.Client,
    origin: str,
    numeric_id: str,
    html: str,
) -> str | None:
    return (
        fetch_preview_clip_api(client, origin, numeric_id)
        or extract_preview_clip(html, numeric_id)
    )


def parse_poster(html: str) -> str:
    og = OG_IMAGE_RE.search(html)
    if og:
        return og.group(1).strip()
    return ""


def parse_duration(html: str) -> int | None:
    match = DURATION_RE.search(html)
    if not match:
        return None
    seconds = int(match.group(1))
    return seconds if seconds > 0 else None


def parse_video_page(
    html: str,
    card: HidenCard,
    origin: str,
    *,
    preview_clip: str | None = None,
) -> ParsedVideo:
    title_match = TITLE_RE.search(html)
    title = clean_description(title_match.group(1)) if title_match else card.title
    category_match = CATEGORY_RE.search(html)
    category = category_match.group(1).strip() if category_match else ""
    tags = parse_tags(html)
    description = parse_description(html, title=title, tags=tags, category=category)
    poster = parse_poster(html)
    preview = preview_clip or extract_preview_clip(html, card.numeric_id) or poster
    poster_path = poster or preview
    preview_path = preview or poster_path

    if preview_path.startswith("http"):
        preview_storage = preview_path
    else:
        preview_storage = preview_path

    if poster_path.startswith("http"):
        poster_storage = poster_path
    else:
        poster_storage = poster_path

    return ParsedVideo(
        id=hiden_video_id(card.numeric_id),
        video_path=card.video_path,
        preview_path=preview_storage,
        poster_path=poster_storage,
        duration_seconds=parse_duration(html),
        title_ru=title,
        description_ru=description,
        title_en="",
        description_en="",
        tags=tags,
        tags_en=[],
        category=category,
        category_en="",
    )


def video_exists(conn: Any, video_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM videos WHERE id = %s", (video_id,)).fetchone()
    return bool(row)


def _parser_mode() -> str:
    mode = os.environ.get("PARSER_MODE", "full").strip().lower()
    if mode not in ("latest", "backlog", "full"):
        raise ValueError(f"invalid PARSER_MODE: {mode!r}")
    return mode


def _load_state(conn: Any, *, lock: bool = False) -> dict[str, Any]:
    sql = "SELECT * FROM parser_state WHERE id = 1"
    if lock:
        sql += " FOR UPDATE"
    state = conn.execute(sql).fetchone()
    if not state:
        raise RuntimeError("parser_state row missing")
    return state


def _list_url(origin: str, page: int) -> str:
    if page <= 1:
        return f"{origin}/"
    return f"{origin}/?page={page}"


def _parse_latest(
    conn: Any,
    client: httpx.Client,
    origin: str,
    html: str,
    *,
    processed: int,
) -> int:
    new_count = 0
    for card in parse_list_cards(html):
        if processed >= MAX_VIDEOS or new_count >= MAX_NEW:
            break
        vid = hiden_video_id(card.numeric_id)
        if video_exists(conn, vid):
            continue
        page_html = _fetch(client, origin + card.video_path)
        clip = resolve_preview_clip(client, origin, card.numeric_id, page_html)
        parsed = enrich_translations(
            parse_video_page(page_html, card, origin, preview_clip=clip)
        )
        save_parsed(conn, parsed)
        processed += 1
        new_count += 1
    return processed


def _parse_backlog(
    conn: Any,
    client: httpx.Client,
    origin: str,
    state: dict[str, Any],
    *,
    processed: int,
) -> int:
    if state.get("hiden_complete") or processed >= MAX_VIDEOS:
        return processed

    total = int(state.get("hiden_total_pages") or 60)
    page = int(state.get("hiden_current_page") or total)

    for _ in range(MAX_BACKLOG_PAGES):
        if page < 1:
            conn.execute(
                "UPDATE parser_state SET hiden_complete = true, updated_at = now() WHERE id = 1"
            )
            break
        before = processed
        html = _fetch(client, _list_url(origin, page))
        cards = list(reversed(parse_list_cards(html)))
        for card in cards:
            if processed >= MAX_VIDEOS:
                break
            vid = hiden_video_id(card.numeric_id)
            if video_exists(conn, vid):
                continue
            page_html = _fetch(client, origin + card.video_path)
            clip = resolve_preview_clip(client, origin, card.numeric_id, page_html)
            parsed = enrich_translations(
                parse_video_page(page_html, card, origin, preview_clip=clip)
            )
            save_parsed(conn, parsed)
            processed += 1

        page -= 1
        conn.execute(
            """
            UPDATE parser_state
            SET hiden_current_page = %s, hiden_total_pages = %s, updated_at = now()
            WHERE id = 1
            """,
            (page, total),
        )

        if page < 1:
            conn.execute(
                "UPDATE parser_state SET hiden_complete = true, updated_at = now() WHERE id = 1"
            )
            break

    return processed


def run_hiden_parser() -> int:
    from peach_db import db_conn

    mode = _parser_mode()
    processed = 0

    with db_conn() as conn:
        if conn.execute(
            "SELECT hiden_complete FROM parser_state WHERE id = 1"
        ).fetchone().get("hiden_complete"):
            print("hiden_parser: backlog complete — skip")
            return 0

        origin = _origin(conn)
        lock_state = mode == "backlog"
        state = _load_state(conn, lock=lock_state)

        with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
            if mode in ("latest", "full"):
                html = _fetch(client, _list_url(origin, 1))
                total = fetch_total_pages(client, origin, html)
                conn.execute(
                    """
                    UPDATE parser_state
                    SET hiden_total_pages = %s, updated_at = now()
                    WHERE id = 1
                    """,
                    (total,),
                )
                processed = _parse_latest(conn, client, origin, html, processed=processed)

            if mode in ("backlog", "full") and not (
                mode == "full" and state.get("hiden_complete")
            ):
                processed = _parse_backlog(conn, client, origin, state, processed=processed)

        conn.commit()

    print(f"hiden_parser[{mode}]: processed {processed} video(s)")
    return processed


if __name__ == "__main__":
    run_hiden_parser()
