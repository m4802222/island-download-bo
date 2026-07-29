import unittest

from islandbot.parsing import (
    extract_magnet,
    extract_post_title,
    extract_quark_share,
    message_text_with_links,
)


class ResourcePostParsingTests(unittest.TestCase):
    def test_extracts_title_not_description(self):
        text = """🎬 非份之罪 (2026) 4K WEB-DL 国语+粤语 中字
🕒 发布时间：2026-07-27
描述：这是很长的剧情介绍 (2024)，不能当成标题。
https://pan.quark.cn/s/3b58c126d5ec"""
        self.assertEqual(extract_post_title(text), "非份之罪 (2026)")

    def test_extracts_embedded_links(self):
        self.assertEqual(
            extract_quark_share("夸克：https://pan.quark.cn/s/abc123。"),
            "https://pan.quark.cn/s/abc123",
        )
        self.assertTrue(
            extract_magnet("资源 magnet:?xt=urn:btih:ABC123&dn=test")
            .startswith("magnet:?xt=")
        )

    def test_hidden_telegram_link_is_preserved(self):
        message = {
            "text": "点击夸克",
            "entities": [{"type": "text_link", "url": "https://pan.quark.cn/s/abc"}],
        }
        self.assertIn("https://pan.quark.cn/s/abc", message_text_with_links(message))


if __name__ == "__main__":
    unittest.main()
