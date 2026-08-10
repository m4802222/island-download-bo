"""Normalize completed qBittorrent episode files before MoviePilot scans them."""

from __future__ import annotations

import posixpath
import re
import time
import urllib.parse
from pathlib import Path, PurePosixPath

from ..cleanup import is_brush_task
from ..media import (
    VIDEO_EXTENSIONS,
    bare_episode_number,
    normalize_bare_episode_filename,
)
from ..resolver import ResolutionError
from ..staging import is_ready_path, ready_save_path


class EpisodeNormalizer:
    """Own the qBittorrent-to-MoviePilot completion handoff."""

    def __init__(self, qbit, resolver, staging_path: str, ready_path: str, retry_seconds: int = 300):
        self.qbit = qbit
        self.resolver = resolver
        self.staging_path = staging_path
        self.ready_path = ready_path
        self.retry_seconds = retry_seconds
        self.identity_cache = {}
        self.last_attempt = {}

    @staticmethod
    def _release_title_probe(value):
        text = Path(str(value or "")).name
        text = re.sub(r"(?i)\s*\{tmdb-\d+\}.*$", "", text)
        text = re.sub(
            r"(?i)[ ._-]+(?:8k|4k|2160p|1440p|1080p|720p|576p|480p|web[- .]?dl|webrip|bluray|bdrip|hdtv|dvdrip)(?:[ ._-].*)?$",
            "",
            text,
        )
        return text.strip(" ._-")

    def _completed_task_root(self, item):
        roots = {
            Path(self.ready_path).parent,
            Path(self.staging_path).parent,
        }
        raw = item.get("content_path") or item.get("save_path") or self.ready_path
        root = Path(str(raw))
        if root.is_file():
            root = root.parent
        if not any(root == base or base in root.parents for base in roots):
            return None
        if any(root == base for base in roots):
            name = str(item.get("name") or "").strip()
            candidate = root / name if name else None
            if not candidate or not candidate.is_dir():
                return None
            root = candidate
        return root if root.is_dir() else None

    def _confirmed_tv_identity(self, item, root):
        key = f"{item.get('hash') or ''}|{root}"
        cached = self.identity_cache.get(key)
        if cached:
            return cached
        now = time.monotonic()
        last_attempt = self.last_attempt.get(key)
        if last_attempt is not None and now - last_attempt < self.retry_seconds:
            return None
        self.last_attempt[key] = now
        for probe in (root.name, item.get("name")):
            title = self._release_title_probe(probe)
            if not title:
                continue
            try:
                identity = self.resolver.automatic(title)
            except (ResolutionError, ValueError) as exc:
                print(f"episode-normalizer resolve skipped {title}: {exc}", flush=True)
                continue
            if identity.media_type != "电视剧":
                print(f"episode-normalizer skipped non-TV task: {title}", flush=True)
                continue
            self.identity_cache[key] = identity
            return identity
        print(f"episode-normalizer skipped ambiguous task: {root}", flush=True)
        return None

    def normalize_completed_episode_files(self, tasks):
        renamed = []
        for item in tasks or []:
            if is_brush_task(item):
                continue
            root = self._completed_task_root(item)
            if not root:
                continue
            task_hash = str(item.get("hash") or "")
            if not task_hash:
                continue
            try:
                files = self.qbit.files(task_hash)
            except Exception as exc:
                print(f"episode-normalizer files {task_hash[:12]} failed: {exc}", flush=True)
                continue
            candidates = []
            for file_item in files:
                if float(file_item.get("progress") or 0) < 1:
                    continue
                old_path = str(file_item.get("name") or "")
                old_name = PurePosixPath(old_path).name
                if bare_episode_number(old_name) is not None:
                    candidates.append((old_path, old_name))
            if not candidates:
                continue
            identity = self._confirmed_tv_identity(item, root)
            if not identity:
                continue
            existing_paths = {
                str(file_item.get("name") or "")
                for file_item in files
                if file_item.get("name")
            }
            for old_path, old_name in candidates:
                target_name = normalize_bare_episode_filename(
                    old_name,
                    identity.title,
                    identity.season or 1,
                )
                if not target_name:
                    continue
                target_path = str(PurePosixPath(old_path).with_name(Path(target_name).name))
                if target_path in existing_paths:
                    print(f"episode-normalizer conflict, keeping {old_path}", flush=True)
                    continue
                try:
                    self.qbit.rename_file(task_hash, old_path, target_path)
                except Exception as exc:
                    print(f"episode-normalizer rename failed {old_path}: {exc}", flush=True)
                    continue
                renamed.append((old_path, target_path))
                existing_paths.add(target_path)
                print(f"episode-normalizer renamed {old_name} -> {Path(target_path).name}", flush=True)
        return renamed

    @staticmethod
    def _task_save_path(item):
        save_path = str(item.get("save_path") or "").strip()
        if save_path:
            return save_path
        content_path = str(item.get("content_path") or "").strip()
        return str(Path(content_path).parent) if content_path else ""

    def _task_info(self, torrent_hash):
        query = urllib.parse.urlencode({"hash": torrent_hash})
        tasks = self.qbit.request(f"/api/v2/torrents/info?{query}").json()
        return tasks[0] if isinstance(tasks, list) and tasks else None

    def _completed_bare_episode_files(self, item):
        files = self.qbit.files(str(item.get("hash") or ""))
        return [
            str(file_item.get("name") or "")
            for file_item in files
            if float(file_item.get("progress") or 0) >= 1
            and str(file_item.get("name") or "").lower().endswith(VIDEO_EXTENSIONS)
            and bare_episode_number(PurePosixPath(str(file_item.get("name") or "")).name) is not None
        ]

    def _promote_completed_task(self, item):
        task_hash = str(item.get("hash") or "")
        if not task_hash or is_brush_task(item):
            return False
        current = self._task_save_path(item)
        if is_ready_path(current, self.ready_path):
            return True
        target = ready_save_path(current, self.staging_path, self.ready_path)
        if not target:
            print(f"episode-staging skipped unknown save path: {current}", flush=True)
            return False
        try:
            self.qbit.set_location(task_hash, target)
        except Exception as exc:
            print(f"episode-staging move failed {task_hash[:12]}: {exc}", flush=True)
            return False
        for _ in range(60):
            try:
                refreshed = self._task_info(task_hash)
            except Exception:
                refreshed = None
            if refreshed and posixpath.normpath(self._task_save_path(refreshed)) == posixpath.normpath(target):
                print(f"episode-staging promoted {task_hash[:12]} -> {target}", flush=True)
                return True
            time.sleep(0.5)
        print(f"episode-staging move timeout {task_hash[:12]}", flush=True)
        return False

    def prepare_completed_tasks(self, tasks):
        ready = []
        for item in tasks or []:
            if float(item.get("progress") or 0) < 1 or is_brush_task(item):
                continue
            try:
                bare_files = self._completed_bare_episode_files(item)
                if bare_files:
                    print(
                        "episode-staging retained bare files: "
                        + " | ".join(Path(name).name for name in bare_files),
                        flush=True,
                    )
                    continue
            except Exception as exc:
                print(f"episode-staging files check failed: {exc}", flush=True)
                continue
            if self._promote_completed_task(item):
                ready.append(item)
        return ready
