"""Narrow rclone OAuth token updates that preserve every other remote."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


ALLOWED_OAUTH_REMOTES = frozenset({"gdrive1", "gdrive2"})
_SECTION = re.compile(r"^[ \t]*\[([^\]\r\n]+)\][ \t]*(?:[;#].*)?(?:\r?\n)?$")
_TOKEN = re.compile(r"^[ \t]*token[ \t]*=[ \t]*(.*?)[ \t]*(?:\r?\n)?$")


def _validate_remote(remote: str) -> None:
    if remote not in ALLOWED_OAUTH_REMOTES:
        raise RuntimeError("只允许更新 gdrive1 或 gdrive2，拒绝修改其他远程")


def _section_bounds(lines: list[str], remote: str) -> tuple[int, int]:
    matches: list[int] = []
    headers: list[int] = []
    for index, line in enumerate(lines):
        match = _SECTION.match(line)
        if not match:
            continue
        headers.append(index)
        if match.group(1).strip() == remote:
            matches.append(index)
    if len(matches) != 1:
        raise RuntimeError(f"rclone 配置中的 [{remote}] 数量不是 1")
    start = matches[0] + 1
    end = next((index for index in headers if index >= start), len(lines))
    return start, end


def _token(lines: list[str], remote: str) -> tuple[str, int | None]:
    start, end = _section_bounds(lines, remote)
    matches: list[tuple[int, str]] = []
    for index in range(start, end):
        match = _TOKEN.match(lines[index])
        if match:
            matches.append((index, match.group(1).strip()))
    if len(matches) > 1:
        raise RuntimeError(f"rclone 配置中的 [{remote}] 存在重复 token")
    if not matches:
        return "", None
    return matches[0][1], matches[0][0]


def replace_oauth_token(source: str, target: str, remote: str) -> str:
    """Replace only one allowed remote's token line, preserving all other bytes."""

    _validate_remote(remote)
    source_lines = source.splitlines(keepends=True)
    target_lines = target.splitlines(keepends=True)
    token, _ = _token(source_lines, remote)
    if not token:
        raise RuntimeError(f"主机 rclone 配置 [{remote}] 没有有效 token")
    _, target_index = _token(target_lines, remote)
    newline = "\r\n" if "\r\n" in target else "\n"
    replacement = f"token = {token}{newline}"
    if target_index is not None:
        target_lines[target_index] = replacement
    else:
        _, end = _section_bounds(target_lines, remote)
        if end and not target_lines[end - 1].endswith(("\n", "\r")):
            target_lines[end - 1] += newline
        target_lines.insert(end, replacement)
    return "".join(target_lines)


def sync_oauth_token(source_path: Path, target_path: Path, remote: str) -> bool:
    """Atomically update one token and preserve the target file's ownership/mode."""

    source = source_path.read_text(encoding="utf-8")
    target = target_path.read_text(encoding="utf-8")
    updated = replace_oauth_token(source, target, remote)
    if updated == target:
        return False

    metadata = target_path.stat()
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        dir=target_path.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, metadata.st_mode)
        os.chown(temporary, metadata.st_uid, metadata.st_gid)
        os.replace(temporary, target_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True
