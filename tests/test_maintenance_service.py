import unittest
from unittest.mock import Mock

from islandbot.services.maintenance import MaintenanceService


def service(**overrides):
    defaults = {
        "monotonic": lambda: 1000.0,
        "wall_time": lambda: 2_000_000.0,
        "get_last_category_sync": lambda: 900.0,
        "set_last_category_sync": Mock(),
        "category_sync_interval": 600.0,
        "category_sync_retry_interval": 60.0,
        "synchronize_categories": Mock(),
        "process_inbox": Mock(),
        "run_quark_queue": Mock(),
        "task_list": Mock(return_value=[]),
        "normalize_completed": Mock(return_value=[]),
        "prepare_completed": Mock(return_value=[]),
        "trigger_transfer": Mock(),
        "run_download_queue": Mock(),
        "send_temporary": Mock(),
        "owner": 1,
        "seen": {},
        "save_seen": Mock(),
        "aria_tracked": {},
        "aria_rpc": Mock(),
        "aria_name": Mock(return_value="file"),
        "send": Mock(),
        "save_aria_tracked": Mock(),
        "cleanup_transferred": Mock(),
        "delete_expired": Mock(),
        "log": Mock(),
    }
    defaults.update(overrides)
    return MaintenanceService(**defaults)


class MaintenanceServiceTests(unittest.TestCase):
    def test_tick_runs_each_maintenance_stage(self):
        calls = []
        worker = service(
            process_inbox=lambda: calls.append("inbox"),
            run_quark_queue=lambda: calls.append("quark"),
            task_list=lambda: calls.append("tasks") or [],
            normalize_completed=lambda tasks: calls.append("normalize") or [],
            prepare_completed=lambda tasks: calls.append("prepare") or [],
            run_download_queue=lambda: calls.append("queue"),
            cleanup_transferred=lambda: calls.append("cleanup"),
            delete_expired=lambda: calls.append("expiry"),
        )

        worker.tick()

        self.assertEqual(
            calls,
            ["inbox", "quark", "tasks", "normalize", "prepare", "queue", "cleanup", "expiry"],
        )

    def test_failed_category_sync_schedules_retry_without_stopping_tick(self):
        set_last_sync = Mock()
        process_inbox = Mock()
        worker = service(
            get_last_category_sync=lambda: 0,
            set_last_category_sync=set_last_sync,
            synchronize_categories=Mock(side_effect=RuntimeError("offline")),
            process_inbox=process_inbox,
        )

        worker.tick()

        set_last_sync.assert_called_once_with(460.0)
        process_inbox.assert_called_once()

    def test_new_completion_is_persisted_and_notified(self):
        item = {"hash": "abc", "name": "Movie", "progress": 1, "category": "华语电影"}
        save_seen = Mock()
        notify = Mock()
        trigger = Mock()
        worker = service(
            task_list=Mock(return_value=[item]),
            normalize_completed=Mock(return_value=[]),
            prepare_completed=Mock(return_value=[item]),
            save_seen=save_seen,
            send_temporary=notify,
            trigger_transfer=trigger,
        )

        worker.watch_completed()

        trigger.assert_called_once()
        save_seen.assert_called_once_with({"abc": 2_000_000.0})
        self.assertIn("Movie", notify.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
