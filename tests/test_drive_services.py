import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from islandbot.services.drive import DriveService
from islandbot.services.transfer import TransferService


class DriveServiceTests(unittest.TestCase):
    def test_drive_queries_are_scoped_to_configured_remote(self):
        def runner(command, **_kwargs):
            if "lsf" in command:
                return SimpleNamespace(
                    returncode=0,
                    stdout="Foo (2024)/Season 1/Foo - S01E01.mkv\nOther/one.mkv\n",
                    stderr="",
                )
            if "about" in command:
                return SimpleNamespace(
                    returncode=0,
                    stdout="Total: 100 GiB\nUsed: 40 GiB\nFree: 60 GiB\n",
                    stderr="",
                )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"IsDir": False, "Size": 123}),
                stderr="",
            )

        service = DriveService(Path("/rclone.conf"), "MP:Media", runner=runner)
        self.assertEqual(
            service.existing("Foo (2024) {tmdb-123} S01"),
            (["Foo (2024)/Season 1/Foo - S01E01.mkv"], True),
        )
        self.assertEqual(service.capacity(), "总 100 GiB · 已用 40 GiB · 可用 60 GiB")
        self.assertEqual(service.destination_size("Media/Foo/E01.mkv"), 123)


class TransferServiceTests(unittest.TestCase):
    def test_moviepilot_history_lookup_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "user.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE transferhistory "
                    "(status BOOLEAN, title TEXT, episodes TEXT, dest TEXT, files TEXT)"
                )
                connection.execute(
                    "INSERT INTO transferhistory VALUES (1, 'Foo', 'E01', "
                    "'/Media/Foo/E01.mkv', '[]')"
                )
                connection.commit()
            service = TransferService(database)
            paths, found = service.moviepilot_existing("Foo (2024) {tmdb-123} S01")
            self.assertTrue(found)
            self.assertIn("/Media/Foo/E01.mkv", paths)


if __name__ == "__main__":
    unittest.main()
