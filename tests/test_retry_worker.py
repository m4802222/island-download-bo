import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from islandbot.retry import HEALTHY


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "retry-moviepilot-rclone.py"
SPEC = importlib.util.spec_from_file_location("retry_moviepilot_rclone", SCRIPT)
WORKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORKER)


class RetryWorkerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
