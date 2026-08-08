import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from islandbot.retry import (
    AUTH,
    HEALTHY,
    NETWORK,
    QUOTA,
    RETRY_DELAYS,
    UNKNOWN,
    TransferFailure,
    classify_probe_error,
    cloud_block_status,
    pending_rclone_failures,
    update_retry_state,
)


class RcloneRetryTests(unittest.TestCase):
    def make_database(self, root):
        database = root / "user.db"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE transferhistory "
                "(id INTEGER, status BOOLEAN, errmsg TEXT, src TEXT, "
                "title TEXT, date TEXT, download_hash TEXT)"
            )
        return database

    @staticmethod
    def row(history_id, status, error, source, title="测试媒体"):
        return (history_id, status, error, source, title, "2026-08-08", "hash")

    @staticmethod
    def failure(source="/downloads/show.mkv", history_id=724):
        return TransferFailure(
            history_id=history_id,
            source=source,
            title="测试媒体",
            error="上传 rclone 失败",
            date="2026-08-08",
            download_hash="hash",
        )

    def test_only_existing_unsuccessful_rclone_sources_are_returned(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = self.make_database(root)
            retry_source = root / "retry.mkv"
            successful_source = root / "done.mkv"
            missing_source = root / "missing.mkv"
            retry_source.touch()
            successful_source.touch()
            with sqlite3.connect(database) as connection:
                connection.executemany(
                    "INSERT INTO transferhistory VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        self.row(1, 0, "上传 rclone 失败", str(retry_source)),
                        self.row(2, 0, "未识别到媒体信息", str(root / "other.srt")),
                        self.row(3, 0, "上传 rclone 失败", str(successful_source)),
                        self.row(4, 1, None, str(successful_source)),
                        self.row(5, 0, "上传 rclone 失败", str(missing_source)),
                    ],
                )
            failures = pending_rclone_failures(database)
            self.assertEqual(list(failures), [str(retry_source)])
            self.assertEqual(failures[str(retry_source)].history_id, 1)

    def test_host_path_mapping_can_be_supplied(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = self.make_database(root)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "INSERT INTO transferhistory VALUES (?, ?, ?, ?, ?, ?, ?)",
                    self.row(7, 0, "upload rclone failed", "/downloads/show.mkv"),
                )
            failures = pending_rclone_failures(
                database,
                source_exists=lambda source: source == "/downloads/show.mkv",
            )
            self.assertEqual(failures["/downloads/show.mkv"].history_id, 7)

    def test_probe_errors_are_classified(self):
        self.assertEqual(classify_probe_error("", 0), HEALTHY)
        self.assertEqual(
            classify_probe_error("googleapi: Error 403: User rate limit exceeded", 1),
            QUOTA,
        )
        self.assertEqual(classify_probe_error("invalid_grant: token expired", 1), AUTH)
        self.assertEqual(classify_probe_error("TLS handshake timeout", 1), NETWORK)
        self.assertEqual(classify_probe_error("permission denied", 1), UNKNOWN)

    def test_first_failure_waits_then_backs_off(self):
        failure = self.failure()
        failures = {failure.source: failure}
        due, state, new = update_retry_state(
            failures, {}, 1000, HEALTHY, allow_retry=True
        )
        self.assertEqual(due, [])
        self.assertEqual(new, [failure])
        self.assertEqual(
            state["items"][failure.source]["next_retry"],
            1000 + RETRY_DELAYS[HEALTHY][0],
        )

        retry_at = state["items"][failure.source]["next_retry"]
        due, state, new = update_retry_state(
            failures, state, retry_at, HEALTHY, allow_retry=True
        )
        self.assertEqual(due, [failure])
        self.assertEqual(new, [])
        self.assertEqual(state["items"][failure.source]["attempts"], 1)
        self.assertEqual(
            state["items"][failure.source]["next_retry"],
            retry_at + RETRY_DELAYS[HEALTHY][1],
        )

    def test_unhealthy_backend_never_retries(self):
        failure = self.failure()
        failures = {failure.source: failure}
        state = {
            "items": {
                failure.source: {"attempts": 2, "first_seen": 1, "next_retry": 1}
            }
        }
        due, updated, _ = update_retry_state(
            failures, state, 1000, QUOTA, allow_retry=False
        )
        self.assertEqual(due, [])
        self.assertEqual(updated["items"][failure.source]["attempts"], 2)

    def test_success_removes_persistent_items(self):
        state = {"items": {"old": {"attempts": 3}}, "backend": {"status": HEALTHY}}
        due, updated, new = update_retry_state(
            {}, state, 1000, HEALTHY, allow_retry=True
        )
        self.assertEqual((due, new), ([], []))
        self.assertEqual(updated["items"], {})
        self.assertEqual(updated["backend"]["status"], HEALTHY)

    def test_cloud_block_status_is_fail_closed_only_for_valid_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "block.json"
            self.assertEqual(cloud_block_status(path), {})
            path.write_text("broken", encoding="utf-8")
            self.assertEqual(cloud_block_status(path), {})
            path.write_text(json.dumps({"active": True}), encoding="utf-8")
            self.assertEqual(cloud_block_status(path), {"active": True})


if __name__ == "__main__":
    unittest.main()
