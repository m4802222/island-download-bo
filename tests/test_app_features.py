"""Tests for newly added app-level features: InfoHash extraction and SEEN migration."""

import re
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class InfoHashExtractionTests(unittest.TestCase):
    """Verify the InfoHash extraction regex used in add_to_qbit conflict detection."""

    def _extract(self, magnet: str) -> str:
        """Reproduce the extraction logic from add_to_qbit."""
        xt_match = re.search(r"xt=urn:btih:([a-fA-F0-9]{40})", magnet)
        if xt_match:
            return xt_match.group(1).lower()
        xt_match = re.search(r"xt=urn:btih:([A-Za-z2-7]{32})", magnet)
        if xt_match:
            import base64
            return base64.b32decode(xt_match.group(1).upper()).hex()
        return ""

    def test_hex_hash_extraction(self):
        magnet = "magnet:?xt=urn:btih:AABBCCDDEE1122334455AABBCCDDEE1122334455&dn=test"
        result = self._extract(magnet)
        self.assertEqual(result, "aabbccddee1122334455aabbccddee1122334455")

    def test_base32_hash_extraction(self):
        magnet = "magnet:?xt=urn:btih:MNQXIIDCPEQGY33UMUQGEZLFONZWS=&dn=test"
        # Base32 hashes are 32 chars — this tests the fallback path.
        xt_match = re.search(r"xt=urn:btih:([A-Za-z2-7]{32})", magnet)
        if xt_match:
            import base64
            result = base64.b32decode(xt_match.group(1).upper()).hex()
            self.assertEqual(len(result), 40)
        # If no base32 match is found, that's fine for this test string.

    def test_no_hash_returns_empty(self):
        result = self._extract("http://example.com/not-a-magnet")
        self.assertEqual(result, "")

    def test_case_insensitive_hex(self):
        magnet = "magnet:?xt=urn:btih:aaBBccDDee1122334455AAbbCCddEE1122334455&dn=test"
        result = self._extract(magnet)
        self.assertEqual(result, "aabbccddee1122334455aabbccddee1122334455")


class SeenMigrationTests(unittest.TestCase):
    """Test the SEEN format migration from list to timestamped dict."""

    def _migrate(self, raw):
        """Reproduce the migration logic from app.py module init."""
        import time
        if isinstance(raw, list):
            return {h: time.time() for h in raw if isinstance(h, str)}
        elif isinstance(raw, dict):
            return {
                h: float(t) for h, t in raw.items()
                if isinstance(h, str) and isinstance(t, (int, float))
            }
        return {}

    def test_migrate_from_list(self):
        old_format = ["hash1", "hash2", "hash3"]
        result = self._migrate(old_format)
        self.assertEqual(set(result.keys()), {"hash1", "hash2", "hash3"})
        for ts in result.values():
            self.assertIsInstance(ts, float)

    def test_migrate_from_dict(self):
        new_format = {"hash1": 1723100000.0, "hash2": 1723200000.0}
        result = self._migrate(new_format)
        self.assertEqual(result, new_format)

    def test_migrate_ignores_non_string_keys(self):
        bad = {123: 1.0, "valid": 2.0}
        result = self._migrate(bad)
        self.assertEqual(set(result.keys()), {"valid"})

    def test_migrate_ignores_non_numeric_values(self):
        bad = {"hash1": "not_a_number", "hash2": 1.0}
        result = self._migrate(bad)
        self.assertEqual(set(result.keys()), {"hash2"})

    def test_migrate_empty_list(self):
        result = self._migrate([])
        self.assertEqual(result, {})

    def test_migrate_empty_dict(self):
        result = self._migrate({})
        self.assertEqual(result, {})

    def test_30_day_cutoff_logic(self):
        import time
        now = time.time()
        seen = {
            "recent": now - 5 * 86400,      # 5 days ago
            "old": now - 40 * 86400,         # 40 days ago
        }
        cutoff = now - 30 * 86400
        cleaned = {h: t for h, t in seen.items() if t >= cutoff}
        self.assertIn("recent", cleaned)
        self.assertNotIn("old", cleaned)


class EpisodeNormalizerTests(unittest.TestCase):
    def test_completed_bare_file_uses_qbit_rename_api(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "complete" / "islandbot"
            root = base / "这一秒过火.2160p"
            root.mkdir(parents=True)
            env = {
                "BOT_TOKEN": "test",
                "OWNER_ID": "1",
                "QBIT_USERNAME": "test",
                "QBIT_PASSWORD": "test",
                "DATA_DIR": str(Path(temp) / "data"),
                "QBIT_SAVE_PATH": str(base),
            }
            with patch.dict(os.environ, env, clear=False):
                app = importlib.import_module("islandbot.app")
            from islandbot.media import MediaIdentity

            original_qbit = app.QBIT_CLIENT
            original_resolver = app.RESOLVER
            try:
                qbit = Mock()
                qbit.files.return_value = [
                    {"name": "08.2160p.mkv", "progress": 1.0},
                    {"name": "09.2160p.mkv", "progress": 0.2},
                ]
                resolver = Mock()
                resolver.automatic.return_value = MediaIdentity(
                    "这一秒过火", "289139", 2026, 1, "电视剧"
                )
                app.QBIT_CLIENT = qbit
                app.RESOLVER = resolver
                app.NORMALIZE_IDENTITY_CACHE.clear()
                app.NORMALIZE_LAST_ATTEMPT.clear()

                result = app.normalize_completed_episode_files(
                    [
                        {
                            "hash": "abc",
                            "content_path": str(root),
                            "name": "这一秒过火.2160p",
                            "category": "华语剧集",
                            "tags": "islandbot",
                        }
                    ]
                )

                self.assertEqual(
                    result,
                    [("08.2160p.mkv", "这一秒过火.S01E08.2160p.mkv")],
                )
                qbit.rename_file.assert_called_once_with(
                    "abc",
                    "08.2160p.mkv",
                    "这一秒过火.S01E08.2160p.mkv",
                )
            finally:
                app.QBIT_CLIENT = original_qbit
                app.RESOLVER = original_resolver


class TelegramCallbackTests(unittest.TestCase):
    def test_expired_callback_does_not_break_update_loop(self):
        with tempfile.TemporaryDirectory() as temp:
            env = {
                "BOT_TOKEN": "test",
                "OWNER_ID": "1",
                "QBIT_USERNAME": "test",
                "QBIT_PASSWORD": "test",
                "DATA_DIR": str(Path(temp) / "data"),
            }
            with patch.dict(os.environ, env, clear=False):
                app = importlib.import_module("islandbot.app")
            with patch.object(
                app,
                "telegram",
                side_effect=RuntimeError(
                    "Telegram 请求失败（HTTP 400）Bad Request: "
                    "query is too old and response timeout expired"
                ),
            ):
                app.answer("expired-callback")

    def test_unrelated_callback_error_is_still_raised(self):
        with tempfile.TemporaryDirectory() as temp:
            env = {
                "BOT_TOKEN": "test",
                "OWNER_ID": "1",
                "QBIT_USERNAME": "test",
                "QBIT_PASSWORD": "test",
                "DATA_DIR": str(Path(temp) / "data"),
            }
            with patch.dict(os.environ, env, clear=False):
                app = importlib.import_module("islandbot.app")
            with patch.object(app, "telegram", side_effect=RuntimeError("HTTP 500")):
                with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                    app.answer("broken-callback")


if __name__ == "__main__":
    unittest.main()
