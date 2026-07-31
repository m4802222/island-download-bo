"""Safe qBittorrent cleanup decisions.

The functions in this module are deterministic and deliberately require an
exact MoviePilot source-path match before a completed torrent can be removed.
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Iterable

from .media import VIDEO_EXTENSIONS


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

    save_path = normalize_path(task.get("save_path"))
    if not save_path:
        return set()
    paths: set[str] = set()
    for item in files:
        name = str(item.get("name") or "")
        if int(item.get("priority") or 0) <= 0:
            continue
        if not name.lower().endswith(VIDEO_EXTENSIONS):
            continue
        paths.add(normalize_path(posixpath.join(save_path, name)))
    return paths


def safe_to_cleanup(
    task: dict,
    files: Iterable[dict],
    successful_sources: set[str],
) -> bool:
    """Require completion, an idle state, and exact MoviePilot confirmation."""

    if float(task.get("progress") or 0) < 1:
        return False
    if task.get("state") not in {"stoppedUP", "missingFiles"}:
        return False
    videos = selected_video_paths(task, files)
    return bool(videos) and videos.issubset(successful_sources)
