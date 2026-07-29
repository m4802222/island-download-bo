"""Atomic JSON persistence and identity override storage."""

from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any, Generic, TypeVar

from .media import MediaIdentity, alias_key


T = TypeVar("T")


class JsonStore(Generic[T]):
    def __init__(self, path: Path, default: T):
        self.path = path
        self.default = default
        self.lock = RLock()

    def load(self) -> T:
        with self.lock:
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return self.default

    def save(self, value: T) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)


class IdentityStore:
    """User-approved aliases; automatic guesses never overwrite manual data."""

    def __init__(self, path: Path):
        self.store = JsonStore(path, {"version": 2, "aliases": {}})
        self.data: dict[str, Any] = self.store.load()
        if self.data.get("version") != 2:
            self.data = {"version": 2, "aliases": {}}

    def get(self, source_title: str) -> MediaIdentity | None:
        raw = self.data["aliases"].get(alias_key(source_title))
        if not isinstance(raw, dict):
            return None
        try:
            return MediaIdentity.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            return None

    def remember(self, source_title: str, identity: MediaIdentity) -> None:
        self.data["aliases"][alias_key(source_title)] = identity.to_dict()
        self.store.save(self.data)

    def forget(self, source_title: str) -> None:
        self.data["aliases"].pop(alias_key(source_title), None)
        self.store.save(self.data)
