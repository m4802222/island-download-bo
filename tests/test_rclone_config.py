import tempfile
import unittest
from pathlib import Path

from islandbot.rclone_config import replace_oauth_token, sync_oauth_token


class RcloneConfigTests(unittest.TestCase):
    def source(self):
        return """[gdrive1]
type = drive
token = {"access_token":"new-one"}

[gdrive2]
type = drive
token = {"access_token":"new-two"}
"""

    def target(self):
        return """[gdrive1]
type = drive
token = {"access_token":"old-one"}
team_drive = first

[gdrive2]
type = drive
token = {"access_token":"old-two"}
team_drive = second

[MP]
type = alias
remote = gdrive2:
"""

    def test_only_selected_token_changes_and_mp_alias_is_byte_identical(self):
        updated = replace_oauth_token(self.source(), self.target(), "gdrive1")
        expected = self.target().replace(
            'token = {"access_token":"old-one"}',
            'token = {"access_token":"new-one"}',
            1,
        )
        self.assertEqual(updated, expected)
        self.assertIn('token = {"access_token":"old-two"}', updated)
        self.assertTrue(updated.endswith("[MP]\ntype = alias\nremote = gdrive2:\n"))

    def test_mp_and_unknown_remotes_are_rejected(self):
        for remote in ("MP", "gdrive3", ""):
            with self.subTest(remote=remote):
                with self.assertRaisesRegex(RuntimeError, "只允许更新"):
                    replace_oauth_token(self.source(), self.target(), remote)

    def test_missing_or_duplicate_token_is_rejected(self):
        missing = self.source().replace(
            'token = {"access_token":"new-one"}\n',
            "",
        )
        with self.assertRaisesRegex(RuntimeError, "没有有效 token"):
            replace_oauth_token(missing, self.target(), "gdrive1")
        duplicate = self.source().replace(
            'token = {"access_token":"new-one"}',
            'token = one\ntoken = two',
        )
        with self.assertRaisesRegex(RuntimeError, "重复 token"):
            replace_oauth_token(duplicate, self.target(), "gdrive1")

    def test_file_update_is_atomic_and_reports_noop(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.conf"
            target = Path(folder) / "target.conf"
            source.write_text(self.source(), encoding="utf-8")
            target.write_text(self.target(), encoding="utf-8")
            self.assertTrue(sync_oauth_token(source, target, "gdrive2"))
            self.assertFalse(sync_oauth_token(source, target, "gdrive2"))
            self.assertIn('token = {"access_token":"new-two"}', target.read_text())


if __name__ == "__main__":
    unittest.main()
