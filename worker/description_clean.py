"""Strip ebalka user/site attribution from catalog text."""
from __future__ import annotations

import html as html_lib
import re

MIN_DESCRIPTION_LEN = 15

SKIP_TAGS = frozenset(
    {
        "от пользователей ебалки",
        "от пользователей ebalka",
        "from ebalka users",
        "ебалка",
        "ebalka",
    }
)

_ATTRIBUTION_TAIL_RES = (
    re.compile(r"(?i)\s*[-–—]\s*запись вебкам трансляции.*$"),
    re.compile(r"(?i)\s*[-–—]\s*видео\s+с\s*участием начинающей порно модели.*$"),
    re.compile(r"(?i)\s*[-–—]\s*домашнее порно видео.*$"),
    re.compile(r"(?i)\s*от пользовател(?:я|ей)\s+.*$"),
    re.compile(r"(?i)\s*by users?\s+.*$"),
    re.compile(r"(?i), с рубриками:.*$"),
    re.compile(r"(?i)^видео\s+.+\s+от пользовател(?:я|ей).*$"),
    re.compile(r"(?i), в главной роли.*$"),
)


def _normalize_tag(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def filter_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            continue
        cleaned = tag.strip()
        if not cleaned:
            continue
        if _normalize_tag(cleaned) in SKIP_TAGS:
            continue
        if "от пользовател" in _normalize_tag(cleaned):
            continue
        if cleaned not in out:
            out.append(cleaned)
    return out


def clean_description(text: str) -> str:
    text = html_lib.unescape(text or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()

    for pattern in _ATTRIBUTION_TAIL_RES:
        text = pattern.sub("", text).strip()

    text = re.sub(r"\s*[-–—]\s*$", "", text).strip(" ,;.-")
    return text.strip()


def is_usable_description(text: str) -> bool:
    value = clean_description(text)
    if len(value) < MIN_DESCRIPTION_LEN:
        return False
    lower = value.casefold()
    if "от пользовател" in lower and len(value) < 120:
        return False
    if lower in SKIP_TAGS:
        return False
    return True
