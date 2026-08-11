"""Telegram presentation primitives with no download-domain decisions."""

from __future__ import annotations

import json
import time
from typing import Callable


class TelegramUI:
    """Own message delivery, short-lived notices, and common keyboards."""

    def __init__(
        self,
        telegram_call: Callable[[str, dict], dict],
        expiring: list[dict],
        expiring_lock,
        expiry_store,
        categories: Callable[[], list[str]],
        max_active_downloads: Callable[[], int],
    ):
        self.telegram_call = telegram_call
        self.expiring = expiring
        self.expiring_lock = expiring_lock
        self.expiry_store = expiry_store
        self.categories = categories
        self.max_active_downloads = max_active_downloads

    def send(self, chat_id, text, keyboard=None):
        # Hermes jobs use a local inbox and must not create a second Telegram
        # conversation from this bot.
        if not chat_id:
            return {"result": {}}
        payload = {"chat_id": chat_id, "text": text}
        if keyboard:
            payload["reply_markup"] = json.dumps(
                {"inline_keyboard": keyboard}, ensure_ascii=False
            )
        return self.telegram_call("sendMessage", payload)

    def send_temporary(self, chat_id, text, lifetime_seconds=300):
        if not chat_id:
            return
        response = self.send(chat_id, text)
        message_id = response.get("result", {}).get("message_id")
        if message_id:
            with self.expiring_lock:
                self.expiring.append(
                    {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "delete_at": time.time() + lifetime_seconds,
                    }
                )
                self.expiry_store.save(self.expiring)

    def delete_expired_messages(self):
        now = time.time()
        with self.expiring_lock:
            snapshot = list(self.expiring)
        remaining = []
        for item in snapshot:
            if item["delete_at"] > now:
                remaining.append(item)
                continue
            try:
                self.telegram_call(
                    "deleteMessage",
                    {"chat_id": item["chat_id"], "message_id": item["message_id"]},
                )
            except Exception as exc:
                print("delete-expired-message error:", exc, flush=True)
        if len(remaining) != len(snapshot):
            with self.expiring_lock:
                self.expiring[:] = remaining
                self.expiry_store.save(self.expiring)

    def answer(self, callback_id, text=None):
        data = {"callback_query_id": callback_id}
        if text:
            data["text"] = text
        try:
            self.telegram_call("answerCallbackQuery", data)
        except RuntimeError as exc:
            message = str(exc).lower()
            expired = (
                "query is too old" in message
                or "response timeout expired" in message
                or "query id is invalid" in message
            )
            if not expired:
                raise
            print("telegram callback expired; continuing", flush=True)

    def home_keyboard(self):
        return [
            [
                {"text": "👤 开号", "callback_data": "account:create"},
                {"text": "📋 我的任务", "callback_data": "home:tasks"},
            ],
            [
                {"text": "🖥 状态与设置", "callback_data": "home:server"},
                {"text": "☁️ 云盘控制", "callback_data": "drive:open"},
            ],
        ]

    def category_keyboard(self):
        categories = list(self.categories())
        rows = []
        for index in range(0, len(categories), 3):
            rows.append(
                [
                    {"text": item, "callback_data": f"category:{item}"}
                    for item in categories[index : index + 3]
                ]
            )
        rows.append([{"text": "🤖 智能分类", "callback_data": "category:__auto__"}])
        rows.append([{"text": "取消", "callback_data": "home:home"}])
        return rows

    def home(self, chat_id, first_name=""):
        greeting = f"你好，{first_name}。" if first_name else "你好。"
        self.send(
            chat_id,
            f"{greeting}\n\nIsland Download\n发送 magnet、.torrent 或夸克分享链接。\n"
            f"系统会智能分类，最多同时下载 {self.max_active_downloads()} 个任务。\n"
            "也可以直接发送中文问题给 AI 助手。",
            self.home_keyboard(),
        )
