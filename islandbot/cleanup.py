"""Safe qBittorrent cleanup decisions.

The functions in this module are deterministic and deliberately require an
exact MoviePilot source-path match before a completed torrent can be removed.
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Iterable

from .media import VIDEO_EXTENSIONS


PROTECTED_BRUSH_CATEGORIES = frozenset({"学校救号"})


def is_brush_task(task: dict) -> bool:
    """Keep qBittorrent traffic-boosting tasks even after media transfer."""

    category = str(task.get("category") or "").strip()
    tags = {
        tag.strip()
        for tag in str(task.get("tags") or "").split(",")
        if tag.strip()
    }
    return (
        category.startswith("刷流")
        or category in PROTECTED_BRUSH_CATEGORIES
        or any(tag.startswith("刷流") for tag in tags)
    )


def normalize_path(value: object) -> str:
    """Normalize a container path without resolving it on the host."""

    text = str(value or "").strip()
    if not text:
        return ""
    normalized = posixpath.normpath(text)
    if normalized.startswith("//"):
        normalized = "/" + normalized.lstrip("/")
    return normalized


def successful_source_paths(rows: Iterable[tuple[object, object]]) -> set[str]:
    """Extract source files from successful MoviePilot history rows."""

    sources: set[str] = set()
    for files_value, destination in rows:
        if not destination:
            continue
        try:
            files = json.loads(str(files_value or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(files, list):
            continue
        sources.update(
            path
            for item in files
            if (path := normalize_path(item))
        )
    return sources


def selected_video_paths(task: dict, files: Iterable[dict]) -> set[str]:
    """Return selected video paths belonging to one qBittorrent task."""

    return set(selected_video_sizes(task, files))


def selected_video_sizes(task: dict, files: Iterable[dict]) -> dict[str, int]:
    """Return selected qBittorrent video paths and their expected byte sizes."""

    save_path = normalize_path(task.get("save_path"))
    if not save_path:
        return {}
    paths: dict[str, int] = {}
    for item in files:
        name = str(item.get("name") or "")
        if int(item.get("priority") or 0) <= 0:
            continue
        if not name.lower().endswith(VIDEO_EXTENSIONS):
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        paths[normalize_path(posixpath.join(save_path, name))] = size
    return paths


def safe_to_cleanup(
    task: dict,
    files: Iterable[dict],
    successful_sources: set[str],
) -> bool:
    """Require completion, an idle state, and exact MoviePilot confirmation."""

    if is_brush_task(task):
        return False
    if float(task.get("progress") or 0) < 1:
        return False
    if task.get("state") not in {
        "stoppedUP",
        "pausedUP",
        "queuedUP",
        "stalledUP",
        "uploading",
        "forcedUP",
        "missingFiles",
    }:
        return False
    videos = selected_video_paths(task, files)
    return bool(videos) and videos.issubset(successful_sources)
