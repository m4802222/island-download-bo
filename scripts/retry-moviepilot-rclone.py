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
RECOVERY_REMOTE = "gdrive2"
RECOVERY_PROBE_PREFIX = ".islandbot-gdrive2-recovery-probe"

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
from islandbot.transfer_verification import (  # noqa: E402
    load_successful_transfer_proofs,
    verified_transfer_sources,
)

PROBE_DELAYS = {
    HEALTHY: (30 * 60,),
    NETWORK: (5 * 60, 15 * 60, 30 * 60, 60 * 60),
    QUOTA: (30 * 60, 60 * 60, 2 * 60 * 60, 4 * 60 * 60, 6 * 60 * 60),
    AUTH: (6 * 60 * 60,),
    UNKNOWN: (30 * 60, 60 * 60, 2 * 60 * 60, 6 * 60 * 60),
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


def moviepilot_upload_active() -> bool:
    """Avoid probing or retrying alongside MoviePilot's own rclone upload."""

    result = run(
        [
            "docker",
            "exec",
            MOVIEPILOT_CONTAINER,
            "pgrep",
            "-f",
            "rclone copyto",
        ],
        timeout=10,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def cleanup_stale_probes(
    remote_name: str = "MP",
    prefix: str = ".islandbot-rclone-probe",
) -> None:
    """Remove only old files created by this worker's write probes."""

    result = run(
        [
            "docker",
            "exec",
            MOVIEPILOT_CONTAINER,
            "rclone",
            "delete",
            f"{remote_name}:/",
            "--include",
            f"{prefix}-*.txt",
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


def probe_remote(
    remote_name: str = "MP",
    prefix: str = ".islandbot-rclone-probe",
) -> tuple[str, str]:
    cleanup_stale_probes(remote_name, prefix)
    name = f"{prefix}-{os.getpid()}-{int(time.time())}.txt"
    remote = f"{remote_name}:/{name}"
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


def moviepilot_alias_remote() -> str | None:
    """Return the exact remote selected by MoviePilot's MP alias."""

    try:
        result = run(
            [
                "docker",
                "exec",
                MOVIEPILOT_CONTAINER,
                "rclone",
                "config",
                "redacted",
                "MP",
            ],
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "remote":
            return value.strip().removesuffix(":")
    return None


def recovery_probe_delay(status: str) -> int:
    """Probe gdrive2 often enough to notify soon without API churn."""

    if status == NETWORK:
        return 15 * 60
    if status in {AUTH, HEALTHY}:
        return 6 * 60 * 60
    return 30 * 60


def monitor_gdrive2_recovery(state: dict, now: float) -> None:
    """Notify once when gdrive2 becomes writable during a gdrive1 fallback."""

    if moviepilot_alias_remote() != "gdrive1":
        return

    monitor = (
        state.get("gdrive2_monitor")
        if isinstance(state.get("gdrive2_monitor"), dict)
        else {}
    )
    if now < float(monitor.get("next_probe") or 0):
        return

    old_status = str(monitor.get("status") or UNKNOWN)
    seen_unhealthy = bool(monitor.get("seen_unhealthy", True))
    recovery_notified = bool(monitor.get("recovery_notified", False))
    status, output = probe_remote(RECOVERY_REMOTE, RECOVERY_PROBE_PREFIX)

    if status != HEALTHY:
        seen_unhealthy = True
        recovery_notified = False
    elif seen_unhealthy and not recovery_notified:
        recovery_notified = notify(
            "✅ gdrive2 共享云盘已恢复写入\n\n"
            "极小文件写入探测已经成功。\n"
            "MoviePilot 目前仍使用 gdrive1，系统没有自动切换。\n"
            "请确认后再手动切回 gdrive2。\n"
            "此通知不会自动删除。"
        )

    next_delay = recovery_probe_delay(status)
    if status == HEALTHY and seen_unhealthy and not recovery_notified:
        next_delay = 5 * 60
    state["gdrive2_monitor"] = {
        "status": status,
        "next_probe": now + next_delay,
        "last_probe": now,
        "seen_unhealthy": seen_unhealthy,
        "recovery_notified": recovery_notified,
    }
    print(
        f"gdrive2 recovery probe status={status} previous={old_status} "
        f"output={output[-300:].replace(chr(10), ' | ')}",
        flush=True,
    )


def probe_delay(status: str, consecutive_failures: int) -> int:
    """Return a bounded progressive delay for the next backend probe."""

    delays = PROBE_DELAYS.get(status, PROBE_DELAYS[UNKNOWN])
    index = max(0, consecutive_failures - 1)
    return delays[min(index, len(delays) - 1)]


def refresh_backend(state: dict, now: float) -> tuple[str, bool, str]:
    """Probe when due and persist the effective Google Drive backend state."""

    backend = state.get("backend") if isinstance(state.get("backend"), dict) else {}
    old_status = str(backend.get("status") or UNKNOWN)
    next_probe = float(backend.get("next_probe") or 0)
    if now < next_probe:
        return old_status, False, ""

    status, output = probe_remote()
    old_failures = max(0, int(backend.get("consecutive_failures") or 0))
    if status == HEALTHY:
        consecutive_failures = 0
    elif status == old_status:
        consecutive_failures = old_failures + 1
    else:
        consecutive_failures = 1
    state["backend"] = {
        "status": status,
        "next_probe": now + probe_delay(status, consecutive_failures),
        "last_probe": now,
        "consecutive_failures": consecutive_failures,
    }
    return status, True, output


def remote_destination_size(destination: str) -> int | None:
    """Read one live destination size through MoviePilot's MP remote."""

    result = run(
        [
            "docker",
            "exec",
            MOVIEPILOT_CONTAINER,
            "rclone",
            "lsjson",
            "--stat",
            "--no-mimetype",
            "--no-modtime",
            f"MP:{destination}",
            "--contimeout",
            "10s",
            "--timeout",
            "20s",
            "--retries",
            "1",
            "--low-level-retries",
            "1",
        ],
        timeout=30,
    )
    if result.returncode != 0:
        return None
    try:
        item = json.loads(result.stdout)
        if not isinstance(item, dict) or item.get("IsDir") is not False:
            return None
        return int(item.get("Size"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def pending_failures():
    """Return unresolved failures and the subset whose local source can retry."""

    failures = pending_rclone_failures(
        DATABASE,
        source_exists=lambda _: True,
        success_verified=lambda _: False,
    )
    if not failures:
        return {}, set()
    proofs = load_successful_transfer_proofs(DATABASE, set(failures))
    proofs = {
        source: proof
        for source, proof in proofs.items()
        if proof.history_id > failures[source].history_id
    }
    verified, _ = verified_transfer_sources(
        proofs,
        remote_destination_size,
    )
    unresolved = {}
    retryable = set()
    for source, failure in failures.items():
        if source in verified:
            continue
        if source_exists(source):
            unresolved[source] = failure
            retryable.add(source)
        elif source in proofs:
            # MoviePilot move removed the source, but cloud proof has not yet
            # passed. Keep downloads blocked without attempting a destructive
            # redo that cannot succeed without the local source.
            unresolved[source] = failure
    return unresolved, retryable


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


def notify(text: str) -> bool:
    """Send a persistent Telegram message that is never auto-deleted."""

    code = "import sys; from islandbot.app import OWNER,send; send(OWNER,sys.argv[1])"
    result = run(
        ["docker", "exec", BOT_CONTAINER, "python", "-c", code, text[:3500]],
        timeout=30,
    )
    if result.returncode != 0:
        print("Telegram notification failed", flush=True)
        return False
    return True


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


def activate_block(status: str, block: dict) -> tuple[list[str], list[str]]:
    """Pause every ordinary in-flight download while preserving brush tasks."""

    already_paused = [str(item) for item in block.get("paused_hashes") or []]
    newly_paused = _qbit_action("pause")
    paused_hashes = sorted(set(already_paused + newly_paused))
    already_paused_aria2 = [str(item) for item in block.get("paused_aria2") or []]
    newly_paused_aria2 = _aria2_action("pause")
    paused_aria2 = sorted(set(already_paused_aria2 + newly_paused_aria2))
    set_block(True, status, paused_hashes, paused_aria2)
    return paused_hashes, paused_aria2


def process(dry_run: bool = False) -> None:
    now = time.time()
    failures, retryable = pending_failures()
    state = load_json(STATE_FILE)
    block = cloud_block_status(BLOCK_FILE)

    if dry_run:
        print(f"DRY_RUN pending={len(failures)} block={bool(block.get('active'))}")
        for failure in failures.values():
            print(f"#{failure.history_id} {failure.title}: {failure.source}")
        return

    if moviepilot_upload_active():
        backend = state.get("backend") if isinstance(state.get("backend"), dict) else {}
        backend["next_probe"] = now + 60
        state["backend"] = backend
        if failures or block.get("active"):
            status = str(block.get("reason") or backend.get("status") or UNKNOWN)
            activate_block(status, block)
        save_json(STATE_FILE, state)
        print("MoviePilot upload active; probe and retry deferred", flush=True)
        return

    monitor_gdrive2_recovery(state, now)

    old_backend = state.get("backend") if isinstance(state.get("backend"), dict) else {}
    old_status = str(old_backend.get("status") or UNKNOWN)
    status, probed, probe_output = refresh_backend(state, now)
    if block.get("active") and not probed:
        # A persisted block is authoritative until a new successful write
        # probe proves recovery.  Never clear it merely because an older
        # state file happened to say healthy.
        status = str(block.get("reason") or status or UNKNOWN)
    if probed:
        print(
            f"rclone probe status={status} "
            f"output={probe_output[-300:].replace(chr(10), ' | ')}",
            flush=True,
        )

    if not failures:
        state["items"] = {}
        if status != HEALTHY:
            paused_hashes, paused_aria2 = activate_block(status, block)
            if not block.get("active"):
                notify(
                    "⚠️ Google Drive 主动检测异常，已暂停普通下载\n\n"
                    f"状态：{STATUS_TEXT[status]}\n"
                    f"已暂停普通下载：{len(paused_hashes) + len(paused_aria2)} 个\n"
                    "尚未发生新的入库失败；刷流任务不会暂停或删除。"
                )
            elif probed and status != old_status:
                notify(f"ℹ️ Google Drive 状态变化\n\n{STATUS_TEXT[status]}")
            save_json(STATE_FILE, state)
            print(f"Upload gate blocked: {STATUS_TEXT[status]}", flush=True)
            return
        if block.get("active"):
            clear_block(block, "写入探测已经恢复，且没有待处理失败文件。")
        else:
            set_block(False, HEALTHY, [], [])
        save_json(STATE_FILE, state)
        print("No pending rclone upload failures", flush=True)
        return

    due, state, new = update_retry_state(
        failures,
        state,
        now,
        backend_status=status,
        allow_retry=status == HEALTHY,
        retryable_sources=retryable,
    )

    paused_hashes, paused_aria2 = activate_block(status, block)

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

    redo_succeeded = []
    for failure in due:
        if moviepilot_upload_active():
            print("MoviePilot upload became active; remaining retries deferred", flush=True)
            break
        success, message = redo(failure.history_id)
        attempts = int(state["items"][failure.source].get("attempts") or 0)
        print(
            f"MoviePilot retry #{failure.history_id} success={success} message={message[:300]}",
            flush=True,
        )
        if success:
            redo_succeeded.append(failure)
        elif attempts in {1, 3, 6}:
            notify(
                f"⚠️ 自动重试上传仍失败（第 {attempts} 次）\n\n"
                f"{failure.title}\n原因：{message[:500]}"
            )

    remaining, _ = pending_failures()
    for failure in redo_succeeded:
        if failure.source not in remaining:
            notify(
                "✅ 自动重试上传成功并通过云盘校验\n\n"
                f"{failure.title}\nMoviePilot 记录 #{failure.history_id}"
            )
        else:
            print(
                f"MoviePilot retry #{failure.history_id} awaiting remote verification",
                flush=True,
            )
    if not remaining:
        clear_block(cloud_block_status(BLOCK_FILE), "失败文件已重新上传成功。")
        state["items"] = {}
    save_json(STATE_FILE, state)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--probe-gdrive2", action="store_true")
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
        if args.probe_gdrive2:
            status, output = probe_remote(RECOVERY_REMOTE, RECOVERY_PROBE_PREFIX)
            print(
                f"GDRIVE2_PROBE status={status} "
                f"output={output[-500:].replace(chr(10), ' | ')}"
            )
            raise SystemExit(0 if status == HEALTHY else 1)
        process(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
