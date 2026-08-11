import unittest
from pathlib import Path


class AiogramDialogRecoveryTests(unittest.TestCase):
    def test_stale_intent_recovery_is_registered_at_dispatcher_boundary(self):
        source = Path(__file__).resolve().parents[1].joinpath(
            "islandbot", "aiogram_ui.py"
        ).read_text()
        self.assertIn("UnknownIntent, OutdatedIntent", source)
        self.assertIn("async def stale_dialog_error", source)
        self.assertIn("dp.errors.register(", source)
        self.assertIn("ExceptionTypeFilter(UnknownIntent, OutdatedIntent)", source)
        self.assertIn("runtime.home", source)

    def test_cloud_drive_control_is_a_separate_dialog_state(self):
        source = Path(__file__).resolve().parents[1].joinpath(
            "islandbot", "aiogram_ui.py"
        ).read_text()
        self.assertIn("drive = State()", source)
        self.assertIn("drive_confirm = State()", source)
        self.assertIn('Const("☁️ 云盘控制")', source)
        self.assertIn("runtime.CLOUD_DRIVE_CONTROL.probe_current", source)


if __name__ == "__main__":
    unittest.main()
