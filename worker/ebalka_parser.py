"""Ebalka list + video page parser."""
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

from peach_db import abs_url, to_storage_path
from description_clean import MIN_DESCRIPTION_LEN, clean_description, filter_tags, is_usable_description
from pipeline_config import (
    MAX_BACKLOG_PAGES,
    MAX_EMPTY_PAGES,
    MAX_NEW,
    MAX_VIDEOS,
    REQUEST_DELAY_SEC,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Connection": "close",
}

CARD_RE = re.compile(
    r'<a class="card__wrap[^"]*" href="([^"]*/video/(\d+)/[^"]*)"[^>]*title="([^"]*)"[\s\S]*?data-preview="([^"]+)"',
    re.I,
)
CARD_RE_NO_TITLE = re.compile(
    r'<a class="card__wrap[^"]*" href="([^"]*/video/(\d+)/[^"]*)"[\s\S]*?data-preview="([^"]+)"',
    re.I,
)
MAX_PAGES_RE = re.compile(
    r'class="pagination__item"[^>]*type="number"[^>]*max="(\d+)"',
    re.I,
)
LAST_PAGE_RE = re.compile(
    r'class="pagination__item last[^"]*"[^>]*href="[^"]*/latest-videos/(\d+)/"',
    re.I,
)
TITLE_RE = re.compile(r'<h1[^>]*class="[^"]*video__title[^"]*"[^>]*>([\s\S]*?)</h1>', re.I)
POSTER_RE = re.compile(r'id="xp-video"[^>]+poster="([^"]+)"', re.I)
OG_IMAGE_RE = re.compile(r'<meta property="og:image" content="([^"]+)"', re.I)
DESC_RE = re.compile(
    r'<p class="text text_paragraph video__desc">([\s\S]*?)</p>',
    re.I,
)
META_DESC_RE = re.compile(r'<meta name="description" content="([^"]+)"', re.I)
TAGS_BLOCK_RE = re.compile(
    r'<ul class="video__tags-container">([\s\S]*?)</ul>',
    re.I,
)
TAG_RE = re.compile(
    r'<a[^>]*class="[^"]*\btag\b[^"]*"[^>]*>\s*([^<]+?)\s*</a>',
    re.I,
)
CATEGORY_RE = re.compile(
    r'Категории:\s*<a[^>]+href="[^"]*/categories/[^"]+"[^>]*>\s*([^<]+?)\s*</a>',
    re.I,
)
BREADCRUMB_CATEGORY_RE = re.compile(
    r'"@type":\s*"BreadcrumbList"[\s\S]*?"position":\s*3,\s*"name":\s*"([^"]+)"',
    re.I,
)
DURATION_RE = re.compile(r'<meta property="video:duration" content="(\d+)">', re.I)
VIDEO_PAGE_PREVIEW_RE = re.compile(r'data-preview="([^"]+)"', re.I)
VIDEO_SRC_PATTERNS = [
    re.compile(r'<video[^>]+id="xp-video"[^>]+src="([^"]+)"', re.I),
    re.compile(r"video_alt_url:\s*'([^']+)'"),
    re.compile(r"video_url:\s*'([^']+)'"),
    re.compile(r'<meta property="og:video" content="([^"]+)"', re.I),
    re.compile(r'"contentUrl":\s*"([^"]+\.mp4[^"]*)"', re.I),
]

SKIP_CATEGORY_NAMES = frozenset(
    {"Категории видео", "Ебалка", "Categories", "От пользователей Ебалки"}
)


@dataclass
class ListCard:
    id: str
    video_path: str
    title: str
    preview_path: str


@dataclass
class ParsedVideo:
    id: str
    video_path: str
    preview_path: str
    poster_path: str
    duration_seconds: int | None
    title_ru: str
    description_ru: str
    title_en: str
    description_en: str
    tags: list[str]
    tags_en: list[str]
    category: str
    category_en: str


def _sleep() -> None:
    time.sleep(REQUEST_DELAY_SEC)


def _fetch(client: httpx.Client, url: str) -> str:
    _sleep()
    res = client.get(url, timeout=30.0)
    res.raise_for_status()
    return res.text


def _normalize_video_path(href: str) -> str:
    if href.startswith("/"):
        return href
    parsed = urlparse(href)
    return parsed.path or href


def parse_cards(html: str, origin: str) -> list[ListCard]:
    cards: list[ListCard] = []
    for match in CARD_RE.finditer(html):
        href, vid, title, preview = match.groups()
        cards.append(
            ListCard(
                id=vid,
                video_path=_normalize_video_path(href),
                title=title.strip(),
                preview_path=to_storage_path(preview, origin),
            )
        )
    if cards:
        return cards
    for match in CARD_RE_NO_TITLE.finditer(html):
        href, vid, preview = match.groups()
        cards.append(
            ListCard(
                id=vid,
                video_path=_normalize_video_path(href),
                title="",
                preview_path=to_storage_path(preview, origin),
            )
        )
    return cards


def parse_total_pages(html: str) -> int | None:
    for pattern in (MAX_PAGES_RE, LAST_PAGE_RE):
        match = pattern.search(html)
        if match:
            return int(match.group(1))
    pages = [int(m.group(1)) for m in re.finditer(r"/latest-videos/(\d+)/", html)]
    return max(pages) if pages else None


def synthesize_description(title: str, tags: list[str], category: str) -> str:
    parts: list[str] = []
    title_value = title.strip().rstrip(".")
    if title_value:
        parts.append(f"{title_value}.")
    if category:
        parts.append(f"Категория: {category}.")
    if tags:
        parts.append("Теги: " + ", ".join(tags[:6]) + ".")
    return " ".join(parts)


def parse_tags(page_html: str) -> list[str]:
    block = TAGS_BLOCK_RE.search(page_html)
    scope = block.group(1) if block else page_html
    tags: list[str] = []
    for match in TAG_RE.finditer(scope):
        tag = html_lib.unescape(match.group(1).strip())
        if tag and tag not in tags:
            tags.append(tag)
    return filter_tags(tags)


def parse_category(html: str) -> str:
    match = CATEGORY_RE.search(html)
    if match:
        return match.group(1).strip()
    match = BREADCRUMB_CATEGORY_RE.search(html)
    if match:
        name = match.group(1).strip()
        if name not in SKIP_CATEGORY_NAMES:
            return name
    return ""


def parse_description(html: str, *, title: str, tags: list[str], category: str) -> str:
    desc_match = DESC_RE.search(html)
    if desc_match:
        text = clean_description(desc_match.group(1))
        if is_usable_description(text):
            return text

    meta_match = META_DESC_RE.search(html)
    if meta_match:
        text = clean_description(meta_match.group(1))
        if is_usable_description(text):
            return text

    return synthesize_description(title, tags, category)


def extract_mp4_url(html: str) -> str | None:
    for pattern in VIDEO_SRC_PATTERNS:
        match = pattern.search(html)
        if match:
            return match.group(1)
    return None


def extract_preview_from_video_page(html: str, origin: str) -> str | None:
    match = VIDEO_PAGE_PREVIEW_RE.search(html)
    if not match:
        return None
    return to_storage_path(match.group(1), origin)


def parse_duration(html: str) -> int | None:
    match = DURATION_RE.search(html)
    if not match:
        return None
    seconds = int(match.group(1))
    return seconds if seconds > 0 else None


def parse_video_page(html: str, card: ListCard, origin: str) -> ParsedVideo:
    title_match = TITLE_RE.search(html)
    title = clean_description(title_match.group(1)) if title_match else card.title

    tags = parse_tags(html)
    category = parse_category(html)
    description = parse_description(html, title=title, tags=tags, category=category)

    poster = ""
    poster_match = POSTER_RE.search(html)
    if poster_match:
        poster = to_storage_path(poster_match.group(1), origin)
    else:
        og_match = OG_IMAGE_RE.search(html)
        if og_match:
            poster = to_storage_path(og_match.group(1), origin)

    return ParsedVideo(
        id=card.id,
        video_path=card.video_path,
        preview_path=card.preview_path,
        poster_path=poster,
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


def enrich_translations(video: ParsedVideo) -> ParsedVideo:
    from translator import translate_fields, translate_taxonomy

    title_en, description_en = translate_fields(video.title_ru, video.description_ru)
    category_en, tags_en = translate_taxonomy(video.category, video.tags)
    return ParsedVideo(
        id=video.id,
        video_path=video.video_path,
        preview_path=video.preview_path,
        poster_path=video.poster_path,
        duration_seconds=video.duration_seconds,
        title_ru=video.title_ru,
        description_ru=video.description_ru,
        title_en=title_en,
        description_en=description_en,
        tags=video.tags,
        tags_en=tags_en,
        category=video.category,
        category_en=category_en,
    )


def video_exists(conn: Any, video_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM videos WHERE id = %s", (video_id,)).fetchone()
    return row is not None


def backfill_attribution_cleanup(conn: Any, *, limit: int = 40) -> int:
    """Clean stored descriptions/tags without re-fetching ebalka."""
    rows = conn.execute(
        """
        SELECT id, description_ru, description_en, tags, tags_en
        FROM videos
        WHERE description_ru ~* 'от пользовател'
           OR COALESCE(description_en, '') ~* 'от пользовател|by user'
           OR tags::text ~* 'Ебалки|ebalka users|От пользователей'
           OR COALESCE(tags_en::text, '') ~* 'Ебалки|ebalka users|От пользователей'
        ORDER BY updated_at ASC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    if not rows:
        return 0

    updated = 0
    for row in rows:
        tags_ru = row.get("tags") or []
        if isinstance(tags_ru, str):
            tags_ru = json.loads(tags_ru)
        tags_en = row.get("tags_en") or []
        if isinstance(tags_en, str):
            tags_en = json.loads(tags_en)

        desc_ru = clean_description(str(row.get("description_ru") or ""))
        desc_en = clean_description(str(row.get("description_en") or ""))
        new_tags_ru = filter_tags(tags_ru if isinstance(tags_ru, list) else [])
        new_tags_en = filter_tags(tags_en if isinstance(tags_en, list) else [])

        if (
            desc_ru == (row.get("description_ru") or "")
            and desc_en == (row.get("description_en") or "")
            and new_tags_ru == tags_ru
            and new_tags_en == tags_en
        ):
            continue

        conn.execute(
            """
            UPDATE videos
            SET description_ru = %s,
                description_en = NULLIF(%s, ''),
                tags = %s::jsonb,
                tags_en = %s::jsonb,
                updated_at = now()
            WHERE id = %s
            """,
            (
                desc_ru,
                desc_en,
                json.dumps(new_tags_ru, ensure_ascii=False),
                json.dumps(new_tags_en, ensure_ascii=False),
                str(row["id"]),
            ),
        )
        updated += 1
        print(f"parser: cleaned attribution {row['id']}")

    return updated


def backfill_metadata(conn: Any, *, limit: int = 20) -> int:
    """Re-fetch ebalka pages for rows missing category/tags/description."""
    rows = conn.execute(
        """
        SELECT id, video_path
        FROM videos
        WHERE category = ''
           OR tags = '[]'::jsonb
           OR description_ru = ''
           OR length(description_ru) < %s
           OR duration_seconds IS NULL
        ORDER BY parsed_at ASC
        LIMIT %s
        """,
        (MIN_DESCRIPTION_LEN, limit),
    ).fetchall()
    if not rows:
        return 0

    origin_row = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'ebalka.source_origin'",
    ).fetchone()
    origin = (origin_row["value"] if origin_row else "https://a.ebalka.love").rstrip("/")
    updated = 0

    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        for row in rows:
            try:
                page_html = _fetch(client, abs_url(origin, row["video_path"]))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    print(f"parser: skip metadata backfill {row['id']} — page 404")
                    continue
                raise
            card = ListCard(
                id=str(row["id"]),
                video_path=str(row["video_path"]),
                title="",
                preview_path="",
            )
            parsed = parse_video_page(page_html, card, origin)
            from translator import translate_fields, translate_taxonomy

            title_en, description_en = translate_fields(parsed.title_ru, parsed.description_ru)
            category_en, tags_en = translate_taxonomy(parsed.category, parsed.tags)
            conn.execute(
                """
                UPDATE videos
                SET category = %s,
                    category_en = COALESCE(NULLIF(category_en, ''), %s),
                    tags = %s::jsonb,
                    tags_en = CASE
                      WHEN tags_en IS NULL OR tags_en = '[]'::jsonb THEN %s::jsonb
                      ELSE tags_en
                    END,
                    description_ru = %s,
                    title_en = COALESCE(NULLIF(title_en, ''), %s),
                    description_en = COALESCE(NULLIF(description_en, ''), %s),
                    duration_seconds = COALESCE(%s, duration_seconds),
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    parsed.category,
                    category_en,
                    json.dumps(parsed.tags, ensure_ascii=False),
                    json.dumps(tags_en, ensure_ascii=False),
                    parsed.description_ru,
                    title_en,
                    description_en,
                    parsed.duration_seconds,
                    str(row["id"]),
                ),
            )
            updated += 1
            print(f"parser: backfilled metadata {row['id']} category={parsed.category!r} tags={len(parsed.tags)}")

    return updated


def _parser_mode() -> str:
    mode = os.environ.get("PARSER_MODE", "full").strip().lower()
    if mode not in ("latest", "backlog", "full"):
        raise ValueError(f"invalid PARSER_MODE: {mode!r}")
    return mode


def _load_parser_state(conn: Any, *, lock: bool = False) -> dict[str, Any]:
    sql = "SELECT * FROM parser_state WHERE id = 1"
    if lock:
        sql += " FOR UPDATE"
    state = conn.execute(sql).fetchone()
    if not state:
        raise RuntimeError("parser_state row missing — run db/schema.sql")
    return state


def _parse_latest_cards(
    conn: Any,
    client: httpx.Client,
    origin: str,
    html: str,
    *,
    processed: int,
) -> int:
    new_count = 0
    for card in parse_cards(html, origin):
        if processed >= MAX_VIDEOS or new_count >= MAX_NEW:
            break
        if video_exists(conn, card.id):
            continue
        page_html = _fetch(client, abs_url(origin, card.video_path))
        parsed = enrich_translations(parse_video_page(page_html, card, origin))
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
    if state.get("backlog_complete") or processed >= MAX_VIDEOS:
        return processed

    empty_streak = 0
    page = int(state.get("current_page") or state.get("total_pages") or 5000)
    for _ in range(MAX_BACKLOG_PAGES):
        if page < 1:
            conn.execute(
                "UPDATE parser_state SET backlog_complete = true, updated_at = now() WHERE id = 1"
            )
            break
        before = processed
        backlog_html = _fetch(client, abs_url(origin, f"/latest-videos/{page}/"))
        cards = list(reversed(parse_cards(backlog_html, origin)))
        for card in cards:
            if processed >= MAX_VIDEOS:
                break
            if video_exists(conn, card.id):
                continue
            page_html = _fetch(client, abs_url(origin, card.video_path))
            parsed = enrich_translations(parse_video_page(page_html, card, origin))
            save_parsed(conn, parsed)
            processed += 1
        if processed == before:
            empty_streak += 1
            if empty_streak >= MAX_EMPTY_PAGES:
                break
        else:
            empty_streak = 0
        page -= 1
        conn.execute(
            "UPDATE parser_state SET current_page = %s, updated_at = now() WHERE id = 1",
            (page,),
        )
    return processed


def save_parsed(conn: Any, video: ParsedVideo) -> None:
    conn.execute(
        """
        INSERT INTO videos (
          id, video_path, preview_path, poster_path, duration_seconds,
          title_ru, description_ru,
          title_en, description_en,
          category, category_en, tags, tags_en, status, parsed_at, updated_at
        ) VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, 'parsed', now(), now()
        )
        ON CONFLICT (id) DO NOTHING
        """,
        (
            video.id,
            video.video_path,
            video.preview_path,
            video.poster_path,
            video.duration_seconds,
            video.title_ru,
            video.description_ru,
            video.title_en,
            video.description_en,
            video.category,
            video.category_en,
            json.dumps(video.tags, ensure_ascii=False),
            json.dumps(video.tags_en, ensure_ascii=False),
        ),
    )


def run_parser() -> int:
    from peach_db import db_conn, get_setting

    mode = _parser_mode()
    processed = 0
    backfilled = 0
    taxonomy_backfilled = 0
    metadata_backfilled = 0
    attribution_cleaned = 0
    with db_conn() as conn:
        origin = get_setting(conn, "ebalka.source_origin", "https://a.ebalka.love")
        lock_state = mode == "backlog"
        state = _load_parser_state(conn, lock=lock_state)

        with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
            if mode in ("latest", "full"):
                list_url = abs_url(origin, "/latest-videos/1/")
                html = _fetch(client, list_url)
                total = parse_total_pages(html) or state.get("total_pages") or 5000
                conn.execute(
                    """
                    UPDATE parser_state
                    SET total_pages = %s, source_origin = %s, updated_at = now()
                    WHERE id = 1
                    """,
                    (total, origin),
                )
                processed = _parse_latest_cards(conn, client, origin, html, processed=processed)

            if mode in ("backlog", "full") and not (
                mode == "full" and state.get("backlog_complete")
            ):
                processed = _parse_backlog(conn, client, origin, state, processed=processed)

        from translator import backfill_taxonomy, backfill_translations

        backfilled = backfill_translations(conn, limit=10)
        taxonomy_backfilled = backfill_taxonomy(conn, limit=15)
        metadata_backfilled = backfill_metadata(conn, limit=20)
        attribution_cleaned = backfill_attribution_cleanup(conn, limit=40)
        conn.commit()

    print(
        f"parser[{mode}]: processed {processed} video(s), "
        f"backfilled {backfilled} translation(s), "
        f"{taxonomy_backfilled} taxonomy row(s), "
        f"{metadata_backfilled} metadata row(s), "
        f"{attribution_cleaned} attribution cleanup(s)"
    )
    return processed


if __name__ == "__main__":
    run_parser()