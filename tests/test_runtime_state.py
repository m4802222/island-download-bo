import tempfile
import unittest
from pathlib import Path

from islandbot.state import RuntimeState, migrate_seen


class RuntimeStateTests(unittest.TestCase):
    def test_seen_migration_accepts_old_list_and_new_mapping(self):
        self.assertEqual(migrate_seen(["one", 2, "two"], now=lambda: 7.0), {"one": 7.0, "two": 7.0})
        self.assertEqual(
            migrate_seen({"one": 1, "two": 2.5, 3: 4, "bad": "x"}),
            {"one": 1.0, "two": 2.5},
        )

    def test_runtime_state_builds_all_stores_under_data_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            state = RuntimeState(Path(temp) / "data")
            state.queue.append("hash")
            state.queue_store.save(state.queue)

            self.assertEqual(state.queue_store.load(), ["hash"])
            self.assertTrue(state.incoming_dir.is_dir())
            self.assertEqual(state.identities.store.path.parent, state.data_dir)


if __name__ == "__main__":
    unittest.main()
