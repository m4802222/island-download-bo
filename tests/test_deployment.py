import hashlib
import subprocess
import unittest
from pathlib import Path

from islandbot.categories import CANONICAL_CATEGORIES, load_moviepilot_categories


ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def test_versioned_category_matches_vps_baseline(self):
        category = ROOT / "config" / "category.yaml"
        digest = hashlib.sha256(category.read_bytes()).hexdigest()
        self.assertEqual(
            digest,
            "e1352efa22c8dcf6fad091ab2549f607f3d501e12921834642c6b0a9f74b9fc9",
        )
        self.assertEqual(
            load_moviepilot_categories(category),
            list(CANONICAL_CATEGORIES),
        )

    def test_shell_entrypoints_are_valid(self):
        for script in (
            ROOT / "scripts" / "deploy-vps.sh",
            ROOT / "scripts" / "sync-rclone-oauth.sh",
        ):
            with self.subTest(script=script.name):
                subprocess.run(["bash", "-n", script], check=True)

    def test_deploy_records_revision_time_and_category_backup(self):
        script = (ROOT / "scripts" / "deploy-vps.sh").read_text()
        for required in (
            "island.downloadbot.commit",
            "island.downloadbot.deployed_at",
            "deployment.json",
            "config/category.yaml",
            ".bak-deploy-",
        ):
            with self.subTest(required=required):
                self.assertIn(required, script)

    def test_oauth_entrypoint_has_one_whitelisted_remote(self):
        script = (ROOT / "scripts" / "sync-rclone-oauth.sh").read_text()
        self.assertIn('REMOTE="${REMOTE:-gdrive1}"', script)
        self.assertNotIn("HOST_REMOTE", script)
        self.assertNotIn("MP_REMOTE", script)


if __name__ == "__main__":
    unittest.main()
