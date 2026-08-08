import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from islandbot.retry import HEALTHY
from islandbot.retry import TransferFailure
from islandbot.transfer_verification import TransferProof


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "retry-moviepilot-rclone.py"
SPEC = importlib.util.spec_from_file_location("retry_moviepilot_rclone", SCRIPT)
WORKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORKER)


class RetryWorkerTests(unittest.TestCase):
    @staticmethod
    def failure():
        return TransferFailure(
            history_id=724,
            source="/downloads/show.mkv",
            title="节目",
            error="上传 rclone 失败",
            date="2026-08-08",
            download_hash="hash",
        )

    def test_stale_cleanup_is_restricted_to_probe_files_at_remote_root(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(WORKER, "run", return_value=completed) as runner:
            WORKER.cleanup_stale_probes()
        command = runner.call_args.args[0]
        self.assertIn(".islandbot-rclone-probe-*.txt", command)
        self.assertEqual(command[command.index("--max-depth") + 1], "1")
        self.assertEqual(command[command.index("--min-age") + 1], "1m")

    def test_successful_probe_retries_eventually_visible_delete(self):
        uploaded = subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.object(WORKER, "cleanup_stale_probes"),
            mock.patch.object(WORKER, "run", return_value=uploaded),
            mock.patch.object(WORKER, "delete_probe", side_effect=[False, True]) as delete,
            mock.patch.object(WORKER.time, "sleep") as sleep,
        ):
            status, _ = WORKER.probe_remote()
        self.assertEqual(status, HEALTHY)
        self.assertEqual(delete.call_count, 2)
        sleep.assert_called_once_with(3)

    def test_remote_destination_size_requires_file_stat(self):
        response = subprocess.CompletedProcess(
            [],
            0,
            json.dumps({"Path": "episode.mkv", "IsDir": False, "Size": 4096}),
            "",
        )
        with mock.patch.object(WORKER, "run", return_value=response) as runner:
            size = WORKER.remote_destination_size("/Media/show/episode.mkv")
        self.assertEqual(size, 4096)
        command = runner.call_args.args[0]
        self.assertIn("--stat", command)
        self.assertIn("MP:/Media/show/episode.mkv", command)

    def test_remote_destination_size_rejects_directory_or_error(self):
        directory = subprocess.CompletedProcess(
            [], 0, json.dumps({"IsDir": True, "Size": -1}), ""
        )
        failed = subprocess.CompletedProcess([], 1, "", "not found")
        with mock.patch.object(WORKER, "run", return_value=directory):
            self.assertIsNone(WORKER.remote_destination_size("/Media/show"))
        with mock.patch.object(WORKER, "run", return_value=failed):
            self.assertIsNone(WORKER.remote_destination_size("/Media/missing.mkv"))

    def test_missing_source_with_proof_blocks_without_retry(self):
        failure = self.failure()
        proof = TransferProof(
            history_id=725,
            source=failure.source,
            destination="/Media/show.mkv",
            source_size=100,
            destination_size=100,
        )
        with (
            mock.patch.object(
                WORKER,
                "pending_rclone_failures",
                return_value={failure.source: failure},
            ),
            mock.patch.object(
                WORKER,
                "load_successful_transfer_proofs",
                return_value={failure.source: proof},
            ),
            mock.patch.object(
                WORKER,
                "verified_transfer_sources",
                return_value=(set(), {failure.source: "not visible"}),
            ),
            mock.patch.object(WORKER, "source_exists", return_value=False),
        ):
            unresolved, retryable = WORKER.pending_failures()
        self.assertEqual(unresolved, {failure.source: failure})
        self.assertEqual(retryable, set())

    def test_missing_source_without_success_proof_is_not_retryable(self):
        failure = self.failure()
        with (
            mock.patch.object(
                WORKER,
                "pending_rclone_failures",
                return_value={failure.source: failure},
            ),
            mock.patch.object(
                WORKER,
                "load_successful_transfer_proofs",
                return_value={},
            ),
            mock.patch.object(
                WORKER,
                "verified_transfer_sources",
                return_value=(set(), {}),
            ),
            mock.patch.object(WORKER, "source_exists", return_value=False),
        ):
            unresolved, retryable = WORKER.pending_failures()
        self.assertEqual(unresolved, {})
        self.assertEqual(retryable, set())

    def test_success_before_failure_is_not_accepted_as_proof(self):
        failure = self.failure()
        proof = TransferProof(
            history_id=700,
            source=failure.source,
            destination="/Media/show.mkv",
            source_size=100,
            destination_size=100,
        )
        with (
            mock.patch.object(
                WORKER,
                "pending_rclone_failures",
                return_value={failure.source: failure},
            ),
            mock.patch.object(
                WORKER,
                "load_successful_transfer_proofs",
                return_value={failure.source: proof},
            ),
            mock.patch.object(
                WORKER,
                "verified_transfer_sources",
                return_value=({failure.source}, {}),
            ) as verifier,
            mock.patch.object(WORKER, "source_exists", return_value=False),
        ):
            unresolved, retryable = WORKER.pending_failures()
        verifier.assert_called_once_with({}, WORKER.remote_destination_size)
        self.assertEqual(unresolved, {})
        self.assertEqual(retryable, set())


if __name__ == "__main__":
    unittest.main()
