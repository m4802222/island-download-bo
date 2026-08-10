"""Quark/QAS operations used by the download workflow."""

from __future__ import annotations

import urllib.parse
from typing import Callable

from ..library import missing_plan
from ..media import VIDEO_EXTENSIONS, explicit_seasons, parse_identity_label


class QuarkService:
    """QAS API adapter and conservative missing-episode planner."""

    def __init__(
        self,
        qas_open: Callable,
        save_path: str,
        history_lookup: Callable[[str], tuple[list[str], bool]],
        drive_lookup: Callable[[str], tuple[list[str], bool]],
    ):
        self.qas_open = qas_open
        self.save_path = save_path.rstrip("/")
        self.history_lookup = history_lookup
        self.drive_lookup = drive_lookup

    def task(self, share_url, task_name, media_title=None, pattern=""):
        aria_save_path = "incoming"
        if media_title:
            identity = parse_identity_label(media_title)
            if not identity:
                raise RuntimeError("媒体身份格式异常，未提交 QAS")
            aria_save_path = f"incoming/{identity.task_label}"
        return {
            "taskname": task_name,
            "shareurl": share_url,
            "savepath": f"{self.save_path}/{task_name}",
            "pattern": pattern,
            "replace": "",
            "addition": {
                "aria2": {
                    "auto_download": True,
                    "download_subdir": True,
                    "save_path": aria_save_path,
                    "pause": False,
                }
            },
        }

    def share_folders(self, share_url, stoken=None):
        payload = {"shareurl": share_url}
        if stoken:
            payload["stoken"] = stoken
        response = self.qas_open("/get_share_detail", payload)
        data = response.json()
        if not data.get("success"):
            error = (
                data.get("data", {}).get("error")
                or data.get("message")
                or "夸克链接解析失败"
            )
            raise RuntimeError(error)
        entries = data.get("data", {}).get("list", [])
        folders = [
            {"fid": item["fid"], "name": item["file_name"]}
            for item in entries
            if item.get("dir") and item.get("fid") and item.get("file_name")
        ]
        return folders, data.get("data", {}).get("stoken")

    def share_video_files(self, share_url):
        response = self.qas_open("/get_share_detail", {"shareurl": share_url})
        data = response.json()
        if not data.get("success"):
            error = (
                data.get("data", {}).get("error")
                or data.get("message")
                or "夸克目录读取失败"
            )
            raise RuntimeError(error)
        return [
            item["file_name"]
            for item in data.get("data", {}).get("list", [])
            if not item.get("dir")
            and item.get("file_name", "").lower().endswith(VIDEO_EXTENSIONS)
        ]

    def missing_plan(self, share_url, media_title):
        files = self.share_video_files(share_url)
        if not files:
            return "", 0, 0
        identity = parse_identity_label(media_title)
        if not identity:
            raise RuntimeError("媒体身份格式异常，未进行缺集判断")
        source_seasons = {
            season for name in files for season in explicit_seasons(name)
        }
        if source_seasons and source_seasons != {identity.season}:
            seasons = "、".join(f"第 {season} 季" for season in sorted(source_seasons))
            raise RuntimeError(
                f"分享文件明确标记为 {seasons}，但当前确认身份是第 {identity.season} 季。"
                "已停止下载，请修改媒体身份后重试。"
            )
        history_paths, _ = self.history_lookup(media_title)
        drive_paths, _ = self.drive_lookup(media_title)
        plan = missing_plan(files, [*history_paths, *drive_paths], identity)
        if plan.complete:
            return None, plan.total, plan.skipped
        return plan.include_regex, plan.total, plan.skipped

    def download_choices(self, share_url, folder_title: Callable[[str], str | None], max_depth=5):
        current_url = share_url
        title_hint = None
        stoken = None
        for _ in range(max_depth):
            folders, stoken = self.share_folders(current_url, stoken)
            if len(folders) != 1:
                return current_url, folders, title_hint
            wrapper = folders[0]
            inferred = folder_title(wrapper["name"])
            if inferred:
                title_hint = inferred
            current_url = self.selected_share_url(current_url, wrapper)
        folders, _ = self.share_folders(current_url, stoken)
        return current_url, folders, title_hint

    @staticmethod
    def selected_share_url(share_url, folder):
        base = share_url.split("#", 1)[0]
        name = urllib.parse.quote(folder["name"], safe="")
        return f"{base}#/list/share/{folder['fid']}-{name}"
