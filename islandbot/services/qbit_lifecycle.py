"""qBittorrent queue, stall detection, promotion gate, and safe cleanup."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Callable

from ..cleanup import is_brush_task, safe_to_cleanup, selected_video_sizes
from ..retry import cloud_block_status
from ..transfer_verification import verified_transfer_sources


class QBitLifecycle:
    """Own qBittorrent lifecycle policy while keeping I/O injectable for tests."""

    def __init__(
        self,
        qbit,
        tasks_loader: Callable[[], list[dict]],
        send_owner: Callable[[str], object],
        owner: str,
        queue: list[str],
        blocked: set[str],
        save_queue: Callable[[], object],
        save_blocked: Callable[[], object],
        queue_ready: bool,
        max_active_downloads: int,
        reserve_gib: int,
        downloads_dir: Path,
        cloud_upload_block_file: Path,
        auto_cleanup_completed: bool,
        cleanup_interval_seconds: int,
        successful_proofs: Callable[[dict[str, int]], dict],
        destination_size: Callable[[str], int | None],
        stall_threshold: int = 30 * 60,
        disk_usage: Callable = shutil.disk_usage,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.qbit = qbit
        self.tasks_loader = tasks_loader
        self.send_owner = send_owner
        self.owner = owner
        self.queue = queue
        self.blocked = blocked
        self.save_queue = save_queue
        self.save_blocked = save_blocked
        self.queue_ready = queue_ready
        self.max_active_downloads = max_active_downloads
        self.reserve_gib = reserve_gib
        self.downloads_dir = downloads_dir
        self.cloud_upload_block_file = cloud_upload_block_file
        self.auto_cleanup_completed = auto_cleanup_completed
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self.successful_proofs = successful_proofs
        self.destination_size = destination_size
        self.stall_threshold = stall_threshold
        self.disk_usage = disk_usage
        self.clock = clock
        self.last_cleanup = 0.0
        self.stall_first_seen: dict[str, float] = {}
        self.stall_notified: set[str] = set()

    @staticmethod
    def file_size(item: dict) -> int:
        if "amount_left" in item:
            return int(item.get("amount_left") or 0)
        return int(item.get("total_size") or item.get("size") or 0)

    def has_enough_space(self, item: dict, free: int | None = None):
        size = self.file_size(item)
        if free is None:
            free = self.disk_usage(self.downloads_dir).free
        if size <= 0:
            return True, size, free
        return size <= max(0, free - self.reserve_gib * 1024**3), size, free

    def _action(self, action: str, torrent_hash: str, delete_files: bool = False):
        return self.qbit.action(action, torrent_hash, delete_files)

    def run_queue(self):
        """Keep up to the configured number of ordinary bot tasks active."""

        tasks = self.tasks_loader()
        task_by_hash = {item["hash"]: item for item in tasks}
        if not self.queue_ready:
            self.queue.extend(
                item["hash"]
                for item in sorted(tasks, key=lambda item: item.get("added_on", 0))
                if item.get("progress", 0) < 1
            )
            self.queue_ready = True

        self.queue[:] = [
            task_hash
            for task_hash in self.queue
            if task_hash in task_by_hash
            and task_by_hash[task_hash].get("progress", 0) < 1
        ]
        self.save_queue()
        stale_blocked = self.blocked - set(self.queue)
        if stale_blocked:
            self.blocked.difference_update(stale_blocked)
            self.save_blocked()
        if not self.queue:
            return

        cloud_block = cloud_block_status(self.cloud_upload_block_file)
        if cloud_block.get("active"):
            for task_hash in self.queue:
                item = task_by_hash.get(task_hash)
                if item and not is_brush_task(item) and item.get("state") not in {
                    "pausedDL", "pausedUP", "stoppedDL", "stoppedUP",
                }:
                    self._action("pause", task_hash)
            return

        active_hashes = self.queue[: self.max_active_downloads]
        for task_hash in self.queue[self.max_active_downloads :]:
            item = task_by_hash.get(task_hash)
            if item and item.get("state") not in {
                "pausedDL", "pausedUP", "stoppedDL", "stoppedUP",
            }:
                self._action("pause", task_hash)

        free = self.disk_usage(self.downloads_dir).free
        available = max(0, free - self.reserve_gib * 1024**3)
        for task_hash in active_hashes:
            item = task_by_hash.get(task_hash)
            if not item:
                continue
            fits, size, _ = self.has_enough_space(
                item, available + self.reserve_gib * 1024**3
            )
            if not fits:
                if item.get("state") not in {
                    "pausedDL", "pausedUP", "stoppedDL", "stoppedUP",
                }:
                    self._action("pause", task_hash)
                if task_hash not in self.blocked:
                    self.blocked.add(task_hash)
                    self.save_blocked()
                    self.send_owner(
                        "⚠️ 队列暂停：空间不足\n\n"
                        f"{item.get('name', '')[:55]}\n"
                        f"剩余需下载：{size / 1024**3:.1f} GB\n"
                        f"当前可用：{free / 1024**3:.1f} GB\n"
                        f"安全预留：{self.reserve_gib} GB\n\n"
                        "等待 MoviePilot 上传并清理文件后，将自动继续。"
                    )
                continue
            available = max(0, available - size)
            if task_hash in self.blocked:
                self.blocked.remove(task_hash)
                self.save_blocked()
            if item.get("state") in {
                "pausedDL", "pausedUP", "stoppedDL", "stoppedUP",
            }:
                self._action("resume", task_hash)

        now = self.clock()
        for task_hash in active_hashes:
            item = task_by_hash.get(task_hash)
            if not item:
                continue
            if float(item.get("progress") or 0) > 0:
                self.stall_first_seen.pop(task_hash, None)
                self.stall_notified.discard(task_hash)
                continue
            if item.get("state") in {
                "pausedDL", "pausedUP", "stoppedDL", "stoppedUP",
            }:
                continue
            if task_hash not in self.stall_first_seen:
                self.stall_first_seen[task_hash] = now
                continue
            if now - self.stall_first_seen[task_hash] >= self.stall_threshold:
                self._action("pause", task_hash)
                if task_hash not in self.stall_notified:
                    self.stall_notified.add(task_hash)
                    self.send_owner(
                        "⚠️ 任务卡死已自动暂停\n\n"
                        f"{item.get('name', '')[:55]}\n"
                        f"已停留 0% 超过 {self.stall_threshold // 60} 分钟。\n\n"
                        "建议在「我的任务」中删除并换种。"
                    )
        stale_stall = set(self.stall_first_seen) - set(self.queue)
        for task_hash in stale_stall:
            self.stall_first_seen.pop(task_hash, None)
            self.stall_notified.discard(task_hash)

    def cleanup_transferred(self):
        """Delete only ordinary tasks verified by MoviePilot and the remote."""

        if not self.auto_cleanup_completed:
            return
        now = self.clock()
        if now - self.last_cleanup < self.cleanup_interval_seconds:
            return
        self.last_cleanup = now
        if cloud_block_status(self.cloud_upload_block_file).get("active"):
            return

        try:
            tasks = self.qbit.request("/api/v2/torrents/info").json()
        except Exception as exc:
            print(f"qbit-cleanup check failed: {exc}", flush=True)
            return

        candidates = []
        expected_sizes = {}
        task_files = {}
        for task in tasks if isinstance(tasks, list) else []:
            if float(task.get("progress") or 0) < 1 or is_brush_task(task):
                continue
            task_hash = str(task.get("hash") or "")
            if not task_hash:
                continue
            try:
                files = self.qbit.files(task_hash)
            except Exception as exc:
                print(f"qbit-cleanup files {task_hash[:12]} failed: {exc}", flush=True)
                continue
            sizes = selected_video_sizes(task, files)
            if not sizes or not safe_to_cleanup(task, files, set(sizes)):
                continue
            task_files[task_hash] = files
            candidates.append(task)
            expected_sizes.update(sizes)

        if not candidates:
            return
        try:
            proofs = self.successful_proofs(expected_sizes)
            sources, rejected = verified_transfer_sources(
                proofs,
                self.destination_size,
                expected_sizes,
            )
        except Exception as exc:
            print(f"qbit-cleanup verification failed: {exc}", flush=True)
            return

        missing = set(expected_sizes) - set(proofs)
        if missing or rejected:
            print(
                "qbit-cleanup retained unverified files: "
                f"missing_history={len(missing)} rejected={len(rejected)}",
                flush=True,
            )

        removed = []
        for task in candidates:
            task_hash = str(task.get("hash") or "")
            try:
                files = task_files[task_hash]
                if not safe_to_cleanup(task, files, sources):
                    continue
                self.qbit.action("delete", task_hash, delete_files=True)
                removed.append(str(task.get("name") or task_hash[:12]))
            except Exception as exc:
                print(f"qbit-cleanup task {task_hash[:12]} failed: {exc}", flush=True)

        if removed:
            print(
                f"qbit-cleanup removed {len(removed)} transferred task(s): "
                + " | ".join(removed),
                flush=True,
            )
