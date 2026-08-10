"""Map qBittorrent staging paths to MoviePilot-ready paths."""

from __future__ import annotations

import posixpath


def _clean(value: object) -> str:
    text = str(value or "").strip()
    return posixpath.normpath(text) if text else ""


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def ready_save_path(save_path: object, staging_inbox: str, ready_inbox: str) -> str:
    """Translate a qB save directory from staging into the ready tree."""

    current = _clean(save_path)
    staging_root = _clean(posixpath.dirname(staging_inbox.rstrip("/")))
    ready_root = _clean(posixpath.dirname(ready_inbox.rstrip("/")))
    if not current or not staging_root or not ready_root or not _under(current, staging_root):
        return ""
    relative = posixpath.relpath(current, staging_root)
    if relative in {".", ".."} or relative.startswith("../"):
        return ""
    return posixpath.join(ready_root, relative)


def is_ready_path(save_path: object, ready_inbox: str) -> bool:
    """Return whether a qB save directory is already MoviePilot-visible."""

    current = _clean(save_path)
    ready_root = _clean(posixpath.dirname(ready_inbox.rstrip("/")))
    return bool(current and ready_root and _under(current, ready_root))
