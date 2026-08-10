import unittest
from unittest.mock import Mock

from islandbot.services.telegram_ui import TelegramUI


class TelegramUITests(unittest.TestCase):
    def test_category_keyboard_reads_current_categories(self):
        ui = TelegramUI(
            Mock(),
            [],
            Mock(),
            Mock(),
            lambda: ["华语电影", "日韩电影", "海外电影", "华语剧集"],
            lambda: 2,
        )
        rows = ui.category_keyboard()
        self.assertEqual([item["text"] for item in rows[0]], ["华语电影", "日韩电影", "海外电影"])
        self.assertEqual(rows[-2][0]["callback_data"], "category:__auto__")

    def test_expired_callback_is_ignored(self):
        telegram = Mock(side_effect=RuntimeError("query is too old"))
        ui = TelegramUI(telegram, [], Mock(), Mock(), lambda: [], lambda: 2)
        ui.answer("callback-id")
        telegram.assert_called_once_with(
            "answerCallbackQuery", {"callback_query_id": "callback-id"}
        )


if __name__ == "__main__":
    unittest.main()
