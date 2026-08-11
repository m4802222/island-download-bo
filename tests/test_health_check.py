import importlib.util
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "health-check.py"
SPEC = importlib.util.spec_from_file_location("island_health_check", SCRIPT)
HEALTH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HEALTH)


class HealthCheckTests(unittest.TestCase):
    def test_upload_policy_requires_remote_identity_limit_and_one_worker(self):
        remote = subprocess.CompletedProcess(
            [],
            0,
            "\n".join(
                [
                    "[gdrive2]",
                    "type = drive",
                    "client_id = XXX",
                    "team_drive = XXX",
                    "stop_on_upload_limit = true",
                ]
            ),
            "",
        )
        threads = subprocess.CompletedProcess([], 0, "1\n", "")
        with mock.patch.object(HEALTH, "run", side_effect=[remote, threads]):
            emoji, message = HEALTH.check_upload_policy()
        self.assertEqual(emoji, "🟢")
        self.assertIn("单线程", message)

    def test_upload_policy_fails_closed_for_blank_identity_or_two_workers(self):
        remote = subprocess.CompletedProcess(
            [],
            0,
            "type = drive\nclient_id =\nteam_drive =\nstop_on_upload_limit = false\n",
            "",
        )
        threads = subprocess.CompletedProcess([], 0, "2\n", "")
        with mock.patch.object(HEALTH, "run", side_effect=[remote, threads]):
            emoji, message = HEALTH.check_upload_policy()
        self.assertEqual(emoji, "🔴")
        for problem in ("client_id", "共享云盘", "快速失败", "线程不是1"):
            self.assertIn(problem, message)

    def test_upload_stats_deduplicate_destination_and_count_failures(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "user.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE transferhistory ("
                    "id INTEGER, status BOOLEAN, date VARCHAR, dest VARCHAR, "
                    "dest_storage VARCHAR, src_fileitem JSON, dest_fileitem JSON, "
                    "errmsg VARCHAR)"
                )
                item = json.dumps({"size": 4096})
                connection.executemany(
                    "INSERT INTO transferhistory VALUES "
                    "(?, ?, datetime('now'), ?, ?, ?, ?, ?)",
                    [
                        (1, 1, "/Media/show.mkv", "rclone", item, item, ""),
                        (2, 1, "/Media/show.mkv", "rclone", item, item, ""),
                        (3, 0, "/Media/failed.mkv", "rclone", item, item, "上传 rclone 失败"),
                    ],
                )
            self.assertEqual(
                HEALTH.moviepilot_upload_stats(database),
                (1, 4096, 1),
            )

    def test_cloud_category_check_reads_union_root(self):
        output = "".join(f"{name}/\n" for name in HEALTH.CANONICAL_CATEGORIES)
        completed = subprocess.CompletedProcess([], 0, output, "")
        with mock.patch.object(HEALTH, "run", return_value=completed) as runner:
            emoji, _ = HEALTH.check_cloud_categories()
        self.assertEqual(emoji, "🟢")
        self.assertEqual(runner.call_args.args[0][2], "mediaunion:")


if __name__ == "__main__":
    unittest.main()
