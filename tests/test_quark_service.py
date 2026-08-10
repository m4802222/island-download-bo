import unittest
from unittest.mock import Mock

from islandbot.services.quark import QuarkService


class QuarkServiceTests(unittest.TestCase):
    def test_task_keeps_confirmed_season_in_aria_path(self):
        service = QuarkService(Mock(), "/IslandDownload", Mock(), Mock())
        task = service.task(
            "https://pan.quark.cn/s/demo",
            "夸克任务-0810",
            "财阀X刑警 (2024) {tmdb-220074} S02",
            "E01|E02",
        )
        self.assertEqual(task["savepath"], "/IslandDownload/夸克任务-0810")
        self.assertEqual(
            task["addition"]["aria2"]["save_path"],
            "incoming/财阀X刑警 (2024) {tmdb-220074} S02",
        )
        self.assertEqual(task["pattern"], "E01|E02")

    def test_share_folder_and_video_queries_filter_entries(self):
        qas = Mock()
        qas.return_value.json.side_effect = [
            {
                "success": True,
                "data": {
                    "stoken": "token",
                    "list": [
                        {"dir": True, "fid": "1", "file_name": "S01"},
                        {"dir": False, "fid": "2", "file_name": "readme.txt"},
                    ],
                },
            },
            {
                "success": True,
                "data": {
                    "list": [
                        {"dir": False, "file_name": "01.mkv"},
                        {"dir": False, "file_name": "cover.jpg"},
                        {"dir": True, "file_name": "nested"},
                    ],
                },
            },
        ]
        service = QuarkService(qas, "/IslandDownload", Mock(), Mock())
        self.assertEqual(
            service.share_folders("https://pan.quark.cn/s/demo"),
            ([{"fid": "1", "name": "S01"}], "token"),
        )
        self.assertEqual(service.share_video_files("https://pan.quark.cn/s/demo"), ["01.mkv"])
        self.assertEqual(
            service.selected_share_url(
                "https://pan.quark.cn/s/demo#old", {"fid": "1", "name": "第 1 季"}
            ),
            "https://pan.quark.cn/s/demo#/list/share/1-%E7%AC%AC%201%20%E5%AD%A3",
        )


if __name__ == "__main__":
    unittest.main()
