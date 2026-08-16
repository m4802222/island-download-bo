"""Construction and persistence helpers for mutable runtime state."""

from __future__ import annotations

import time
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from .storage import IdentityStore, JsonStore


def migrate_seen(raw: Any, now: Callable[[], float] = time.time) -> dict[str, float]:
    """Normalize historical completion state into ``hash -> timestamp`` form."""
    if isinstance(raw, list):
        return {item: now() for item in raw if isinstance(item, str)}
    if isinstance(raw, dict):
        return {
            item: float(timestamp)
            for item, timestamp in raw.items()
            if isinstance(item, str) and isinstance(timestamp, (int, float))
        }
    return {}


class RuntimeState:
    """Own the stores and in-memory values used by the runtime coordinator."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.done_store = JsonStore(self.data_dir / "done.json", {})
        self.seen = migrate_seen(self.done_store.load())

        self.queue_file = self.data_dir / "queue.json"
        self.queue_store = JsonStore(self.queue_file, [])
        self.queue = list(self.queue_store.load())
        self.queue_ready = self.queue_file.exists()

        self.blocked_store = JsonStore(self.data_dir / "blocked.json", [])
        self.blocked = set(self.blocked_store.load())

        self.expiry_store = JsonStore(self.data_dir / "expiry.json", [])
        self.expiring = list(self.expiry_store.load())

        self.incoming_dir = self.data_dir / "incoming"
        self.incoming_dir.mkdir(parents=True, exist_ok=True)

        self.quark_queue_store = JsonStore(self.data_dir / "quark_queue.json", [])
        self.quark_queue = list(self.quark_queue_store.load())
        self.quark_pending_store = JsonStore(self.data_dir / "quark_pending.json", {})
        self.quark_pending = self.quark_pending_store.load()
        self.quark_title_pending_store = JsonStore(
            self.data_dir / "quark_title_pending.json", {}
        )
        self.quark_title_pending = self.quark_title_pending_store.load()
        self.quark_confirm_pending_store = JsonStore(
            self.data_dir / "quark_confirm_pending.json", {}
        )
        self.quark_confirm_pending = self.quark_confirm_pending_store.load()

        self.aria2_store = JsonStore(self.data_dir / "aria2.json", {})
        self.aria2_tracked = self.aria2_store.load()
        self.account_pending_store = JsonStore(
            self.data_dir / "account_pending.json", {}
        )
        self.account_pending = self.account_pending_store.load()

        self.hermes_inbox_file = self.data_dir / "hermes_inbox.json"
        self.identities = IdentityStore(self.data_dir / "identities-v2.json")

        self.queue_lock = Lock()
        self.expiring_lock = Lock()
        self.quark_lock = Lock()
        self.hermes_inbox_lock = Lock()

    def save_quark_queue(self) -> None:
        self.quark_queue_store.save(self.quark_queue)

    def save_quark_pending(self) -> None:
        self.quark_pending_store.save(self.quark_pending)

    def save_quark_title_pending(self) -> None:
        self.quark_title_pending_store.save(self.quark_title_pending)

    def save_quark_confirm_pending(self) -> None:
        self.quark_confirm_pending_store.save(self.quark_confirm_pending)

    def save_aria2_tracked(self) -> None:
        self.aria2_store.save(self.aria2_tracked)

    def save_account_pending(self) -> None:
        self.account_pending_store.save(self.account_pending)
