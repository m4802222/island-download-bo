#!/usr/bin/env python3
"""Daily full-chain health check with Telegram report.

The release deployment installs a systemd timer for 08:00 Asia/Shanghai.
For a manual report, run with ``--dry-run``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

BOT_DIR = Path("/opt/media/downloadbot")
PROJECT_DIR = Path(__file__).resolve().parents[1]
DOWNLOADS = Path("/opt/media/downloads")
MOVIEPILOT_CONTAINER = "moviepilot"
BOT_CONTAINER = "island-download-bot"
BLOCK_FILE = BOT_DIR / "data" / "cloud-upload-block.json"

sys.path.insert(0, str(BOT_DIR if BOT_DIR.is_dir() else PROJECT_DIR))

from islandbot.retry import (  # noqa: E402
    HEALTHY,
    classify_probe_error,
    cloud_block_status,
)
from islandbot.categories import CANONICAL_CATEGORIES  # noqa: E402


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def run(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False
    )


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# Individual checks — each returns (emoji, description)
# ---------------------------------------------------------------------------

def check_disk() -> tuple[str, str]:
    """VPS disk usage."""
    try:
        stat = os.statvfs(str(DOWNLOADS))
        total = stat.f_frsize * stat.f_blocks
        free = stat.f_frsize * stat.f_bfree
        used = total - free
        pct = used / total * 100 if total else 0
        gib = 1024 ** 3
        emoji = "🟢" if pct < 70 else ("🟡" if pct < 85 else "🔴")
        return emoji, f"VPS 磁盘：已用 {used / gib:.0f}GB / {total / gib:.0f}GB（{pct:.0f}%），剩余 {free / gib:.1f}GB"
    except OSError as exc:
        return "🔴", f"VPS 磁盘：检测失败 — {exc}"


def check_rclone_remote(remote: str) -> tuple[str, str]:
    """Test rclone remote read access through the MoviePilot container."""
    try:
        result = run(
            [
                "docker", "exec", MOVIEPILOT_CONTAINER,
                "rclone", "lsd", f"{remote}:/",
                "--max-depth", "1",
                "--contimeout", "10s",
                "--timeout", "20s",
                "--retries", "1",
                "--low-level-retries", "1",
            ],
            timeout=30,
        )
        if result.returncode == 0:
            return "🟢", f"{remote} 挂载：正常"
        output = (result.stdout + result.stderr).strip()[-200:]
        if "invalid_grant" in output or "oauth2" in output.lower():
            return "🔴", f"{remote} OAuth：授权失效"
        return "🔴", f"{remote} 挂载：异常 — {output[:80]}"
    except subprocess.TimeoutExpired:
        return "🔴", f"{remote} 挂载：超时"
    except OSError as exc:
        return "🔴", f"{remote} 挂载：{exc}"


def check_rclone_write() -> tuple[str, str]:
    """Quick rclone write probe through MoviePilot's MP remote."""
    probe_name = f".islandbot-health-probe-{os.getpid()}-{int(time.time())}.txt"
    remote_path = f"MP:/{probe_name}"
    try:
        result = run(
            [
                "docker", "exec", MOVIEPILOT_CONTAINER,
                "rclone", "copyto", "/etc/hostname", remote_path,
                "--contimeout", "10s", "--timeout", "20s",
                "--retries", "1", "--low-level-retries", "1",
            ],
            timeout=40,
        )
        output = (result.stdout + result.stderr).strip()
        status = classify_probe_error(output, result.returncode)
        # Clean up probe file
        run(
            [
                "docker", "exec", MOVIEPILOT_CONTAINER,
                "rclone", "deletefile", remote_path,
                "--retries", "1", "--low-level-retries", "1",
            ],
            timeout=20,
        )
        if status == HEALTHY:
            return "🟢", "Google Drive 写入：正常"
        return "🔴", f"Google Drive 写入：{status}"
    except subprocess.TimeoutExpired:
        return "🔴", "Google Drive 写入：探测超时"
    except OSError as exc:
        return "🔴", f"Google Drive 写入：{exc}"


def check_mp_alias() -> tuple[str, str]:
    """Confirm MoviePilot's MP alias still targets gdrive2 only."""
    try:
        result = run(
            ["docker", "exec", MOVIEPILOT_CONTAINER, "rclone", "config", "redacted", "MP"],
            timeout=10,
        )
        output = result.stdout + result.stderr
        if result.returncode == 0 and "type = alias" in output and "remote = gdrive2:" in output:
            return "🟢", "MP 云盘别名：指向 gdrive2"
        return "🔴", f"MP 云盘别名：异常 — {output.strip()[-120:] or '未找到'}"
    except (subprocess.TimeoutExpired, OSError) as exc:
        return "🔴", f"MP 云盘别名：检测失败 — {exc}"


def check_docker_service(container: str, display_name: str) -> tuple[str, str]:
    """Check if a Docker container is running."""
    try:
        result = run(
            ["docker", "inspect", "--format", "{{.State.Status}}", container],
            timeout=10,
        )
        status = result.stdout.strip()
        if status == "running":
            return "🟢", f"{display_name}：在线"
        return "🔴", f"{display_name}：{status or '未找到'}"
    except (subprocess.TimeoutExpired, OSError):
        return "🔴", f"{display_name}：检测失败"


def check_qbittorrent() -> tuple[str, str]:
    """Check qBittorrent via the bot container's API client."""
    code = (
        "import json; from islandbot.app import QBIT_CLIENT; from islandbot.cleanup import is_brush_task; "
        "tasks=QBIT_CLIENT.request('/api/v2/torrents/info').json(); "
        "active=[t for t in tasks if float(t.get('progress') or 0)<1]; "
        "completed=[t for t in tasks if float(t.get('progress') or 0)>=1 and not is_brush_task(t)]; "
        "stalled=[t for t in tasks if t.get('state')=='stalledDL' and float(t.get('progress') or 0)==0 and not is_brush_task(t)]; "
        "print('__HEALTH__'+json.dumps({'total':len(tasks),'active':len(active),'completed':len(completed),'stalled':len(stalled)}))"
    )
    try:
        result = run(
            ["docker", "exec", BOT_CONTAINER, "python", "-c", code],
            timeout=30,
        )
        for line in result.stdout.splitlines():
            if line.startswith("__HEALTH__"):
                data = json.loads(line.removeprefix("__HEALTH__"))
                total = data.get("total", 0)
                active = data.get("active", 0)
                completed = data.get("completed", 0)
                stalled = data.get("stalled", 0)
                extra = ""
                if completed:
                    extra += f"，{completed} 个普通任务待清理"
                if stalled:
                    extra += f"，{stalled} 个 0% 卡住"
                return ("🟡" if completed or stalled else "🟢"), f"qBittorrent：在线（{total} 个任务，{active} 个下载中{extra}）"
        return "🟡", "qBittorrent：在线但数据读取失败"
    except (subprocess.TimeoutExpired, OSError):
        return "🔴", "qBittorrent：连接失败"


def check_moviepilot() -> tuple[str, str]:
    """Check MoviePilot API."""
    emoji, status = check_docker_service(MOVIEPILOT_CONTAINER, "MoviePilot")
    return emoji, status


def check_aria2() -> tuple[str, str]:
    """Check Aria2 via the bot container."""
    code = (
        "import json; from islandbot.app import ARIA2_CLIENT; "
        "items=ARIA2_CLIENT.recent(); "
        "active=[t for t in items if t.get('status')=='active']; "
        "print('__HEALTH__'+json.dumps({'total':len(items),'active':len(active)}))"
    )
    try:
        result = run(
            ["docker", "exec", BOT_CONTAINER, "python", "-c", code],
            timeout=30,
        )
        for line in result.stdout.splitlines():
            if line.startswith("__HEALTH__"):
                data = json.loads(line.removeprefix("__HEALTH__"))
                return "🟢", f"Aria2：在线（{data.get('active', 0)} 个活动任务）"
        return "🟡", "Aria2：在线但数据读取失败"
    except (subprocess.TimeoutExpired, OSError):
        return "🔴", "Aria2：连接失败"


def check_emby() -> tuple[str, str]:
    """Check Emby via the bot container."""
    code = (
        "import json; from islandbot.app import EMBY_CLIENT; "
        "info=EMBY_CLIENT.system_info(); "
        "print('__HEALTH__'+json.dumps({'name':info.get('ServerName','')}))"
    )
    try:
        result = run(
            ["docker", "exec", BOT_CONTAINER, "python", "-c", code],
            timeout=30,
        )
        for line in result.stdout.splitlines():
            if line.startswith("__HEALTH__"):
                data = json.loads(line.removeprefix("__HEALTH__"))
                name = data.get("name", "")
                return "🟢", f"Emby：在线{f'（{name}）' if name else ''}"
        return "🟡", "Emby：在线但数据读取失败"
    except (subprocess.TimeoutExpired, OSError):
        return "🔴", "Emby：连接失败"


def check_media_mount() -> tuple[str, str]:
    """Confirm the host mediaunion rclone mount is present."""
    try:
        result = run(
            ["findmnt", "-T", "/mnt/gdrive1", "-o", "SOURCE,FSTYPE", "-n"],
            timeout=10,
        )
        value = result.stdout.strip()
        if result.returncode == 0 and "mediaunion" in value and "fuse.rclone" in value:
            return "🟢", "mediaunion 挂载：正常"
        return "🔴", f"mediaunion 挂载：异常 — {value or result.stderr.strip()[:100]}"
    except (subprocess.TimeoutExpired, OSError) as exc:
        return "🔴", f"mediaunion 挂载：检测失败 — {exc}"


def check_media_union() -> tuple[str, str]:
    """Confirm mediaunion contains both read-only cloud remotes."""
    try:
        result = run(["rclone", "config", "redacted", "mediaunion"], timeout=10)
        output = result.stdout + result.stderr
        if (
            result.returncode == 0
            and "type = union" in output
            and "gdrive1:Media" in output
            and "gdrive2:Media" in output
        ):
            return "🟢", "mediaunion 配置：包含 gdrive1 + gdrive2"
        return "🔴", f"mediaunion 配置：异常 — {output.strip()[-120:] or '未找到'}"
    except (subprocess.TimeoutExpired, OSError) as exc:
        return "🔴", f"mediaunion 配置：检测失败 — {exc}"


def check_cloud_categories() -> tuple[str, str]:
    """Confirm the union cloud view exposes all nine canonical directories."""
    try:
        result = run(
            ["rclone", "lsf", "mediaunion:Media", "--dirs-only", "--max-depth", "1"],
            timeout=35,
        )
        names = {line.strip().rstrip("/") for line in result.stdout.splitlines() if line.strip()}
        missing = [name for name in CANONICAL_CATEGORIES if name not in names]
        if result.returncode == 0 and not missing:
            return "🟢", "云盘九分类目录：正常"
        return "🔴", f"云盘九分类目录：缺少 {missing or '读取失败'}"
    except (subprocess.TimeoutExpired, OSError) as exc:
        return "🔴", f"云盘九分类目录：检测失败 — {exc}"


def check_emby_libraries() -> tuple[str, str]:
    """Check the nine canonical Emby libraries and required paths."""
    code = (
        "import json; from islandbot.app import EMBY_CLIENT; "
        "from islandbot.categories import CANONICAL_CATEGORIES; "
        "items=EMBY_CLIENT._request('/Library/VirtualFolders', action='读取媒体库').json(); "
        "by={str(x.get('Name') or ''):x for x in items if isinstance(x,dict)}; "
        "missing=[n for n in CANONICAL_CATEGORIES if n not in by]; "
        "wrong=[n for n in CANONICAL_CATEGORIES if n in by and '/media/'+n not in (by[n].get('Locations') or [])]; "
        "print('__HEALTH__'+json.dumps({'missing':missing,'wrong':wrong},ensure_ascii=False))"
    )
    try:
        result = run(["docker", "exec", BOT_CONTAINER, "python", "-c", code], timeout=30)
        for line in result.stdout.splitlines():
            if line.startswith("__HEALTH__"):
                data = json.loads(line.removeprefix("__HEALTH__"))
                missing = data.get("missing") or []
                wrong = data.get("wrong") or []
                if not missing and not wrong:
                    return "🟢", "Emby 九分类：正常"
                return "🔴", f"Emby 九分类：缺少 {missing or '无'}，路径异常 {wrong or '无'}"
        return "🟡", "Emby 九分类：读取失败"
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        return "🔴", f"Emby 九分类：检测失败 — {exc}"


def check_upload_block() -> tuple[str, str]:
    """Check cloud upload block status."""
    block = cloud_block_status(BLOCK_FILE)
    if block.get("active"):
        reason = block.get("reason", "未知")
        paused = len(block.get("paused_hashes") or []) + len(block.get("paused_aria2") or [])
        return "🔴", f"上传阻断：活跃 — {reason}（已暂停 {paused} 个下载）"
    return "🟢", "上传阻断：未激活"


def check_orphan_files() -> tuple[str, str]:
    """Detect completed downloads not picked up by MoviePilot.

    An "orphan" is an old video file under /opt/media/downloads/complete/.
    Directory mtimes are ignored because category directories are long-lived.
    """
    complete_dir = DOWNLOADS / "complete"
    if not complete_dir.is_dir():
        return "🟢", "孤儿文件：完成目录不存在"
    orphans = []
    cutoff = time.time() - 24 * 60 * 60
    video_extensions = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".mov"}
    try:
        for child in complete_dir.iterdir():
            if child.name.startswith(".") or not child.is_dir():
                continue
            old_videos = []
            for path in child.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in video_extensions:
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        old_videos.append(path)
                except OSError:
                    continue
            if old_videos:
                orphans.append(f"{child.name}（{len(old_videos)}个视频）")
    except OSError as exc:
        return "🟡", f"孤儿文件：扫描失败 — {exc}"
    if not orphans:
        return "🟢", "孤儿文件：无"
    names = "、".join(orphans[:5])
    suffix = f"… 等 {len(orphans)} 个" if len(orphans) > 5 else ""
    return "🟡", f"孤儿文件：{len(orphans)} 个（{names}{suffix}）"


def check_pending_retries() -> tuple[str, str]:
    """Check if there are pending rclone upload retries."""
    state_file = BOT_DIR / "data" / "rclone_retry.json"
    state = load_json(state_file)
    items = state.get("items")
    if not items or not isinstance(items, dict):
        return "🟢", "待重试上传：无"
    count = len(items)
    return "🟡", f"待重试上传：{count} 个文件"


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def build_report() -> str:
    """Run all checks and format the daily report."""
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M")

    checks = [
        check_disk(),
        ("", ""),  # separator
        check_rclone_remote("gdrive1"),
        check_rclone_remote("gdrive2"),
        check_mp_alias(),
        check_rclone_write(),
        check_upload_block(),
        check_pending_retries(),
        ("", ""),  # separator
        check_docker_service(BOT_CONTAINER, "Island Bot"),
        check_qbittorrent(),
        check_moviepilot(),
        check_aria2(),
        check_emby(),
        check_media_mount(),
        check_media_union(),
        check_emby_libraries(),
        check_cloud_categories(),
        ("", ""),  # separator
        check_orphan_files(),
    ]

    lines = [f"📋 每日健康报告  {now}\n"]
    for emoji, text in checks:
        if not emoji and not text:
            lines.append("")
            continue
        lines.append(f"{emoji} {text}")

    # Summary
    red_count = sum(1 for e, _ in checks if e == "🔴")
    yellow_count = sum(1 for e, _ in checks if e == "🟡")
    if red_count:
        lines.append(f"\n⚠️ 发现 {red_count} 个异常，请尽快处理。")
    elif yellow_count:
        lines.append(f"\n💡 发现 {yellow_count} 个警告，建议关注。")
    else:
        lines.append("\n✅ 所有系统正常运行。")

    return "\n".join(lines)


def notify(text: str) -> None:
    """Send report via the bot container's Telegram client."""
    code = "import sys; from islandbot.app import OWNER,send; send(OWNER,sys.argv[1])"
    result = run(
        ["docker", "exec", BOT_CONTAINER, "python", "-c", code, text[:4000]],
        timeout=30,
    )
    if result.returncode != 0:
        print("Telegram notification failed:", result.stderr[:200], flush=True)
        sys.exit(1)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Daily health check")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print report to stdout instead of sending via Telegram",
    )
    parser.add_argument(
        "--check",
        choices=[
            "disk", "gdrive1", "gdrive2", "mp", "write", "qbit",
            "moviepilot", "aria2", "emby", "block", "orphan", "retry",
        ],
        help="Run a single check and print the result",
    )
    args = parser.parse_args()

    if args.check:
        check_map = {
            "disk": check_disk,
            "gdrive1": lambda: check_rclone_remote("gdrive1"),
            "gdrive2": lambda: check_rclone_remote("gdrive2"),
            "mp": check_mp_alias,
            "write": check_rclone_write,
            "qbit": check_qbittorrent,
            "moviepilot": check_moviepilot,
            "aria2": check_aria2,
            "emby": check_emby,
            "block": check_upload_block,
            "orphan": check_orphan_files,
            "retry": check_pending_retries,
        }
        emoji, text = check_map[args.check]()
        print(f"{emoji} {text}")
        return

    report = build_report()

    if args.dry_run:
        print(report)
        return

    print(report, flush=True)
    notify(report)
    print("Health report sent", flush=True)


if __name__ == "__main__":
    main()
