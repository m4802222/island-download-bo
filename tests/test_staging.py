import unittest

from islandbot.staging import is_ready_path, ready_save_path


class StagingPathTests(unittest.TestCase):
    def test_category_save_path_moves_from_staging_to_ready_tree(self):
        self.assertEqual(
            ready_save_path(
                "/downloads/incoming/华语剧集",
                "/downloads/incoming/islandbot",
                "/downloads/complete/islandbot",
            ),
            "/downloads/complete/华语剧集",
        )

    def test_unrelated_save_path_is_rejected(self):
        self.assertEqual(
            ready_save_path(
                "/downloads/other",
                "/downloads/incoming/islandbot",
                "/downloads/complete/islandbot",
            ),
            "",
        )

    def test_ready_path_detection_uses_ready_root(self):
        self.assertTrue(
            is_ready_path(
                "/downloads/complete/华语剧集",
                "/downloads/complete/islandbot",
            )
        )
        self.assertFalse(
            is_ready_path(
                "/downloads/incoming/华语剧集",
                "/downloads/complete/islandbot",
            )
        )
