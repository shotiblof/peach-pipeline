from __future__ import annotations

import unittest

from namevids_client import (
    build_caption,
    build_namevids_caption_link,
    build_web_video_link,
    generate_fa,
    resolve_fa,
    resolve_user_agent,
    sanitize_namevids_title,
)


class NamevidsCaptionTests(unittest.TestCase):
    def test_build_web_video_link(self) -> None:
        link = build_web_video_link({"webapp.base_url": "https://peach.example.com"}, "264487")
        self.assertEqual(link, "https://peach.example.com/watch/264487")

    def test_build_namevids_caption_link_from_settings(self) -> None:
        link = build_namevids_caption_link(
            {"namevids.caption_link": "https://t.me/jesovixxx"},
            "264487",
        )
        self.assertEqual(link, "https://t.me/jesovixxx")

    def test_build_namevids_caption_link_default(self) -> None:
        link = build_namevids_caption_link({}, "264487")
        self.assertEqual(link, "https://t.me/jesovixxx")

    def test_build_caption_format(self) -> None:
        settings = {"namevids.caption_link": "https://t.me/jesovixxx"}
        link = build_namevids_caption_link(settings, "264487")
        caption = build_caption(
            settings,
            {
                "id": "264487",
                "description_en": "Hot clip with a long English description for the caption.",
            },
            link,
        )
        self.assertTrue(caption.startswith("full video\n"))
        self.assertIn(link, caption)
        self.assertIn("Hot clip", caption)
        self.assertLessEqual(len(caption), 200)

    def test_build_caption_prefers_english(self) -> None:
        settings = {"namevids.caption_link": "https://t.me/jesovixxx"}
        link = build_namevids_caption_link(settings, "1")
        caption = build_caption(
            settings,
            {"description_en": "English text", "description_ru": "Русский текст"},
            link,
        )
        self.assertIn("English text", caption)
        self.assertNotIn("Русский", caption)

    def test_generate_fa_deterministic_per_seed(self) -> None:
        a = generate_fa(seed="acct-1")
        b = generate_fa(seed="acct-1")
        c = generate_fa(seed="acct-2")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_resolve_fa_uses_metadata_seed(self) -> None:
        meta = {"fa_seed": "stable-seed"}
        self.assertEqual(resolve_fa(meta, login_name="x"), resolve_fa(meta, login_name="y"))

    def test_resolve_user_agent_explicit(self) -> None:
        ua = resolve_user_agent({"user_agent": "CustomUA/1.0"}, login_name="any")
        self.assertEqual(ua, "CustomUA/1.0")

    def test_sanitize_namevids_title_prefers_english(self) -> None:
        title = sanitize_namevids_title(
            title_ru="Блондинка сосёт",
            title_en="Fucking at home",
            video_id="265352",
        )
        self.assertEqual(title, "Fucking at home")

    def test_sanitize_namevids_title_transliterates_russian(self) -> None:
        title = sanitize_namevids_title(
            title_ru="Домашний секс - часть 2",
            title_en="",
            video_id="123",
        )
        self.assertEqual(title, "domashniy seks")
        self.assertLessEqual(len(title), 50)

    def test_sanitize_namevids_title_falls_back_to_video_id(self) -> None:
        title = sanitize_namevids_title(title_ru="###", title_en="", video_id="18970")
        self.assertEqual(title, "18970")


if __name__ == "__main__":
    unittest.main()
