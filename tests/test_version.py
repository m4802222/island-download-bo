import unittest
from pathlib import Path

import islandbot


class VersionTests(unittest.TestCase):
    def test_package_version_matches_version_file(self):
        expected = (
            Path(__file__).resolve().parents[1].joinpath("VERSION").read_text().strip()
        )
        self.assertEqual(islandbot.__version__, expected)


if __name__ == "__main__":
    unittest.main()
