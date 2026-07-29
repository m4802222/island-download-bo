import os
import unittest
from unittest.mock import patch

from islandbot.config import Settings


class SettingsTests(unittest.TestCase):
    def test_required_values_and_limits_are_validated(self):
        environment = {
            "BOT_TOKEN": "token",
            "OWNER_ID": "123",
            "QBIT_USERNAME": "admin",
            "QBIT_PASSWORD": "secret",
            "MIN_FREE_GIB": "5",
            "MAX_ACTIVE_DOWNLOADS": "3",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.owner_id, 123)
        self.assertEqual(settings.min_free_gib, 5)
        self.assertEqual(settings.max_active_downloads, 3)

    def test_invalid_queue_limit_stops_startup(self):
        environment = {
            "BOT_TOKEN": "token",
            "OWNER_ID": "123",
            "QBIT_USERNAME": "admin",
            "QBIT_PASSWORD": "secret",
            "MAX_ACTIVE_DOWNLOADS": "0",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "不能小于"):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
