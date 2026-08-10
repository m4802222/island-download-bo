import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from islandbot.media import MediaIdentity
from islandbot.services.normalizer import EpisodeNormalizer


class EpisodeNormalizerServiceTests(unittest.TestCase):
    def test_completed_bare_episode_is_renamed_without_importing_app(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "incoming" / "islandbot" / "这一秒过火.2160p"
            root.mkdir(parents=True)
            qbit = Mock()
            qbit.files.return_value = [{"name": "08.2160p.mkv", "progress": 1.0}]
            resolver = Mock()
            resolver.automatic.return_value = MediaIdentity(
                "这一秒过火", "289139", 2026, 1, "电视剧"
            )
            service = EpisodeNormalizer(
                qbit,
                resolver,
                str(Path(temp) / "incoming" / "islandbot"),
                str(Path(temp) / "complete" / "islandbot"),
            )

            result = service.normalize_completed_episode_files(
                [{
                    "hash": "abc",
                    "content_path": str(root),
                    "name": root.name,
                    "category": "华语剧集",
                    "tags": "islandbot",
                }]
            )

            self.assertEqual(
                result,
                [("08.2160p.mkv", "这一秒过火.S01E08.2160p.mkv")],
            )
            qbit.rename_file.assert_called_once_with(
                "abc", "08.2160p.mkv", "这一秒过火.S01E08.2160p.mkv"
            )


if __name__ == "__main__":
    unittest.main()
