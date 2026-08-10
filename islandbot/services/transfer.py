"""MoviePilot history and verified-transfer access."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..media import parse_identity_label
from ..transfer_verification import load_successful_transfer_proofs


class TransferService:
    """Read transfer history without coupling it to Telegram or qB policy."""

    def __init__(self, database: Path):
        self.database = database

    def moviepilot_existing(self, media_title: str):
        if not self.database.is_file():
            raise RuntimeError("MoviePilot 整理历史不可读取，已停止提交下载以防重复")
        identity = parse_identity_label(media_title)
        if not identity:
            raise RuntimeError("媒体身份格式异常，未检查 MoviePilot 历史")
        title = identity.title
        try:
            with sqlite3.connect(f"file:{self.database}?mode=ro", uri=True) as connection:
                rows = connection.execute(
                    "SELECT title, episodes, dest, files FROM transferhistory "
                    "WHERE status = 1 AND (title LIKE ? OR dest LIKE ?)",
                    (f"%{title}%", f"%{title}%"),
                ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError(f"MoviePilot 整理历史读取失败，未提交下载：{exc}") from exc
        paths = []
        for row in rows:
            for value in row:
                if value:
                    paths.append(str(value))
        return paths, bool(rows)

    def successful_proofs(self, candidates):
        return load_successful_transfer_proofs(self.database, set(candidates))
