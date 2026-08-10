import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from islandbot.handlers import BotHandlers


class HandlerTests(unittest.TestCase):
    def _runtime(self):
        runtime = SimpleNamespace(
            OWNER=1,
            OFFSET=0,
            MAX_ACTIVE_DOWNLOADS=2,
            ACCOUNT_PENDING={},
            QUARK_TITLE_PENDING={},
            message_text_with_links=lambda message: message.get("text", ""),
            extract_quark_share=lambda text: None,
            extract_magnet=lambda text: None,
            send=Mock(),
            telegram=Mock(),
            answer=Mock(),
            home=Mock(),
            time=SimpleNamespace(time=lambda: 1),
        )
        return runtime

    def test_help_message_is_routed(self):
        runtime = self._runtime()
        handlers = BotHandlers(lambda: runtime)

        handlers.handle({
            "update_id": 7,
            "message": {"from": {"id": 1}, "chat": {"id": 1}, "text": "/help"},
        })

        self.assertEqual(runtime.OFFSET, 8)
        runtime.send.assert_called_once()
        self.assertIn("magnet", runtime.send.call_args.args[1])

    def test_legacy_tasks_command_is_routed(self):
        runtime = self._runtime()
        runtime.show_tasks = Mock()
        handlers = BotHandlers(lambda: runtime)

        self.assertTrue(handlers.legacy_command(1, "/tasks"))
        runtime.show_tasks.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
