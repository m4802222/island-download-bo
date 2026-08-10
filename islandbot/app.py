"""Small compatibility/startup entry point for Island Download Bot.

The application runtime lives in :mod:`islandbot.runtime`.  This module keeps
the historical import path stable for scripts and tests while ``main`` remains
the only startup responsibility here.
"""

from . import runtime as _runtime
import os

# Preserve the small set of mutable names used by existing integrations.  The
# wrappers below copy test/runtime substitutions into the real runtime before
# invoking it; read-only names are provided by ``__getattr__``.
QBIT_CLIENT = _runtime.QBIT_CLIENT
RESOLVER = _runtime.RESOLVER
NORMALIZE_IDENTITY_CACHE = _runtime.NORMALIZE_IDENTITY_CACHE
NORMALIZE_LAST_ATTEMPT = _runtime.NORMALIZE_LAST_ATTEMPT
telegram = _runtime.telegram


def _sync_compatibility_state():
    _runtime.QBIT_CLIENT = QBIT_CLIENT
    _runtime.RESOLVER = RESOLVER
    _runtime.NORMALIZER.qbit = QBIT_CLIENT
    _runtime.NORMALIZER.resolver = RESOLVER
    _runtime.telegram = telegram


def normalize_completed_episode_files(tasks):
    _sync_compatibility_state()
    return _runtime.normalize_completed_episode_files(tasks)


def answer(callback_id, text=None):
    _sync_compatibility_state()
    return _runtime.answer(callback_id, text)


def main():
    """Start the long-polling runtime."""
    if os.environ.get("TELEGRAM_UI_ENGINE", "legacy").strip().lower() == "aiogram_dialog":
        from .aiogram_ui import run

        return run()
    print(f"Island Download Bot {_runtime.__version__} started", flush=True)
    while not _runtime.LAST_CATEGORY_SYNC:
        try:
            _runtime.synchronize_media_categories()
        except Exception as exc:
            print(f"media-category-sync error: {exc}", flush=True)
            _runtime.time.sleep(10)
    while True:
        try:
            updates = _runtime.telegram(
                "getUpdates",
                {
                    "offset": _runtime.OFFSET,
                    "timeout": 25,
                    "allowed_updates": _runtime.json.dumps(["message", "callback_query"]),
                },
            )
            for update in updates.get("result", []):
                _runtime.handle(update)
            _runtime.service_tick()
        except Exception as exc:
            print("error:", exc, flush=True)
            _runtime.time.sleep(5)


def __getattr__(name):
    return getattr(_runtime, name)


__all__ = ["answer", "main", "normalize_completed_episode_files"]
