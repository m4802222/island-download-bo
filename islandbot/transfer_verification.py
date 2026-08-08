"""Proofs that a MoviePilot transfer is present and complete on cloud storage."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .cleanup import normalize_path


@dataclass(frozen=True)
class TransferProof:
    history_id: int
    source: str
    destination: str
    source_size: int
    destination_size: int


def _object(value: object) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _paths(value: object) -> set[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    if not isinstance(parsed, list):
        return set()
    return {
        path
        for item in parsed
        if (path := normalize_path(item))
    }


def successful_transfer_proofs(
    rows: Iterable[tuple[object, object, object, object, object, object]],
    candidates: set[str] | None = None,
) -> dict[str, TransferProof]:
    """Parse latest-first successful MoviePilot rows into source/destination proofs."""

    wanted = {normalize_path(item) for item in candidates or set()}
    proofs: dict[str, TransferProof] = {}
    for history_id, source, destination, files, source_item, destination_item in rows:
        source_path = normalize_path(source)
        destination_path = normalize_path(destination)
        if not source_path or not destination_path or source_path in proofs:
            continue
        if wanted and source_path not in wanted:
            continue
        if source_path not in _paths(files):
            continue
        source_data = _object(source_item)
        destination_data = _object(destination_item)
        if normalize_path(source_data.get("path")) != source_path:
            continue
        if normalize_path(destination_data.get("path")) != destination_path:
            continue
        try:
            source_size = int(source_data.get("size") or 0)
            destination_size = int(destination_data.get("size") or 0)
        except (TypeError, ValueError):
            continue
        proofs[source_path] = TransferProof(
            history_id=int(history_id),
            source=source_path,
            destination=destination_path,
            source_size=source_size,
            destination_size=destination_size,
        )
    return proofs


def load_successful_transfer_proofs(
    database: Path,
    candidates: set[str],
) -> dict[str, TransferProof]:
    """Read latest successful MoviePilot proof rows for selected sources."""

    if not database.is_file():
        raise RuntimeError("MoviePilot 整理历史不可读取")
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT id, src, dest, files, src_fileitem, dest_fileitem "
                "FROM transferhistory "
                "WHERE status = 1 AND dest IS NOT NULL AND dest != '' "
                "ORDER BY id DESC"
            ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"MoviePilot 整理历史读取失败：{exc}") from exc
    return successful_transfer_proofs(rows, candidates)


def verified_transfer_sources(
    proofs: dict[str, TransferProof],
    remote_size: Callable[[str], int | None],
    expected_sizes: dict[str, int] | None = None,
) -> tuple[set[str], dict[str, str]]:
    """Require equal historical sizes and a matching live cloud object."""

    expected = expected_sizes or {}
    verified: set[str] = set()
    rejected: dict[str, str] = {}
    for source, proof in proofs.items():
        if proof.source_size <= 0 or proof.destination_size <= 0:
            rejected[source] = "历史记录缺少有效大小"
            continue
        if proof.source_size != proof.destination_size:
            rejected[source] = "历史源文件与目标文件大小不一致"
            continue
        expected_size = int(expected.get(source) or 0)
        if expected_sizes is not None and expected_size != proof.source_size:
            rejected[source] = "qBittorrent 文件大小与整理记录不一致"
            continue
        live_size = remote_size(proof.destination)
        if live_size is None:
            rejected[source] = "云盘目标不存在或暂时不可读取"
            continue
        if live_size != proof.destination_size:
            rejected[source] = "云盘实时大小与整理记录不一致"
            continue
        verified.add(source)
    return verified, rejected
