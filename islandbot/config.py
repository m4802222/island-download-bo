"""Validated runtime configuration."""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def _integer(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数") from exc
    if value < minimum:
        raise RuntimeError(f"{name} 不能小于 {minimum}")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} 必须是 true 或 false")


def explicit_web_port(url: str) -> str:
    """Show the real public port even when HTTPS/HTTP uses its default."""
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    if parsed.port is not None or parsed.scheme not in {"http", "https"}:
        return url
    port = 443 if parsed.scheme == "https" else 80
    netloc = f"{parsed.hostname}:{port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_id: int
    qbit_url: str
    qbit_username: str
    qbit_password: str
    qas_url: str
    qas_username: str
    qas_password: str
    quark_save_path: str
    aria2_url: str
    aria2_secret: str
    moviepilot_url: str
    moviepilot_token: str
    emby_url: str
    emby_api_key: str
    emby_public_url: str
    emby_default_password: str
    ollama_url: str
    ollama_model: str
    data_dir: Path
    downloads_dir: Path
    qbit_save_path: str
    moviepilot_db: Path
    moviepilot_category_file: Path
    rclone_config: Path
    drive_remote: str
    min_free_gib: int
    max_active_downloads: int
    auto_cleanup_completed: bool
    cleanup_interval_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            bot_token=_required("BOT_TOKEN"),
            owner_id=int(_required("OWNER_ID")),
            qbit_url=os.environ.get("QBIT_URL", "http://qbittorrent:8080").rstrip("/"),
            qbit_username=_required("QBIT_USERNAME"),
            qbit_password=_required("QBIT_PASSWORD"),
            qas_url=os.environ.get("QAS_URL", "http://quark-auto-save:5005").rstrip("/"),
            qas_username=os.environ.get("QAS_USERNAME", "").strip(),
            qas_password=os.environ.get("QAS_PASSWORD", "").strip(),
            quark_save_path=os.environ.get("QUARK_SAVE_PATH", "/IslandDownloadBot"),
            aria2_url=os.environ.get("ARIA2_URL", "http://aria2:6800/jsonrpc"),
            aria2_secret=os.environ.get("ARIA2_SECRET", "").strip(),
            moviepilot_url=os.environ.get("MOVIEPILOT_URL", "http://moviepilot:3001").rstrip("/"),
            moviepilot_token=os.environ.get("MOVIEPILOT_TOKEN", "").strip(),
            emby_url=os.environ.get("EMBY_URL", "http://emby:8096").rstrip("/"),
            emby_api_key=os.environ.get("EMBY_API_KEY", "").strip(),
            emby_public_url=os.environ.get("EMBY_PUBLIC_URL", "").strip().rstrip("/"),
            emby_default_password=os.environ.get("EMBY_DEFAULT_PASSWORD", "123456"),
            ollama_url=os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/"),
            ollama_model=os.environ.get("OLLAMA_MODEL", "qwen3:1.7b"),
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            downloads_dir=Path(os.environ.get("DOWNLOADS_DIR", "/downloads")),
            qbit_save_path=os.environ.get(
                "QBIT_SAVE_PATH", "/downloads/complete/islandbot"
            ).rstrip("/"),
            moviepilot_db=Path(os.environ.get("MOVIEPILOT_DB", "/moviepilot-config/user.db")),
            moviepilot_category_file=Path(
                os.environ.get(
                    "MOVIEPILOT_CATEGORY_FILE",
                    "/moviepilot-config/category.yaml",
                )
            ),
            rclone_config=Path(os.environ.get("RCLONE_CONFIG", "/rclone/rclone.conf")),
            drive_remote=os.environ.get("DRIVE_REMOTE", "MP:Media"),
            min_free_gib=_integer("MIN_FREE_GIB", 10),
            max_active_downloads=_integer("MAX_ACTIVE_DOWNLOADS", 2),
            auto_cleanup_completed=_boolean("AUTO_CLEANUP_COMPLETED", True),
            cleanup_interval_seconds=_integer("CLEANUP_INTERVAL_SECONDS", 60, 30),
        )
