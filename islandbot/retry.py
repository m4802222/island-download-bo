"""Conservative recovery rules for MoviePilot rclone upload failures."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


HEALTHY = "healthy"
QUOTA = "quota"
AUTH = "auth"
NETWORK = "network"
UNKNOWN = "unknown"

RETRY_DELAYS = {
    HEALTHY: (5 * 60, 15 * 60, 30 * 60, 60 * 60, 2 * 60 * 60, 6 * 60 * 60),
    NETWORK: (5 * 60, 15 * 60, 30 * 60, 60 * 60, 2 * 60 * 60, 6 * 60 * 60),
    QUOTA: (30 * 60, 60 * 60, 2 * 60 * 60, 4 * 60 * 60, 6 * 60 * 60),
    AUTH: (6 * 60 * 60,),
    UNKNOWN: (30 * 60, 60 * 60, 2 * 60 * 60, 6 * 60 * 60),
}


@dataclass(frozen=True)
class TransferFailure:
    history_id: int
    source: str
    title: str
    error: str
    date: str
    download_hash: str


def _is_rclone_upload_error(error: object) -> bool:
    text = str(error or "").casefold()
    return "rclone" in text and ("上传" in text or "upload" in text)


def pending_rclone_failures(
    database: Path,
    source_exists: Callable[[str], bool] | None = None,
    success_verified: Callable[[str], bool] | None = None,
) -> dict[str, TransferFailure]:
    """Return the latest retryable upload failure for each existing source."""

    if not database.is_file():
        raise RuntimeError("MoviePilot 整理历史不可读取")
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT failed.id, failed.src, failed.title, failed.errmsg, "
                "failed.date, failed.download_hash, EXISTS("
                "SELECT 1 FROM transferhistory AS ok "
                "WHERE ok.status = 1 AND ok.src = failed.src "
                "AND ok.id > failed.id"
                ") AS has_success "
                "FROM transferhistory AS failed "
                "WHERE failed.status = 0 AND failed.src IS NOT NULL "
                "AND failed.src != '' "
                "AND failed.id = ("
                "SELECT MAX(latest.id) FROM transferhistory AS latest "
                "WHERE latest.status = 0 AND latest.src = failed.src"
                ") ORDER BY failed.id",
            ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"MoviePilot 整理历史读取失败：{exc}") from exc

    exists = source_exists or (lambda source: Path(source).is_file())
    failures: dict[str, TransferFailure] = {}
    for history_id, source, title, error, date, download_hash, has_success in rows:
        source = str(source or "")
        if not _is_rclone_upload_error(error) or not exists(source):
            continue
        if has_success and (
            success_verified is None or success_verified(source)
        ):
            continue
        failures[source] = TransferFailure(
            history_id=int(history_id),
            source=source,
            title=str(title or Path(source).name),
            error=str(error or ""),
            date=str(date or ""),
            download_hash=str(download_hash or ""),
        )
    return failures


def classify_probe_error(output: str, returncode: int) -> str:
    """Classify a small remote write probe without exposing its raw output."""

    if returncode == 0:
        return HEALTHY
    text = str(output or "").casefold()
    if any(
        marker in text
        for marker in (
            "invalid_grant",
            "token expired",
            "couldn't fetch token",
            "oauth",
            "unauthorized",
            "invalid credentials",
        )
    ):
        return AUTH
    if any(
        marker in text
        for marker in (
            "userratelimitexceeded",
            "user rate limit exceeded",
            "rate limit exceeded",
            "storagequotaexceeded",
            "dailylimitexceeded",
            "downloadquotaexceeded",
            "quota exceeded",
        )
    ):
        return QUOTA
    if any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "connection reset",
            "connection refused",
            "temporary failure",
            "no such host",
            "tls handshake",
            "network is unreachable",
        )
    ):
        return NETWORK
    return UNKNOWN


def retry_delay(attempts: int, backend_status: str) -> int:
    delays = RETRY_DELAYS.get(backend_status, RETRY_DELAYS[UNKNOWN])
    return delays[min(max(0, attempts), len(delays) - 1)]


def update_retry_state(
    failures: dict[str, TransferFailure],
    state: dict,
    now: float,
    backend_status: str,
    allow_retry: bool,
    retryable_sources: set[str] | None = None,
) -> tuple[list[TransferFailure], dict, list[TransferFailure]]:
    """Register failures, retain backoff, and return due and newly seen items."""

    previous_items = state.get("items") if isinstance(state, dict) else {}
    if not isinstance(previous_items, dict):
        previous_items = {}
    items: dict[str, dict] = {}
    due: list[TransferFailure] = []
    new: list[TransferFailure] = []

    for source, failure in failures.items():
        item = dict(previous_items.get(source) or {})
        if not item:
            new.append(failure)
            item = {
                "attempts": 0,
                "first_seen": now,
                "next_retry": now + retry_delay(0, backend_status),
            }
        attempts = max(0, int(item.get("attempts") or 0))
        item.update(asdict(failure))
        retryable = retryable_sources is None or source in retryable_sources
        if allow_retry and retryable and now >= float(item.get("next_retry") or 0):
            due.append(failure)
            attempts += 1
            item["attempts"] = attempts
            item["next_retry"] = now + retry_delay(attempts, backend_status)
        items[source] = item

    updated = dict(state) if isinstance(state, dict) else {}
    updated["items"] = items
    return due, updated, new


def cloud_block_status(path: Path) -> dict:
    """Read the shared upload block file used by the bot download queue."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
