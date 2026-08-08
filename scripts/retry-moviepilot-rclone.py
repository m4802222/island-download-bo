#!/usr/bin/env python3
"""Retry failed MoviePilot rclone uploads from the Docker host."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BOT_DIR = Path("/opt/media/downloadbot")
DATABASE = Path("/opt/media/moviepilot/config/user.db")
DOWNLOADS = Path("/opt/media/downloads")
STATE_FILE = BOT_DIR / "data" / "rclone_retry.json"
LOCK_FILE = BOT_DIR / "data" / "rclone_retry.lock"
BLOCK_FILE = BOT_DIR / "data" / "cloud-upload-block.json"
MOVIEPILOT_CONTAINER = "moviepilot"
BOT_CONTAINER = "island-download-bot"

sys.path.insert(0, str(BOT_DIR))

from islandbot.retry import (  # noqa: E402
    AUTH,
    HEALTHY,
    NETWORK,
    QUOTA,
    UNKNOWN,
    classify_probe_error,
    cloud_block_status,
    pending_rclone_failures,
    update_retry_state,
)

PROBE_DELAYS = {
    HEALTHY: 5 * 60,
    NETWORK: 15 * 60,
    QUOTA: 30 * 60,
    AUTH: 30 * 60,
    UNKNOWN: 30 * 60,
}
STATUS_TEXT = {
    HEALTHY: "Google Drive 写入检测正常",
    NETWORK: "VPS 到 Google Drive 的网络连接异常",
    QUOTA: "Google Drive 当前限制该授权用户写入",
    AUTH: "Google Drive OAuth 授权已经失效",
    UNKNOWN: "Google Drive 写入检测失败，原因尚未分类",
}


def source_exists(source: str) -> bool:
    prefix = "/downloads/"
    if not source.startswith(prefix):
        return False
    return (DOWNLOADS / source[len(prefix):]).is_file()


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def cleanup_stale_probes() -> None:
    """Remove only old files created by this worker's write probes."""

    result = run(
        [
            "docker",
            "exec",
            MOVIEPILOT_CONTAINER,
            "rclone",
            "delete",
            "MP:/",
            "--include",
            ".islandbot-rclone-probe-*.txt",
            "--max-depth",
            "1",
            "--min-age",
            "1m",
            "--retries",
            "1",
            "--low-level-retries",
            "1",
        ],
        timeout=40,
    )
    if result.returncode != 0:
        print("Deferred rclone probe cleanup is still unavailable", flush=True)


def delete_probe(remote: str) -> bool:
    result = run(
        [
            "docker",
            "exec",
            MOVIEPILOT_CONTAINER,
            "rclone",
            "deletefile",
            remote,
            "--retries",
            "1",
            "--low-level-retries",
            "1",
        ],
        timeout=30,
    )
    return result.returncode == 0


def probe_remote() -> tuple[str, str]:
    cleanup_stale_probes()
    name = f".islandbot-rclone-probe-{os.getpid()}-{int(time.time())}.txt"
    remote = f"MP:/{name}"
    result = run(
        [
            "docker",
            "exec",
            MOVIEPILOT_CONTAINER,
            "rclone",
            "copyto",
            "/etc/hostname",
            remote,
            "--contimeout",
            "10s",
            "--timeout",
            "20s",
            "--retries",
            "1",
            "--low-level-retries",
            "1",
        ],
        timeout=40,
    )
    output = (result.stdout + result.stderr).strip()
    status = classify_probe_error(output, result.returncode)
    if status == HEALTHY:
        for delay in (0, 3, 10, 30):
            if delay:
                time.sleep(delay)
            if delete_probe(remote):
                break
        else:
            print("rclone probe cleanup deferred until the next check", flush=True)
    return status, output[-1000:]


def redo(history_id: int) -> tuple[bool, str]:
    code = (
        "import json; from app.chain.transfer import TransferChain; "
        f"ok,msg=TransferChain().redo_transfer_history({history_id}); "
        "print('__ISLAND_RETRY__'+json.dumps({'success':bool(ok),'message':str(msg or '')},ensure_ascii=False))"
    )
    result = run(
        [
            "timeout",
            "20m",
            "docker",
            "exec",
            MOVIEPILOT_CONTAINER,
            "python",
            "-c",
            code,
        ],
        timeout=1210,
    )
    output = (result.stdout + result.stderr).strip()
    payload = None
    for line in output.splitlines():
        if line.startswith("__ISLAND_RETRY__"):
            try:
                payload = json.loads(line.removeprefix("__ISLAND_RETRY__"))
            except json.JSONDecodeError:
                payload = None
    if result.returncode == 0 and isinstance(payload, dict):
        return bool(payload.get("success")), str(payload.get("message") or "")
    return False, output[-500:] or f"退出码 {result.returncode}"


def notify(text: str) -> None:
    code = "import sys; from islandbot.app import OWNER,send; send(OWNER,sys.argv[1])"
    result = run(
        ["docker", "exec", BOT_CONTAINER, "python", "-c", code, text[:3500]],
        timeout=30,
    )
    if result.returncode != 0:
        print("Telegram notification failed", flush=True)


def _qbit_action(action: str, requested: list[str] | None = None) -> list[str]:
    code = """
import json,sys
from islandbot.app import QBIT_CLIENT
from islandbot.cleanup import is_brush_task
tasks=QBIT_CLIENT.request('/api/v2/torrents/info').json()
requested=set(json.loads(sys.argv[2])) if len(sys.argv)>2 else set()
paused={'pausedDL','pausedUP','stoppedDL','stoppedUP'}
if sys.argv[1]=='pause':
    chosen=[str(t.get('hash') or '') for t in tasks if float(t.get('progress') or 0)<1 and not is_brush_task(t) and t.get('state') not in paused]
else:
    chosen=[str(t.get('hash') or '') for t in tasks if str(t.get('hash') or '') in requested and float(t.get('progress') or 0)<1 and not is_brush_task(t) and t.get('state') in paused]
chosen=[item for item in chosen if item]
if chosen:
    QBIT_CLIENT.action(sys.argv[1],'|'.join(chosen))
print('__ISLAND_QBIT__'+json.dumps(chosen))
""".strip()
    command = ["docker", "exec", BOT_CONTAINER, "python", "-c", code, action]
    if requested is not None:
        command.append(json.dumps(requested))
    result = run(command, timeout=40)
    for line in result.stdout.splitlines():
        if line.startswith("__ISLAND_QBIT__"):
            try:
                value = json.loads(line.removeprefix("__ISLAND_QBIT__"))
                return value if isinstance(value, list) else []
            except json.JSONDecodeError:
                break
    print(f"qBittorrent {action} failed", flush=True)
    return []


def _aria2_action(action: str, requested: list[str] | None = None) -> list[str]:
    code = """
import json,sys
from islandbot.app import ARIA2_CLIENT,ARIA2_TRACKED
items={str(t.get('gid') or ''):t for t in ARIA2_CLIENT.recent()}
requested=set(json.loads(sys.argv[2])) if len(sys.argv)>2 else set()
if sys.argv[1]=='pause':
    chosen=[gid for gid,t in items.items() if gid in ARIA2_TRACKED and t.get('status') in {'active','waiting'}]
    method='aria2.forcePause'
else:
    chosen=[gid for gid,t in items.items() if gid in requested and t.get('status')=='paused']
    method='aria2.unpause'
completed=[]
for gid in chosen:
    ARIA2_CLIENT.call(method,[gid])
    completed.append(gid)
print('__ISLAND_ARIA2__'+json.dumps(completed))
""".strip()
    command = ["docker", "exec", BOT_CONTAINER, "python", "-c", code, action]
    if requested is not None:
        command.append(json.dumps(requested))
    result = run(command, timeout=40)
    for line in result.stdout.splitlines():
        if line.startswith("__ISLAND_ARIA2__"):
            try:
                value = json.loads(line.removeprefix("__ISLAND_ARIA2__"))
                return value if isinstance(value, list) else []
            except json.JSONDecodeError:
                break
    print(f"Aria2 {action} failed", flush=True)
    return []


def set_block(
    active: bool,
    reason: str,
    paused_hashes: list[str],
    paused_aria2: list[str],
) -> None:
    save_json(
        BLOCK_FILE,
        {
            "active": active,
            "reason": reason,
            "paused_hashes": sorted(set(paused_hashes)),
            "paused_aria2": sorted(set(paused_aria2)),
            "updated_at": time.time(),
        },
    )


def clear_block(block: dict, notification: str | None = None) -> None:
    paused = [str(item) for item in block.get("paused_hashes") or []]
    paused_aria2 = [str(item) for item in block.get("paused_aria2") or []]
    resumed = _qbit_action("resume", paused) if paused else []
    resumed_aria2 = _aria2_action("resume", paused_aria2) if paused_aria2 else []
    set_block(False, HEALTHY, [], [])
    if notification:
        resumed_count = len(resumed) + len(resumed_aria2)
        suffix = f"\n已恢复普通下载：{resumed_count} 个" if resumed_count else ""
        notify(f"✅ Google Drive 上传通道已恢复\n\n{notification}{suffix}")


def process(dry_run: bool = False) -> None:
    now = time.time()
    failures = pending_rclone_failures(DATABASE, source_exists=source_exists)
    state = load_json(STATE_FILE)
    block = cloud_block_status(BLOCK_FILE)

    if dry_run:
        print(f"DRY_RUN pending={len(failures)} block={bool(block.get('active'))}")
        for failure in failures.values():
            print(f"#{failure.history_id} {failure.title}: {failure.source}")
        return

    if not failures:
        if block.get("active"):
            clear_block(block, "待处理上传已经全部成功或源文件已不再存在。")
        else:
            set_block(False, HEALTHY, [], [])
        state["items"] = {}
        state["backend"] = {
            "status": HEALTHY,
            "next_probe": now + PROBE_DELAYS[HEALTHY],
        }
        save_json(STATE_FILE, state)
        print("No pending rclone upload failures", flush=True)
        return

    backend = state.get("backend") if isinstance(state.get("backend"), dict) else {}
    old_status = str(backend.get("status") or UNKNOWN)
    status = old_status
    if now >= float(backend.get("next_probe") or 0):
        status, probe_output = probe_remote()
        backend = {
            "status": status,
            "next_probe": now + PROBE_DELAYS[status],
            "last_probe": now,
        }
        print(
            f"rclone probe status={status} output={probe_output[-300:].replace(chr(10), ' | ')}",
            flush=True,
        )
    state["backend"] = backend

    due, state, new = update_retry_state(
        failures,
        state,
        now,
        backend_status=status,
        allow_retry=status == HEALTHY,
    )

    already_paused = [str(item) for item in block.get("paused_hashes") or []]
    newly_paused = _qbit_action("pause")
    paused_hashes = sorted(set(already_paused + newly_paused))
    already_paused_aria2 = [str(item) for item in block.get("paused_aria2") or []]
    newly_paused_aria2 = _aria2_action("pause")
    paused_aria2 = sorted(set(already_paused_aria2 + newly_paused_aria2))
    set_block(True, status, paused_hashes, paused_aria2)

    if not block.get("active"):
        titles = "、".join(item.title for item in list(failures.values())[:3])
        notify(
            "⚠️ Google Drive 上传失败，已进入自动恢复\n\n"
            f"待处理：{len(failures)} 个文件\n"
            f"媒体：{titles}\n"
            f"状态：{STATUS_TEXT[status]}\n"
            f"已暂停普通下载：{len(paused_hashes) + len(paused_aria2)} 个\n"
            "源文件会保留；刷流任务不会暂停或删除。"
        )
    elif status != old_status:
        notify(f"ℹ️ Google Drive 状态变化\n\n{STATUS_TEXT[status]}")
    elif new:
        notify(f"⚠️ 新增 {len(new)} 个 Google Drive 上传失败文件，源文件已保留。")

    save_json(STATE_FILE, state)
    if status != HEALTHY:
        print(f"Upload backlog blocked: {STATUS_TEXT[status]}", flush=True)
        return

    for failure in due:
        success, message = redo(failure.history_id)
        attempts = int(state["items"][failure.source].get("attempts") or 0)
        print(
            f"MoviePilot retry #{failure.history_id} success={success} message={message[:300]}",
            flush=True,
        )
        if success:
            notify(f"✅ 自动重试上传成功\n\n{failure.title}\nMoviePilot 记录 #{failure.history_id}")
        elif attempts in {1, 3, 6}:
            notify(
                f"⚠️ 自动重试上传仍失败（第 {attempts} 次）\n\n"
                f"{failure.title}\n原因：{message[:500]}"
            )

    remaining = pending_rclone_failures(DATABASE, source_exists=source_exists)
    if not remaining:
        clear_block(cloud_block_status(BLOCK_FILE), "失败文件已重新上传成功。")
        state["items"] = {}
    save_json(STATE_FILE, state)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Retry worker already running", flush=True)
            return
        if args.probe:
            status, output = probe_remote()
            print(
                f"PROBE status={status} "
                f"output={output[-500:].replace(chr(10), ' | ')}"
            )
            raise SystemExit(0 if status == HEALTHY else 1)
        process(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
