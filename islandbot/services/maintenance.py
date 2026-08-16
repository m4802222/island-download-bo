"""Background maintenance orchestration for the download runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class MaintenanceService:
    """Run periodic jobs and completion watchers in a deterministic order."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float],
        wall_time: Callable[[], float],
        get_last_category_sync: Callable[[], float],
        set_last_category_sync: Callable[[float], None],
        category_sync_interval: float,
        category_sync_retry_interval: float,
        synchronize_categories: Callable[[], Any],
        process_inbox: Callable[[], Any],
        run_quark_queue: Callable[[], Any],
        task_list: Callable[[], list[dict]],
        normalize_completed: Callable[[list[dict]], list],
        prepare_completed: Callable[[list[dict]], list[dict]],
        trigger_transfer: Callable[[], Any],
        run_download_queue: Callable[[], Any],
        send_temporary: Callable[[Any, str], Any],
        owner: Any,
        seen: dict[str, float],
        save_seen: Callable[[dict[str, float]], Any],
        aria_tracked: dict[str, dict],
        aria_rpc: Callable[[str, list], Any],
        aria_name: Callable[[dict], str],
        send: Callable[[Any, str], Any],
        save_aria_tracked: Callable[[], Any],
        cleanup_transferred: Callable[[], Any],
        delete_expired: Callable[[], Any],
        log: Callable[[str], Any] = print,
    ):
        self.monotonic = monotonic
        self.wall_time = wall_time
        self.get_last_category_sync = get_last_category_sync
        self.set_last_category_sync = set_last_category_sync
        self.category_sync_interval = category_sync_interval
        self.category_sync_retry_interval = category_sync_retry_interval
        self.synchronize_categories = synchronize_categories
        self.process_inbox = process_inbox
        self.run_quark_queue = run_quark_queue
        self.task_list = task_list
        self.normalize_completed = normalize_completed
        self.prepare_completed = prepare_completed
        self.trigger_transfer = trigger_transfer
        self.run_download_queue = run_download_queue
        self.send_temporary = send_temporary
        self.owner = owner
        self.seen = seen
        self.save_seen = save_seen
        self.aria_tracked = aria_tracked
        self.aria_rpc = aria_rpc
        self.aria_name = aria_name
        self.send = send
        self.save_aria_tracked = save_aria_tracked
        self.cleanup_transferred = cleanup_transferred
        self.delete_expired = delete_expired
        self.log = log

    def tick(self) -> None:
        now = self.monotonic()
        last_sync = self.get_last_category_sync()
        if not last_sync or now - last_sync >= self.category_sync_interval:
            try:
                self.synchronize_categories()
            except Exception as exc:
                self.log(f"media-category-sync error: {exc}")
                retry_at = now - self.category_sync_interval + self.category_sync_retry_interval
                self.set_last_category_sync(retry_at)
        self.process_inbox()
        self.run_quark_queue()
        self.watch_completed()
        self.watch_aria2_completed()
        self.cleanup_transferred()
        self.delete_expired()

    def watch_completed(self) -> None:
        tasks = self.task_list()
        renamed = self.normalize_completed(tasks)
        if renamed:
            tasks = self.task_list()
        ready_tasks = self.prepare_completed(tasks)
        ready_hashes = {str(item.get("hash") or "") for item in ready_tasks}
        newly_done = [
            item
            for item in ready_tasks
            if item.get("progress", 0) >= 1 and item["hash"] not in self.seen
        ]
        if newly_done or (renamed and ready_hashes):
            try:
                self.trigger_transfer()
            except Exception as exc:
                self.log(f"moviepilot-transfer-now error: {exc}")
            cutoff = self.wall_time() - 30 * 24 * 60 * 60
            for item in newly_done:
                self.seen[item["hash"]] = self.wall_time()
                self.save_seen(
                    {item_hash: timestamp for item_hash, timestamp in self.seen.items() if timestamp >= cutoff}
                )
                self.send_temporary(
                    self.owner,
                    "✅ 下载完成\n\n"
                    f"{item.get('name', '')}\n"
                    f"分类：{item.get('category') or '智能分类（MoviePilot）'}\n\n"
                    "MoviePilot 将自动识别、整理并上传到 Google Drive。",
                )
        self.run_download_queue()

    def watch_aria2_completed(self) -> None:
        changed = False
        for gid, tracked in list(self.aria_tracked.items()):
            try:
                item = self.aria_rpc(
                    "aria2.tellStatus",
                    [gid, ["gid", "status", "errorMessage", "files"]],
                )
            except Exception as exc:
                if "HTTP 400" in str(exc):
                    self.aria_tracked.pop(gid, None)
                    changed = True
                    continue
                self.log(f"aria2-watch error: {exc}")
                continue
            status = item.get("status")
            if status == "complete" and not tracked.get("notified"):
                tracked["notified"] = True
                changed = True
                try:
                    self.trigger_transfer()
                    result_text = "已通知 MoviePilot 立即识别、整理并上传 Google Drive。"
                except Exception as exc:
                    self.log(f"moviepilot-transfer-now error: {exc}")
                    result_text = "MoviePilot 立即整理调用失败；文件仍保留在完成目录，请检查整理记录。"
                self.send_temporary(
                    self.owner,
                    "✅ Aria2 下载完成\n\n"
                    f"{tracked.get('name', self.aria_name(item))}\n\n{result_text}",
                )
            elif status == "complete" and tracked.get("notified"):
                self.aria_tracked.pop(gid, None)
                changed = True
            elif status == "error":
                self.send(
                    self.owner,
                    "⚠️ Aria2 下载失败\n\n"
                    f"{tracked.get('name', self.aria_name(item))}\n"
                    f"{item.get('errorMessage') or '请在我的任务中检查。'}",
                )
                self.aria_tracked.pop(gid, None)
                changed = True
        if changed:
            self.save_aria_tracked()
