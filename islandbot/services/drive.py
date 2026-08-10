"""Google Drive and rclone read-only access used by download planning."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..media import parse_identity_label


class DriveService:
    """Keep rclone command details out of the Telegram/application layer."""

    def __init__(self, config: Path, remote: str, runner=subprocess.run):
        self.config = config
        self.remote = remote
        self.runner = runner

    @property
    def remote_root(self) -> str:
        return self.remote.split(":", 1)[0] + ":"

    def existing(self, media_title: str):
        identity = parse_identity_label(media_title)
        if not identity:
            raise RuntimeError("媒体身份格式异常，未检查 Google Drive")
        try:
            result = self.runner(
                [
                    "rclone",
                    "--config",
                    str(self.config),
                    "lsf",
                    self.remote,
                    "--recursive",
                    "--files-only",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Google Drive 媒体库读取失败，未提交下载：{exc}") from exc
        if result.returncode != 0:
            raise RuntimeError(
                "Google Drive 媒体库读取失败，未提交下载："
                f"{result.stderr.strip()[:120]}"
            )
        matched = []
        for path in result.stdout.splitlines():
            components = path.split("/")
            if any(
                component == identity.title
                or component.startswith(f"{identity.title} (")
                for component in components
            ):
                matched.append(path)
        return matched, bool(matched)

    def capacity(self) -> str:
        try:
            result = self.runner(
                ["rclone", "--config", str(self.config), "about", self.remote_root],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "rclone about failed")
            values = {}
            for line in result.stdout.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    values[key.strip()] = value.strip()
            if not values.get("Total"):
                raise RuntimeError("未返回配额")
            return (
                f"总 {values.get('Total', '—')} · 已用 {values.get('Used', '—')} · "
                f"可用 {values.get('Free', '—')}"
            )
        except Exception as exc:
            print("google-drive-capacity error:", exc, flush=True)
            return "暂时无法读取"

    def destination_size(self, destination: str) -> int | None:
        try:
            result = self.runner(
                [
                    "rclone",
                    "--config",
                    str(self.config),
                    "lsjson",
                    "--stat",
                    "--no-mimetype",
                    "--no-modtime",
                    f"{self.remote_root}{destination}",
                    "--contimeout",
                    "10s",
                    "--timeout",
                    "20s",
                    "--retries",
                    "1",
                    "--low-level-retries",
                    "1",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                return None
            item = json.loads(result.stdout)
            if not isinstance(item, dict) or item.get("IsDir") is not False:
                return None
            return int(item.get("Size"))
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError):
            return None
