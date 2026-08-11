import unittest

from islandbot.cleanup import (
    is_brush_task,
    safe_to_cleanup,
    selected_video_paths,
    successful_source_paths,
)


class CleanupTests(unittest.TestCase):
    def test_existing_school_rescue_category_is_protected_as_brush(self):
        self.assertTrue(is_brush_task({"category": "学校救号"}))

    def setUp(self):
        self.task = {
            "progress": 1,
            "state": "stoppedUP",
            "save_path": "/downloads/complete/国产剧集",
        }
        self.files = [
            {"name": "剧名/S01E01.mkv", "priority": 1},
            {"name": "剧名/readme.txt", "priority": 1},
        ]

    def test_successful_history_paths_are_normalized(self):
        paths = successful_source_paths(
            [
                (
                    '["//downloads/complete/国产剧集/剧名/S01E01.mkv"]',
                    "/Media/国产剧集/剧名/Season 1/E01.mkv",
                )
            ]
        )
        self.assertEqual(
            paths,
            {"/downloads/complete/国产剧集/剧名/S01E01.mkv"},
        )

    def test_all_selected_videos_must_be_transferred(self):
        sources = {"/downloads/complete/国产剧集/剧名/S01E01.mkv"}
        self.assertTrue(safe_to_cleanup(self.task, self.files, sources))
        self.files.append({"name": "剧名/S01E02.mkv", "priority": 1})
        self.assertFalse(safe_to_cleanup(self.task, self.files, sources))

    def test_unselected_video_does_not_block_cleanup(self):
        self.files.append({"name": "剧名/S01E02.mkv", "priority": 0})
        paths = selected_video_paths(self.task, self.files)
        self.assertEqual(
            paths,
            {"/downloads/complete/国产剧集/剧名/S01E01.mkv"},
        )

    def test_active_or_incomplete_task_is_never_deleted(self):
        sources = {"/downloads/complete/国产剧集/剧名/S01E01.mkv"}
        for state, progress in (("checkingUP", 1), ("downloading", 0.5)):
            task = {**self.task, "state": state, "progress": progress}
            self.assertFalse(safe_to_cleanup(task, self.files, sources))

    def test_completed_ordinary_seeding_states_can_be_deleted(self):
        sources = {"/downloads/complete/国产剧集/剧名/S01E01.mkv"}
        for state in ("stalledUP", "uploading", "forcedUP", "queuedUP"):
            task = {**self.task, "state": state}
            self.assertTrue(safe_to_cleanup(task, self.files, sources))

    def test_brush_tags_are_never_deleted(self):
        sources = {"/downloads/complete/国产剧集/剧名/S01E01.mkv"}
        for tags in ("刷流", "MOVIEPILOT, 刷流-452ba49e"):
            task = {**self.task, "tags": tags}
            self.assertTrue(is_brush_task(task))
            self.assertFalse(safe_to_cleanup(task, self.files, sources))

    def test_brush_category_is_never_deleted(self):
        sources = {"/downloads/complete/国产剧集/剧名/S01E01.mkv"}
        task = {**self.task, "category": "刷流"}
        self.assertTrue(is_brush_task(task))
        self.assertFalse(safe_to_cleanup(task, self.files, sources))

    def test_history_without_destination_is_not_confirmation(self):
        sources = successful_source_paths(
            [('["/downloads/complete/剧名/S01E01.mkv"]', None)]
        )
        self.assertEqual(sources, set())


if __name__ == "__main__":
    unittest.main()
