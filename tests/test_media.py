import unittest

from islandbot.library import indexed_episodes, missing_plan
from islandbot.media import (
    MediaIdentity,
    alias_key,
    bare_episode_number,
    episode_key,
    explicit_seasons,
    normalize_bare_episode_filename,
    parse_identity_label,
    season_number,
)


class MediaParsingTests(unittest.TestCase):
    def test_season_variants(self):
        self.assertEqual(season_number("斩神 第二季 (2026)"), 2)
        self.assertEqual(season_number("Season 5"), 5)
        self.assertEqual(season_number("Demon.Slayer.S03E02"), 3)
        self.assertEqual(season_number("没有季数"), 1)

    def test_bare_episode_requires_known_season(self):
        self.assertIsNone(episode_key("08.mkv"))
        self.assertEqual(episode_key("08.mkv", 2), "S02E008")
        self.assertEqual(episode_key("第 8 集.mp4", 2), "S02E008")
        self.assertEqual(episode_key("Show.S02E08.mkv"), "S02E008")

    def test_bare_episode_filename_is_normalized_for_confirmed_tv(self):
        source = "08.2160p.HD国语中字无水印.mkv"
        self.assertEqual(bare_episode_number(source), 8)
        self.assertEqual(
            normalize_bare_episode_filename(source, "这一秒过火", 1),
            "这一秒过火.S01E08.2160p.HD国语中字无水印.mkv",
        )

    def test_normalizer_skips_incomplete_and_explicit_names(self):
        self.assertIsNone(
            normalize_bare_episode_filename("08.mkv.!qB", "这一秒过火", 1)
        )
        self.assertIsNone(
            normalize_bare_episode_filename("Show.S01E08.mkv", "这一秒过火", 1)
        )
        self.assertIsNone(
            normalize_bare_episode_filename("08.mkv", "", 1)
        )

    def test_explicit_seasons_are_read_from_release_names(self):
        self.assertEqual(explicit_seasons("Flex.x.Cop.S02E01.1080p.mkv"), {2})
        self.assertEqual(explicit_seasons("S01E01-S02E02.mkv"), {1, 2})
        self.assertEqual(explicit_seasons("01.mkv"), set())

    def test_identity_round_trip(self):
        identity = parse_identity_label(
            "斩神之凡尘神域 (2024) {tmdb-259231} S02"
        )
        self.assertEqual(identity, MediaIdentity("斩神之凡尘神域", "259231", 2024, 2))

    def test_movie_label_has_no_fake_season(self):
        identity = MediaIdentity("寒战", "137409", 2012, media_type="电影")
        self.assertEqual(identity.task_label, "寒战 (2012) {tmdb-137409}")
        self.assertEqual(parse_identity_label(identity.task_label), identity)

    def test_alias_keeps_season(self):
        self.assertNotEqual(alias_key("斩神 第一季"), alias_key("斩神 第二季"))


class MissingEpisodeTests(unittest.TestCase):
    def setUp(self):
        self.identity = MediaIdentity("斩神之凡尘神域", "259231", 2024, 2)
        self.drive = [
            "国产动漫/斩神之凡尘神域 (2024)/Season 1/斩神之凡尘神域 - S01E01.mp4",
            "国产动漫/斩神之凡尘神域 (2024)/Season 2/斩神之凡尘神域 - S02E01.mkv",
            "国产动漫/斩神之凡尘神域 (2024)/Season 2/斩神之凡尘神域 - S02E02.mkv",
        ]

    def test_indexes_exact_season_only(self):
        self.assertEqual(
            indexed_episodes(self.drive, self.identity),
            {"S02E001", "S02E002"},
        )

    def test_bare_source_names_use_confirmed_season(self):
        plan = missing_plan(["01.mkv", "02.mkv", "03.mkv"], self.drive, self.identity)
        self.assertEqual(plan.skipped, 2)
        self.assertEqual(plan.missing_names, ("03.mkv",))

    def test_first_season_does_not_cancel_second(self):
        identity = MediaIdentity("斩神之凡尘神域", "259231", 2024, 2)
        first_only = [self.drive[0]]
        plan = missing_plan(["01.mkv", "02.mkv"], first_only, identity)
        self.assertEqual(plan.skipped, 0)
        self.assertEqual(plan.remaining, 2)

    def test_second_season_bare_files_skip_only_existing_second_season(self):
        identity = MediaIdentity("斩神之凡尘神域", "259231", 2024, season=2)
        plan = missing_plan(
            ["01.mkv", "02.mkv", "08.mkv"],
            [
                "国产动漫/斩神之凡尘神域 (2024)/Season 1/斩神之凡尘神域 - S01E01.mkv",
                "国产动漫/斩神之凡尘神域 (2024)/Season 2/斩神之凡尘神域 - S02E01.mkv",
                "国产动漫/斩神之凡尘神域 (2024)/Season 2/斩神之凡尘神域 - S02E02.mkv",
            ],
            identity,
        )
        self.assertEqual(plan.skipped, 2)
        self.assertEqual(plan.missing_names, ("08.mkv",))


if __name__ == "__main__":
    unittest.main()
