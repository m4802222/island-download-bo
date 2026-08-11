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
    def __init__(self, copy_returncode=0, copy_error="", delete_returncode=0):
        self.copy_returncode = copy_returncode
        self.copy_error = copy_error
        self.delete_returncode = delete_returncode
        self.commands = []

    def __call__(self, command, **_kwargs):
        self.commands.append(command)
        if "copyto" in command:
            return subprocess.CompletedProcess(
                command, self.copy_returncode, "", self.copy_error
            )
        if "deletefile" in command:
            return subprocess.CompletedProcess(command, self.delete_returncode, "", "delete failed")
        return subprocess.CompletedProcess(command, 0, "", "")


class CloudDriveControlTests(unittest.TestCase):
    def service(self, folder, runner=None):
        config = Path(folder) / "rclone.conf"
        config.write_text(CONFIG, encoding="utf-8")
        return config, CloudDriveControl(config, runner=runner or FakeRunner())

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
            copy_target = runner.commands[0][runner.commands[0].index("copyto") + 2]
            delete_target = runner.commands[1][runner.commands[1].index("deletefile") + 1]
            self.assertTrue(copy_target.startswith("gdrive1:/.islandbot-ui-probe-"))
            self.assertEqual(delete_target, copy_target)

    def test_switch_probes_target_then_changes_only_mp_remote(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = FakeRunner()
            config, service = self.service(folder, runner)
            result = service.switch("gdrive2")
            self.assertTrue(result.changed)
            self.assertEqual(service.current(), "gdrive2")
            updated = config.read_text(encoding="utf-8")
            self.assertEqual(updated, CONFIG.replace("remote = gdrive1:", "remote = gdrive2:"))
            self.assertIn("gdrive2:/.islandbot-ui-probe-", " ".join(runner.commands[0]))

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
