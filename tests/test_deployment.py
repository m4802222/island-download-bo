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

        self.assertTrue((ROOT / "scripts" / "island-health.service").is_file())
        self.assertTrue((ROOT / "scripts" / "island-health.timer").is_file())

    def test_deploy_records_revision_time_and_category_backup(self):
        script = (ROOT / "scripts" / "deploy-vps.sh").read_text()
        for required in (
            "island.downloadbot.commit",
            "island.downloadbot.deployed_at",
            "deployment.json",
            "config/category.yaml",
            ".bak-deploy-",
            "island-health.timer",
            "health-check.py",
            "stop_on_upload_limit = true",
            "settings.TRANSFER_THREADS",
        ):
            with self.subTest(required=required):
                self.assertIn(required, script)

    def test_deploy_defaults_to_the_versioned_release(self):
        version = (ROOT / "VERSION").read_text().strip()
        script = (ROOT / "scripts" / "deploy-vps.sh").read_text()
        self.assertIn(f"ref=${{ISLAND_BOT_REF:-v{version}}}", script)

    def test_upload_gate_timer_checks_every_minute_without_probing_every_minute(self):
        timer = (ROOT / "scripts" / "moviepilot-rclone-retry.timer").read_text()
        worker = (ROOT / "scripts" / "retry-moviepilot-rclone.py").read_text()
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("HEALTHY: (30 * 60,)", worker)

    def test_oauth_entrypoint_has_one_whitelisted_remote(self):
        script = (ROOT / "scripts" / "sync-rclone-oauth.sh").read_text()
        self.assertIn('REMOTE="${REMOTE:-gdrive1}"', script)
        self.assertNotIn("HOST_REMOTE", script)
        self.assertNotIn("MP_REMOTE", script)


if __name__ == "__main__":
    unittest.main()
