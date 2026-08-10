import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from islandbot.services.qbit_lifecycle import QBitLifecycle


class QBitLifecycleTests(unittest.TestCase):
    def test_queue_pauses_after_limit_and_resumes_first_tasks(self):
        qbit = Mock()
        tasks = [
            {"hash": "one", "name": "one", "progress": 0, "state": "pausedDL", "total_size": 1},
            {"hash": "two", "name": "two", "progress": 0, "state": "pausedDL", "total_size": 1},
            {"hash": "three", "name": "three", "progress": 0, "state": "downloading", "total_size": 1},
        ]
        with tempfile.TemporaryDirectory() as temp:
            save_queue = Mock()
            save_blocked = Mock()
            service = QBitLifecycle(
                qbit,
                lambda: tasks,
                Mock(),
                "owner",
                ["one", "two", "three"],
                set(),
                save_queue,
                save_blocked,
                True,
                2,
                1,
                Path(temp),
                Path(temp) / "block.json",
                False,
                60,
                Mock(),
                Mock(),
                disk_usage=lambda _: SimpleNamespace(free=10 * 1024**3),
            )

            service.run_queue()

        self.assertEqual(
            [call.args[:2] for call in qbit.action.call_args_list],
            [("pause", "three"), ("resume", "one"), ("resume", "two")],
        )
        save_queue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
