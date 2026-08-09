import tempfile
import unittest
from pathlib import Path

from islandbot.media import MediaIdentity
from islandbot.resolver import MediaResolver, ResolutionError
from islandbot.storage import IdentityStore


class FakeMoviePilot:
    def recognize(self, title):
        if "斩神" in title:
            return {"title": "斩神 第2季", "year": 2026, "tmdb_id": 326935}
        return None

    def tmdb(self, tmdb_id, type_name, title=""):
        if tmdb_id == "259231" and type_name == "电视剧":
            return {
                "title": "斩神之凡尘神域",
                "year": 2024,
                "tmdb_id": 259231,
            }
        return None


class ResolverTests(unittest.TestCase):
    def test_automatic_does_not_accept_standalone_second_season_as_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            resolver = MediaResolver(
                FakeMoviePilot(),
                IdentityStore(Path(directory) / "identities.json"),
            )
            with self.assertRaises(ResolutionError):
                resolver.automatic("斩神 第二季 (2026)")

    def test_manual_parent_series_wins_for_same_alias_forever(self):
        with tempfile.TemporaryDirectory() as directory:
            store = IdentityStore(Path(directory) / "identities.json")
            resolver = MediaResolver(FakeMoviePilot(), store)
            confirmed = resolver.reply("斩神 第二季 (2026)", "259231 第2季")
            self.assertEqual(confirmed.tmdb_id, "259231")
            self.assertEqual(confirmed.season, 2)
            again = resolver.automatic("斩神 第二季 (2026)")
            self.assertEqual(again.tmdb_id, "259231")
        self.assertEqual(again.season, 2)

    def test_explicit_season_is_stored_when_source_has_no_season(self):
        with tempfile.TemporaryDirectory() as directory:
            store = IdentityStore(Path(directory) / "identities.json")
            resolver = MediaResolver(FakeMoviePilot(), store)
            resolver.reply("斩神 (2026)", "259231 第2季")
            self.assertIsNone(store.get("斩神 (2026)"))
            self.assertEqual(store.get("斩神 (2026) S02").season, 2)

    def test_first_and_second_season_aliases_do_not_collide(self):
        with tempfile.TemporaryDirectory() as directory:
            store = IdentityStore(Path(directory) / "identities.json")
            store.remember(
                "作品 第一季",
                MediaIdentity("作品", "12345", 2024, season=1),
            )
            self.assertIsNone(store.get("作品 第二季"))
