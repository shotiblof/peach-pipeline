"""Debug ebalka parse fields."""
from __future__ import annotations

import os
from pathlib import Path

for line in Path(__file__).resolve().parent.parent.joinpath(".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ[k] = v

import httpx
from ebalka_parser import HEADERS, _fetch, parse_cards, parse_video_page
from peach_db import abs_url

origin = "https://a.ebalka.love"
with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
    html = _fetch(client, abs_url(origin, "/latest-videos/1/"))
    for card in parse_cards(html, origin)[:2]:
        page = _fetch(client, abs_url(origin, card.video_path))
        v = parse_video_page(page, card, origin)
        print("---", v.id, v.title_ru[:50])
        print("category:", repr(v.category))
        print("tags:", v.tags[:6])
        print("desc:", repr(v.description_ru[:100]))
