import subprocess
import tempfile
import unittest
from pathlib import Path

from islandbot.retry import HEALTHY, QUOTA
from islandbot.services.cloud_drive_control import CloudDriveControl


CONFIG = """[gdrive1]
type = drive
token = one

[gdrive2]
type = drive
token = two

[MP]
type = alias
remote = gdrive1:

[mediaunion]
type = union
upstreams = gdrive1:Media gdrive2:Media
"""


class FakeRunner:
    def __init__(
        self,
        copy_returncode=0,
        copy_error="",
        delete_returncode=0,
        delete_returncodes=None,
    ):
        self.copy_returncode = copy_returncode
        self.copy_error = copy_error
        self.delete_returncode = delete_returncode
        self.delete_returncodes = list(delete_returncodes or [])
        self.commands = []

    def __call__(self, command, **_kwargs):
        self.commands.append(command)
        if "copyto" in command:
            return subprocess.CompletedProcess(
                command, self.copy_returncode, "", self.copy_error
            )
        if "deletefile" in command:
            returncode = (
                self.delete_returncodes.pop(0)
                if self.delete_returncodes
                else self.delete_returncode
            )
            return subprocess.CompletedProcess(command, returncode, "", "delete failed")
        return subprocess.CompletedProcess(command, 0, "", "")


class CloudDriveControlTests(unittest.TestCase):
    def service(self, folder, runner=None, sleeper=lambda _delay: None):
        config = Path(folder) / "rclone.conf"
        config.write_text(CONFIG, encoding="utf-8")
        return config, CloudDriveControl(
            config,
            runner=runner or FakeRunner(),
            sleeper=sleeper,
        )

    def test_live_view_reads_mp_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            _, service = self.service(folder)
            self.assertEqual(service.view(), {"current": "gdrive1", "target": "gdrive2"})

    def test_probe_checks_current_remote_and_deletes_exact_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = FakeRunner()
            _, service = self.service(folder, runner)
            result = service.probe_current()
            self.assertEqual(result.status, HEALTHY)
            self.assertTrue(result.cleaned)
            cleanup = next(command for command in runner.commands if "delete" in command)
            copy = next(command for command in runner.commands if "copyto" in command)
            delete = next(command for command in runner.commands if "deletefile" in command)
            copy_target = copy[copy.index("copyto") + 2]
            delete_target = delete[delete.index("deletefile") + 1]
            self.assertIn(".islandbot-ui-probe-*.txt", cleanup)
            self.assertEqual(cleanup[cleanup.index("--max-depth") + 1], "1")
            self.assertTrue(copy_target.startswith("gdrive1:/.islandbot-ui-probe-"))
            self.assertEqual(delete_target, copy_target)

    def test_probe_waits_for_google_visibility_before_retrying_delete(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = FakeRunner(delete_returncodes=[1, 0])
            sleeps = []
            _, service = self.service(folder, runner, sleeper=sleeps.append)
            result = service.probe_current()
            self.assertTrue(result.healthy)
            self.assertTrue(result.cleaned)
            self.assertEqual(sleeps, [3, 10])
            self.assertEqual(
                sum("deletefile" in command for command in runner.commands),
                2,
            )

    def test_switch_probes_target_then_changes_only_mp_remote(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = FakeRunner()
            config, service = self.service(folder, runner)
            result = service.switch("gdrive2")
            self.assertTrue(result.changed)
            self.assertEqual(service.current(), "gdrive2")
            updated = config.read_text(encoding="utf-8")
            self.assertEqual(updated, CONFIG.replace("remote = gdrive1:", "remote = gdrive2:"))
            copy = next(command for command in runner.commands if "copyto" in command)
            self.assertIn("gdrive2:/.islandbot-ui-probe-", " ".join(copy))

    def test_failed_target_probe_never_changes_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = FakeRunner(1, "googleapi: Error 403: User rate limit exceeded")
            config, service = self.service(folder, runner)
            result = service.switch("gdrive2")
            self.assertFalse(result.changed)
            self.assertEqual(result.probe.status, QUOTA)
            self.assertEqual(config.read_text(encoding="utf-8"), CONFIG)

    def test_same_target_is_noop_without_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = FakeRunner()
            _, service = self.service(folder, runner)
            result = service.switch("gdrive1")
            self.assertFalse(result.changed)
            self.assertIsNone(result.probe)
            self.assertEqual(runner.commands, [])

    def test_switch_is_refused_when_probe_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = FakeRunner(delete_returncode=1)
            config, service = self.service(folder, runner)
            result = service.switch("gdrive2")
            self.assertFalse(result.changed)
            self.assertFalse(result.probe.healthy)
            self.assertEqual(config.read_text(encoding="utf-8"), CONFIG)

    def test_unknown_drive_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            _, service = self.service(folder)
            with self.assertRaisesRegex(RuntimeError, "只允许切换"):
                service.switch("MP")


if __name__ == "__main__":
    unittest.main()
