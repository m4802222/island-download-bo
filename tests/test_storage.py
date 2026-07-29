import json
import tempfile
import unittest
from pathlib import Path

from islandbot.media import MediaIdentity
from islandbot.storage import IdentityStore, JsonStore


class StorageTests(unittest.TestCase):
    def test_corrupt_json_uses_safe_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(JsonStore(path, []).load(), [])

    def test_manual_identity_is_season_aware_and_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identities.json"
            store = IdentityStore(path)
            season_two = MediaIdentity(
                "斩神之凡尘神域",
                "259231",
                2024,
                2,
                "电视剧",
            )
            store.remember("斩神 第二季 (2026)", season_two)

            restored = IdentityStore(path).get("斩神 第二季 (2026)")
            self.assertEqual(restored, season_two)
            self.assertIsNone(IdentityStore(path).get("斩神 第一季 (2024)"))

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 2)


if __name__ == "__main__":
    unittest.main()
