import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from islandbot.transfer_verification import (
    load_successful_transfer_proofs,
    successful_transfer_proofs,
    verified_transfer_sources,
)


class TransferVerificationTests(unittest.TestCase):
    source = "/downloads/show/episode.mkv"
    destination = "/Media/华语剧集/节目/Season 1/节目 - S01E01.mkv"

    def row(self, source_size=1024, destination_size=1024):
        return (
            833,
            self.source,
            self.destination,
            json.dumps([self.source]),
            json.dumps({"path": self.source, "storage": "local", "size": source_size}),
            json.dumps(
                {
                    "path": self.destination,
                    "storage": "rclone",
                    "size": destination_size,
                }
            ),
        )

    def test_requires_history_sizes_and_live_remote_size(self):
        proofs = successful_transfer_proofs([self.row()], {self.source})
        verified, rejected = verified_transfer_sources(
            proofs,
            lambda destination: 1024,
            {self.source: 1024},
        )
        self.assertEqual(verified, {self.source})
        self.assertEqual(rejected, {})

    def test_rejects_historical_size_mismatch(self):
        proofs = successful_transfer_proofs([self.row(destination_size=900)])
        verified, rejected = verified_transfer_sources(proofs, lambda _: 900)
        self.assertEqual(verified, set())
        self.assertIn("历史源文件与目标文件大小不一致", rejected[self.source])

    def test_rejects_qbit_size_mismatch(self):
        proofs = successful_transfer_proofs([self.row()])
        verified, rejected = verified_transfer_sources(
            proofs,
            lambda _: 1024,
            {self.source: 2048},
        )
        self.assertEqual(verified, set())
        self.assertIn("qBittorrent 文件大小", rejected[self.source])

    def test_rejects_missing_or_wrong_sized_remote(self):
        proofs = successful_transfer_proofs([self.row()])
        verified, rejected = verified_transfer_sources(proofs, lambda _: None)
        self.assertEqual(verified, set())
        self.assertIn("不存在", rejected[self.source])
        verified, rejected = verified_transfer_sources(proofs, lambda _: 512)
        self.assertEqual(verified, set())
        self.assertIn("实时大小", rejected[self.source])

    def test_malformed_or_unrelated_rows_are_not_proofs(self):
        malformed = list(self.row())
        malformed[3] = "broken"
        self.assertEqual(successful_transfer_proofs([tuple(malformed)]), {})
        self.assertEqual(
            successful_transfer_proofs([self.row()], {"/downloads/other.mkv"}),
            {},
        )

    def test_loads_proofs_from_moviepilot_database(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "user.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE transferhistory "
                    "(id INTEGER, src TEXT, dest TEXT, files JSON, "
                    "src_fileitem JSON, dest_fileitem JSON, status BOOLEAN)"
                )
                connection.execute(
                    "INSERT INTO transferhistory VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (*self.row(), 1),
                )
            proofs = load_successful_transfer_proofs(database, {self.source})
            self.assertEqual(proofs[self.source].destination_size, 1024)


if __name__ == "__main__":
    unittest.main()
