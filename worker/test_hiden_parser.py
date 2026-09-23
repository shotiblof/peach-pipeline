from __future__ import annotations

import unittest

from hiden_parser import (
    clean_hiden_description,
    extract_preview_clip,
    hiden_video_id,
    is_hiden_video_id,
    parse_list_cards,
    parse_tags,
    parse_video_page,
    HidenCard,
    extract_hiden_mp4,
)

LIST_SNIPPET = """
<article class="card" data-video-id="5064">
  <a href="/porno/byvshiy-zaklyuchennyy-trahaet-russkuyu-blyad-i-konchaet-ey-v-rot/">
    <h3 class="card-title clamp-2">Бывший заключённый трахает блядь</h3>
  </a>
</article>
"""

PAGE_SNIPPET = """
<span id="videoTitleText">Бывший заключённый трахает блядь и кончает ей в рот</span>
<span id="videoCategoryText">Домашнее порно</span>
<div class="description-body" id="videoDescriptionBody"
  data-raw-description="Чистое описание без мусора. Рекомендуем так же к просмотру наши категории: на улице и куколды!">
</div>
<div class="tags-row" id="videoTagsRow">
  <a class="tag-chip" href="/tag/konchaet/">Кончает</a>
  <a class="tag-chip" href="/tag/minet/">Минет</a>
</div>
<meta property="video:duration" content="441">
<meta property="og:image" content="https://media.hiden.live/previews/variants/preview_pad_x.jpg">
https://media.hiden.live/videos/00000401_b_11.mp4
https://media.hiden.live/previews/clips/preview_clip_abc.mp4
"""


class HidenParserTests(unittest.TestCase):
    def test_hiden_video_id(self) -> None:
        self.assertEqual(hiden_video_id("5064"), "h5064")
        self.assertTrue(is_hiden_video_id("h5064"))
        self.assertFalse(is_hiden_video_id("5064"))

    def test_parse_list_cards(self) -> None:
        cards = parse_list_cards(LIST_SNIPPET)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].numeric_id, "5064")
        self.assertIn("/porno/", cards[0].video_path)

    def test_clean_description_strips_recommend_tail(self) -> None:
        raw = "Чистое описание. Рекомендуем так же к просмотру наши категории: foo!"
        cleaned = clean_hiden_description(raw)
        self.assertIn("Чистое описание", cleaned)
        self.assertNotIn("Рекомендуем", cleaned)

    def test_parse_video_page(self) -> None:
        card = HidenCard("5064", "/porno/test/", "fallback")
        video = parse_video_page(PAGE_SNIPPET, card, "https://hiden.live")
        self.assertEqual(video.id, "h5064")
        self.assertIn("заключённый", video.title_ru)
        self.assertEqual(video.category, "Домашнее порно")
        self.assertEqual(video.tags, ["Кончает", "Минет"])
        self.assertEqual(video.duration_seconds, 441)
        self.assertNotIn("Рекомендуем", video.description_ru)

    def test_extract_mp4(self) -> None:
        url = extract_hiden_mp4(PAGE_SNIPPET)
        self.assertIsNotNone(url)
        assert url is not None
        self.assertIn("media.hiden.live/videos/", url)

    def test_extract_preview_clip_prefers_video_id(self) -> None:
        html = PAGE_SNIPPET + """
https://media.hiden.live/previews/clips/clip_9999_wrong.mp4
https://media.hiden.live/previews/clips/clip_5064_1781455376.mp4
"""
        clip = extract_preview_clip(html, "5064")
        self.assertIsNotNone(clip)
        assert clip is not None
        self.assertIn("clip_5064_", clip)


if __name__ == "__main__":
    unittest.main()
