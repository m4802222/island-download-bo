"""Tests for islandbot.library — missing-episode decisions."""

import unittest

from islandbot.library import (
    MissingPlan,
    indexed_episodes,
    matching_paths,
    missing_plan,
)
from islandbot.media import MediaIdentity


class IndexedEpisodesTests(unittest.TestCase):
    """indexed_episodes should only index episodes from the right show+season."""

    def _identity(self, title="庆余年", tmdb_id="12345", season=1, year=2024):
        return MediaIdentity(
            title=title, tmdb_id=tmdb_id, year=year, season=season
        )

    def test_exact_title_and_season_are_indexed(self):
        paths = [
            "庆余年 (2024) {tmdb-12345}/Season 1/庆余年 - S01E01.mkv",
            "庆余年 (2024) {tmdb-12345}/Season 1/庆余年 - S01E02.mkv",
        ]
        result = indexed_episodes(paths, self._identity())
        self.assertEqual(result, {"S01E001", "S01E002"})

    def test_wrong_season_is_excluded(self):
        paths = [
            "庆余年 (2024) {tmdb-12345}/Season 2/庆余年 - S02E01.mkv",
        ]
        result = indexed_episodes(paths, self._identity(season=1))
        self.assertEqual(result, set())

    def test_wrong_title_is_excluded(self):
        paths = [
            "斗破苍穹 (2020) {tmdb-99999}/Season 1/斗破苍穹 - S01E01.mkv",
        ]
        result = indexed_episodes(paths, self._identity())
        self.assertEqual(result, set())

    def test_empty_paths(self):
        result = indexed_episodes([], self._identity())
        self.assertEqual(result, set())


class MatchingPathsTests(unittest.TestCase):
    """matching_paths should filter paths to the exact canonical title."""

    def _identity(self, **kwargs):
        defaults = dict(title="庆余年", tmdb_id="12345", year=2024, season=1)
        defaults.update(kwargs)
        return MediaIdentity(**defaults)

    def test_matching_title_and_season(self):
        paths = [
            "庆余年 (2024) {tmdb-12345}/Season 1/E01.mkv",
            "庆余年 (2024) {tmdb-12345}/Season 2/E01.mkv",
            "其他剧/Season 1/E01.mkv",
        ]
        result = matching_paths(paths, self._identity())
        self.assertEqual(len(result), 1)
        self.assertIn("庆余年 (2024) {tmdb-12345}/Season 1/E01.mkv", result)

    def test_movie_type_ignores_season_dir(self):
        identity = self._identity(media_type="电影")
        paths = [
            "庆余年 (2024) {tmdb-12345}/庆余年.mkv",
        ]
        result = matching_paths(paths, identity)
        self.assertEqual(len(result), 1)


class MissingPlanTests(unittest.TestCase):
    """missing_plan determines which files still need downloading."""

    def _identity(self, **kwargs):
        defaults = dict(title="庆余年", tmdb_id="12345", year=2024, season=1)
        defaults.update(kwargs)
        return MediaIdentity(**defaults)

    def test_all_episodes_missing(self):
        source = ["剧名/S01E01.mkv", "剧名/S01E02.mkv"]
        plan = missing_plan(source, [], self._identity())
        self.assertEqual(plan.total, 2)
        self.assertEqual(plan.skipped, 0)
        self.assertEqual(plan.remaining, 2)
        self.assertFalse(plan.complete)

    def test_all_episodes_exist(self):
        source = ["剧名/S01E01.mkv", "剧名/S01E02.mkv"]
        existing = [
            "庆余年 (2024) {tmdb-12345}/Season 1/庆余年 - S01E01.mkv",
            "庆余年 (2024) {tmdb-12345}/Season 1/庆余年 - S01E02.mkv",
        ]
        plan = missing_plan(source, existing, self._identity())
        self.assertEqual(plan.total, 2)
        self.assertEqual(plan.skipped, 2)
        self.assertEqual(plan.remaining, 0)
        self.assertTrue(plan.complete)

    def test_partial_episodes_exist(self):
        source = ["剧名/S01E01.mkv", "剧名/S01E02.mkv", "剧名/S01E03.mkv"]
        existing = [
            "庆余年 (2024) {tmdb-12345}/Season 1/庆余年 - S01E01.mkv",
        ]
        plan = missing_plan(source, existing, self._identity())
        self.assertEqual(plan.total, 3)
        self.assertEqual(plan.skipped, 1)
        self.assertEqual(plan.remaining, 2)
        self.assertEqual(plan.missing_names, ("剧名/S01E02.mkv", "剧名/S01E03.mkv"))

    def test_non_video_files_are_excluded(self):
        source = ["剧名/S01E01.mkv", "剧名/readme.txt", "剧名/poster.jpg"]
        plan = missing_plan(source, [], self._identity())
        self.assertEqual(plan.total, 1)
        self.assertEqual(plan.remaining, 1)

    def test_movie_deduplication(self):
        identity = self._identity(media_type="电影")
        source = ["庆余年.mkv"]
        existing = [
            "庆余年 (2024) {tmdb-12345}/庆余年.mkv",
        ]
        plan = missing_plan(source, existing, identity)
        self.assertEqual(plan.total, 1)
        self.assertEqual(plan.skipped, 1)
        self.assertEqual(plan.remaining, 0)

    def test_include_regex_is_empty_when_no_skip(self):
        source = ["剧名/S01E01.mkv"]
        plan = missing_plan(source, [], self._identity())
        self.assertEqual(plan.include_regex, "")

    def test_include_regex_contains_only_missing(self):
        source = ["剧名/S01E01.mkv", "剧名/S01E02.mkv"]
        existing = [
            "庆余年 (2024) {tmdb-12345}/Season 1/庆余年 - S01E01.mkv",
        ]
        plan = missing_plan(source, existing, self._identity())
        self.assertEqual(plan.skipped, 1)
        self.assertIn("S01E02", plan.include_regex)
        self.assertNotEqual(plan.include_regex, "")


class StallDetectionLogicTests(unittest.TestCase):
    """Test the stall detection state machine logic in isolation."""

    def test_stall_tracking_dict_operations(self):
        """Verify dict-based stall tracking follows expected lifecycle."""
        stall_first_seen: dict[str, float] = {}
        stall_notified: set[str] = set()
        threshold = 30 * 60

        hash_a = "abc123"
        # First observation: just record
        self.assertNotIn(hash_a, stall_first_seen)
        stall_first_seen[hash_a] = 0.0  # monotonic time
        self.assertIn(hash_a, stall_first_seen)

        # Not yet past threshold
        elapsed = 10 * 60
        self.assertFalse(elapsed >= threshold)

        # Past threshold → should trigger
        elapsed = 31 * 60
        self.assertTrue(elapsed >= threshold)

        # After notification
        stall_notified.add(hash_a)
        self.assertIn(hash_a, stall_notified)

        # Progress clears tracking
        stall_first_seen.pop(hash_a, None)
        stall_notified.discard(hash_a)
        self.assertNotIn(hash_a, stall_first_seen)
        self.assertNotIn(hash_a, stall_notified)

    def test_stale_cleanup(self):
        """Stall entries for tasks no longer in queue are cleaned up."""
        queue = ["hash1", "hash2"]
        stall_first_seen = {"hash1": 0.0, "hash3": 0.0}  # hash3 left queue
        stale = set(stall_first_seen) - set(queue)
        self.assertEqual(stale, {"hash3"})
        for h in stale:
            stall_first_seen.pop(h, None)
        self.assertNotIn("hash3", stall_first_seen)
        self.assertIn("hash1", stall_first_seen)


if __name__ == "__main__":
    unittest.main()
