import os
import unittest
from unittest.mock import patch

from islandbot.config import Settings, explicit_web_port


class SettingsTests(unittest.TestCase):
    def test_public_url_displays_default_https_port(self):
        self.assertEqual(
            explicit_web_port("https://emby.6668777.xyz"),
            "https://emby.6668777.xyz:443",
        )

    def test_public_url_keeps_explicit_port(self):
        self.assertEqual(
            explicit_web_port("http://207.58.173.248:8096"),
            "http://207.58.173.248:8096",
        )

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
        self.assertEqual(settings.qbit_save_path, "/downloads/complete/islandbot")
        self.assertTrue(settings.auto_cleanup_completed)
        self.assertEqual(settings.cleanup_interval_seconds, 60)

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

    def test_invalid_cleanup_switch_stops_startup(self):
        environment = {
            "BOT_TOKEN": "token",
            "OWNER_ID": "123",
            "QBIT_USERNAME": "admin",
            "QBIT_PASSWORD": "secret",
            "AUTO_CLEANUP_COMPLETED": "sometimes",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "true 或 false"):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
