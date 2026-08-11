import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from islandbot.retry import HEALTHY, NETWORK, QUOTA
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

    def test_gdrive2_recovery_notifies_once(self):
        state = {
            "gdrive2_monitor": {
                "status": QUOTA,
                "next_probe": 0,
                "seen_unhealthy": True,
                "recovery_notified": False,
            }
        }
        with (
            mock.patch.object(WORKER, "moviepilot_alias_remote", return_value="gdrive1"),
            mock.patch.object(WORKER, "probe_remote", return_value=(HEALTHY, "")) as probe,
            mock.patch.object(WORKER, "notify", return_value=True) as notify,
        ):
            WORKER.monitor_gdrive2_recovery(state, 1000)
            WORKER.monitor_gdrive2_recovery(state, 1001)
        probe.assert_called_once_with("gdrive2", WORKER.RECOVERY_PROBE_PREFIX)
        notify.assert_called_once()
        self.assertIn("gdrive2 共享云盘已恢复写入", notify.call_args.args[0])
        self.assertTrue(state["gdrive2_monitor"]["recovery_notified"])

    def test_gdrive2_failure_is_recorded_without_notification(self):
        state = {}
        with (
            mock.patch.object(WORKER, "moviepilot_alias_remote", return_value="gdrive1"),
            mock.patch.object(WORKER, "probe_remote", return_value=(QUOTA, "403")),
            mock.patch.object(WORKER, "notify") as notify,
        ):
            WORKER.monitor_gdrive2_recovery(state, 1000)
        notify.assert_not_called()
        self.assertEqual(state["gdrive2_monitor"]["status"], QUOTA)
        self.assertFalse(state["gdrive2_monitor"]["recovery_notified"])

    def test_gdrive2_recovery_notification_failure_retries_in_five_minutes(self):
        state = {
            "gdrive2_monitor": {
                "status": QUOTA,
                "next_probe": 0,
                "seen_unhealthy": True,
                "recovery_notified": False,
            }
        }
        with (
            mock.patch.object(WORKER, "moviepilot_alias_remote", return_value="gdrive1"),
            mock.patch.object(WORKER, "probe_remote", return_value=(HEALTHY, "")),
            mock.patch.object(WORKER, "notify", return_value=False),
        ):
            WORKER.monitor_gdrive2_recovery(state, 1000)
        self.assertEqual(state["gdrive2_monitor"]["next_probe"], 1300)
        self.assertFalse(state["gdrive2_monitor"]["recovery_notified"])

    def test_gdrive2_monitor_is_disabled_when_mp_is_not_on_fallback(self):
        with (
            mock.patch.object(WORKER, "moviepilot_alias_remote", return_value="gdrive2"),
            mock.patch.object(WORKER, "probe_remote") as probe,
        ):
            WORKER.monitor_gdrive2_recovery({}, 1000)
        probe.assert_not_called()

    def test_probe_backoff_is_progressive_and_bounded(self):
        self.assertEqual(WORKER.probe_delay(NETWORK, 1), 5 * 60)
        self.assertEqual(WORKER.probe_delay(NETWORK, 2), 15 * 60)
        self.assertEqual(WORKER.probe_delay(NETWORK, 99), 60 * 60)
        self.assertEqual(WORKER.probe_delay(QUOTA, 1), 30 * 60)
        self.assertEqual(WORKER.probe_delay(QUOTA, 99), 6 * 60 * 60)

    def test_no_failure_quota_probe_activates_global_gate(self):
        state = {"backend": {"status": HEALTHY, "next_probe": 0}}
        with (
            mock.patch.object(WORKER, "pending_failures", return_value=({}, set())),
            mock.patch.object(WORKER, "load_json", return_value=state),
            mock.patch.object(WORKER, "cloud_block_status", return_value={}),
            mock.patch.object(WORKER, "moviepilot_upload_active", return_value=False),
            mock.patch.object(WORKER, "monitor_gdrive2_recovery"),
            mock.patch.object(WORKER, "probe_remote", return_value=(QUOTA, "403")),
            mock.patch.object(
                WORKER, "activate_block", return_value=(["ordinary"], ["aria"])
            ) as activate,
            mock.patch.object(WORKER, "notify") as notify,
            mock.patch.object(WORKER, "save_json") as save,
        ):
            WORKER.process()
        activate.assert_called_once_with(QUOTA, {})
        self.assertIn("主动检测异常", notify.call_args.args[0])
        self.assertEqual(state["backend"]["status"], QUOTA)
        save.assert_called()

    def test_no_failure_block_clears_only_after_healthy_probe(self):
        block = {"active": True, "reason": QUOTA}
        state = {"backend": {"status": QUOTA, "next_probe": 0}}
        with (
            mock.patch.object(WORKER, "pending_failures", return_value=({}, set())),
            mock.patch.object(WORKER, "load_json", return_value=state),
            mock.patch.object(WORKER, "cloud_block_status", return_value=block),
            mock.patch.object(WORKER, "moviepilot_upload_active", return_value=False),
            mock.patch.object(WORKER, "monitor_gdrive2_recovery"),
            mock.patch.object(WORKER, "probe_remote", return_value=(HEALTHY, "")),
            mock.patch.object(WORKER, "clear_block") as clear,
            mock.patch.object(WORKER, "activate_block") as activate,
            mock.patch.object(WORKER, "save_json"),
        ):
            WORKER.process()
        clear.assert_called_once()
        activate.assert_not_called()

    def test_persisted_block_cannot_clear_without_a_new_probe(self):
        block = {"active": True, "reason": QUOTA}
        state = {
            "backend": {
                "status": HEALTHY,
                "next_probe": 9999999999,
            }
        }
        with (
            mock.patch.object(WORKER, "pending_failures", return_value=({}, set())),
            mock.patch.object(WORKER, "load_json", return_value=state),
            mock.patch.object(WORKER, "cloud_block_status", return_value=block),
            mock.patch.object(WORKER, "moviepilot_upload_active", return_value=False),
            mock.patch.object(WORKER, "monitor_gdrive2_recovery"),
            mock.patch.object(WORKER, "probe_remote") as probe,
            mock.patch.object(WORKER, "activate_block", return_value=([], [])) as activate,
            mock.patch.object(WORKER, "clear_block") as clear,
            mock.patch.object(WORKER, "save_json"),
        ):
            WORKER.process()
        probe.assert_not_called()
        activate.assert_called_once_with(QUOTA, block)
        clear.assert_not_called()

    def test_active_moviepilot_upload_defers_probe_and_retry(self):
        failure = self.failure()
        state = {"backend": {"status": HEALTHY, "next_probe": 0}}
        block = {"active": True, "reason": QUOTA}
        with (
            mock.patch.object(
                WORKER,
                "pending_failures",
                return_value=({failure.source: failure}, {failure.source}),
            ),
            mock.patch.object(WORKER, "load_json", return_value=state),
            mock.patch.object(WORKER, "cloud_block_status", return_value=block),
            mock.patch.object(WORKER, "moviepilot_upload_active", return_value=True),
            mock.patch.object(WORKER, "activate_block") as activate,
            mock.patch.object(WORKER, "probe_remote") as probe,
            mock.patch.object(WORKER, "redo") as redo,
            mock.patch.object(WORKER, "save_json"),
        ):
            WORKER.process()
        activate.assert_called_once_with(QUOTA, block)
        probe.assert_not_called()
        redo.assert_not_called()

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
