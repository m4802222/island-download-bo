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


if __name__ == "__main__":
    unittest.main()
