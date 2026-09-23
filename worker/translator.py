"""Free translation via deep-translator (Google / MyMemory)."""
from __future__ import annotations

import os
import time
import json
from collections.abc import Callable
from typing import Any

REQUEST_DELAY_SEC = 0.3


def _provider_name() -> str:
    return (os.environ.get("TRANSLATOR_PROVIDER") or "google").strip().lower()


def _sleep() -> None:
    time.sleep(REQUEST_DELAY_SEC)


def _translate_google(text: str, *, source: str, target: str) -> str:
    from deep_translator import GoogleTranslator

    return GoogleTranslator(source=source, target=target).translate(text)


def _translate_mymemory(text: str, *, source: str, target: str) -> str:
    from deep_translator import MyMemoryTranslator

    return MyMemoryTranslator(source=source, target=target).translate(text)


def _provider_chain() -> list[tuple[str, Callable[..., str]]]:
    preferred = _provider_name()
    by_name: dict[str, Callable[..., str]] = {
        "google": _translate_google,
        "mymemory": _translate_mymemory,
    }
    ordered: list[tuple[str, Callable[..., str]]] = []
    if preferred in by_name:
        ordered.append((preferred, by_name[preferred]))
    for name, fn in by_name.items():
        if name != preferred:
            ordered.append((name, fn))
    return ordered


def translate_text(text: str, *, source: str = "ru", target: str = "en") -> str:
    value = text.strip()
    if not value:
        return ""

    for name, fn in _provider_chain():
        try:
            _sleep()
            result = fn(value, source=source, target=target)
            if result and str(result).strip():
                return str(result).strip()
        except Exception as exc:
            print(f"translator: {name} failed: {exc}")
    return ""


def translate_fields(
    title_ru: str,
    description_ru: str,
    *,
    source: str = "ru",
    target: str = "en",
) -> tuple[str, str]:
    title_en = translate_text(title_ru, source=source, target=target)
    description_en = (
        translate_text(description_ru, source=source, target=target)
        if description_ru.strip()
        else ""
    )
    return title_en, description_en


def translate_tags(tags_ru: list[str], *, source: str = "ru", target: str = "en") -> list[str]:
    translated: list[str] = []
    for tag in tags_ru:
        value = tag.strip()
        if not value:
            continue
        en = translate_text(value, source=source, target=target)
        translated.append(en if en else value)
    return translated


def translate_category(category_ru: str, *, source: str = "ru", target: str = "en") -> str:
    value = category_ru.strip()
    if not value:
        return ""
    return translate_text(value, source=source, target=target)


def translate_taxonomy(
    category_ru: str,
    tags_ru: list[str],
    *,
    source: str = "ru",
    target: str = "en",
) -> tuple[str, list[str]]:
    return (
        translate_category(category_ru, source=source, target=target),
        translate_tags(tags_ru, source=source, target=target),
    )


def backfill_translations(conn: Any, *, limit: int = 10) -> int:
    rows = conn.execute(
        """
        SELECT id, title_ru, description_ru
        FROM videos
        WHERE title_ru <> ''
          AND (title_en IS NULL OR title_en = '')
        ORDER BY parsed_at ASC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()

    updated = 0
    for row in rows:
        title_en, description_en = translate_fields(
            str(row["title_ru"] or ""),
            str(row["description_ru"] or ""),
        )
        if not title_en:
            print(f"translator: skip {row['id']} — empty translation")
            continue
        conn.execute(
            """
            UPDATE videos
            SET title_en = %s,
                description_en = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (title_en, description_en, str(row["id"])),
        )
        updated += 1
    return updated


def backfill_taxonomy(conn: Any, *, limit: int = 15) -> int:
    rows = conn.execute(
        """
        SELECT id, category, tags
        FROM videos
        WHERE category <> ''
          AND (
            category_en IS NULL
            OR category_en = ''
            OR tags_en IS NULL
            OR tags_en = '[]'::jsonb
          )
        ORDER BY parsed_at ASC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()

    updated = 0
    for row in rows:
        tags = row["tags"] or []
        if isinstance(tags, str):
            tags = json.loads(tags)
        category_en, tags_en = translate_taxonomy(str(row["category"] or ""), list(tags))
        if not category_en and not tags_en:
            print(f"translator: skip taxonomy {row['id']} — empty translation")
            continue
        conn.execute(
            """
            UPDATE videos
            SET category_en = %s,
                tags_en = %s::jsonb,
                updated_at = now()
            WHERE id = %s
            """,
            (category_en, json.dumps(tags_en, ensure_ascii=False), str(row["id"])),
        )
        updated += 1
    return updated
