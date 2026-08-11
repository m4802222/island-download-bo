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

    def test_cloud_gate_pauses_ordinary_task_but_never_brush_task(self):
        qbit = Mock()
        tasks = [
            {
                "hash": "ordinary",
                "name": "ordinary",
                "progress": 0,
                "state": "downloading",
                "category": "华语剧集",
            },
            {
                "hash": "brush",
                "name": "brush",
                "progress": 0,
                "state": "downloading",
                "category": "学校救号",
            },
        ]
        with tempfile.TemporaryDirectory() as temp:
            block = Path(temp) / "block.json"
            block.write_text('{"active": true}', encoding="utf-8")
            service = QBitLifecycle(
                qbit,
                lambda: tasks,
                Mock(),
                "owner",
                ["ordinary", "brush"],
                set(),
                Mock(),
                Mock(),
                True,
                2,
                1,
                Path(temp),
                block,
                False,
                60,
                Mock(),
                Mock(),
            )
            service.run_queue()

        self.assertEqual(
            [call.args[:2] for call in qbit.action.call_args_list],
            [("pause", "ordinary")],
        )


if __name__ == "__main__":
    unittest.main()
