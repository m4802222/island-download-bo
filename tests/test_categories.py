import tempfile
import unittest
from pathlib import Path

from islandbot.categories import (
    CANONICAL_CATEGORIES,
    download_path,
    load_moviepilot_categories,
    qbit_category_paths,
)


class CategoryTests(unittest.TestCase):
    def category_yaml(self):
        return """movie:
  华语电影:
  日韩电影:
  海外电影:
tv:
  华语动漫:
  日韩动漫:
  海外动漫:
  华语剧集:
  日韩剧集:
  海外剧集:
"""

    def test_moviepilot_nine_categories_are_loaded_in_canonical_order(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "category.yaml"
            path.write_text(self.category_yaml(), encoding="utf-8")
            self.assertEqual(
                load_moviepilot_categories(path),
                list(CANONICAL_CATEGORIES),
            )

    def test_category_drift_stops_strict_loading(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "category.yaml"
            path.write_text(
                self.category_yaml().replace("海外电影", "欧美电影"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "不是约定的九分类"):
                load_moviepilot_categories(path)

    def test_missing_file_only_falls_back_when_explicitly_allowed(self):
        path = Path("/definitely/missing/category.yaml")
        with self.assertRaisesRegex(RuntimeError, "不可读取"):
            load_moviepilot_categories(path)
        self.assertEqual(
            load_moviepilot_categories(path, required=False),
            list(CANONICAL_CATEGORIES),
        )

    def test_explicit_category_uses_its_own_download_directory(self):
        categories = list(CANONICAL_CATEGORIES)
        inbox = "/downloads/complete/islandbot"
        paths = qbit_category_paths(categories, inbox)
        self.assertEqual(paths["海外电影"], "/downloads/complete/海外电影")
        self.assertEqual(download_path("__auto__", inbox, categories), inbox)
        self.assertEqual(
            download_path("海外动漫", inbox, categories),
            "/downloads/complete/海外动漫",
        )
        with self.assertRaisesRegex(RuntimeError, "未知下载分类"):
            download_path("欧美电影", inbox, categories)


if __name__ == "__main__":
    unittest.main()
