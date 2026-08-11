"""Telegram update handlers wired to the application runtime."""

from __future__ import annotations

import threading


class BotHandlers:
    """Keep Telegram routing separate while preserving existing app seams."""

    def __init__(self, runtime):
        self.runtime = runtime

    def legacy_command(self, chat_id, text):
        r = self.runtime()
        if text == "/tasks":
            return r.show_tasks(chat_id)
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or parts[0] not in {"/pause", "/resume", "/delete"}:
            return False
        item = r.find_task(parts[1].strip())
        if not item:
            r.send(chat_id, "没有找到唯一任务，请在“我的任务”中选择。")
            return True
        command = parts[0][1:]
        r.qbit_action(command, item["hash"], delete_files=command == "delete")
        if command in {"pause", "delete"} and item["hash"] in r.QUEUE:
            r.QUEUE.remove(item["hash"])
            r.save_queue()
        elif command == "resume" and item.get("progress", 0) < 1 and item["hash"] not in r.QUEUE:
            r.QUEUE.append(item["hash"])
            r.save_queue()
        r.run_queue()
        r.send(
            chat_id,
            f"已{ '删除' if command == 'delete' else ('暂停' if command == 'pause' else '继续') }："
            f"{item.get('name', '')[:45]}",
        )
        return True

    def handle_callback(self, callback):
        r = self.runtime()
        user_id = callback["from"]["id"]
        chat_id = callback["message"]["chat"]["id"]
        r.answer(callback["id"])
        if user_id != r.OWNER:
            return
        try:
            r.telegram(
                "deleteMessage",
                {
                    "chat_id": chat_id,
                    "message_id": callback["message"]["message_id"],
                },
            )
        except Exception:
            pass
        data = callback.get("data", "")
        if data == "home:home":
            return r.home(chat_id, callback["from"].get("first_name", ""))
        if data == "account:create":
            r.ACCOUNT_PENDING[str(chat_id)] = {"created_at": r.time.time()}
            r.save_account_pending()
            return r.send(
                chat_id,
                "请输入新账号的用户名。\n\n密码固定为 123456，仅有普通观看权限。\n输入 /cancel 可取消。",
                [[{"text": "取消", "callback_data": "account:cancel"}]],
            )
        if data == "account:cancel":
            r.ACCOUNT_PENDING.pop(str(chat_id), None)
            r.save_account_pending()
            return r.home(chat_id, callback["from"].get("first_name", ""))
        if data == "home:tasks":
            return r.show_tasks(chat_id)
        if data == "home:completed":
            return r.show_recent_completed(chat_id)
        if data == "home:server":
            return r.server_status(chat_id)
        if data == "drive:open":
            return r.cloud_drive_screen(chat_id)
        if data == "drive:check":
            return r.cloud_drive_probe(chat_id)
        if data.startswith("drive:switchask:"):
            return r.cloud_drive_confirm(chat_id, data.rsplit(":", 1)[1])
        if data.startswith("drive:switch:"):
            return r.cloud_drive_switch(chat_id, data.rsplit(":", 1)[1])
        if data == "quarkusecandidate":
            pending = r.QUARK_TITLE_PENDING.get(str(chat_id))
            if not pending or not pending.get("candidate"):
                return r.send(chat_id, "这个名称确认已过期，请重新发送分享链接。", r.home_keyboard())
            try:
                title = r.moviepilot_media_title(pending["candidate"])
            except RuntimeError as exc:
                return r.send(chat_id, str(exc))
            r.QUARK_TITLE_PENDING.pop(str(chat_id), None)
            r.save_quark_title_pending()
            return r.confirm_quark_download(chat_id, pending["url"], title)
        if data == "quarkcancelcandidate":
            r.QUARK_TITLE_PENDING.pop(str(chat_id), None)
            r.save_quark_title_pending()
            return r.send(chat_id, "已取消这次夸克下载。", r.home_keyboard())
        if data.startswith("quarkconfirm:"):
            key = data.split(":", 1)[1]
            pending = r.QUARK_CONFIRM_PENDING.get(key)
            if not pending:
                return r.send(chat_id, "这个确认已过期，请重新发送分享链接。", r.home_keyboard())
            if not r.safe_confirmed_media_title(pending["media_title"]):
                r.QUARK_CONFIRM_PENDING.pop(key, None)
                r.save_quark_confirm_pending()
                return r.request_quark_title(
                    chat_id,
                    pending["url"],
                    pending["media_title"],
                    "旧识别结果无效，已禁止下载",
                )
            try:
                result = r.enqueue_quark_task(chat_id, pending["url"], pending["media_title"])
            except Exception as exc:
                print("quark-confirm error:", exc, flush=True)
                return r.send(
                    chat_id,
                    f"⚠️ 暂未开始下载\n\n{pending['media_title']}\n"
                    f"原因：{str(exc)[:180]}\n\n"
                    "确认记录已保留，可以直接重试，不会重复创建任务。",
                    [
                        [{"text": "重试提交", "callback_data": f"quarkconfirm:{key}"}],
                        [
                            {"text": "修改剧名", "callback_data": f"quarkedit:{key}"},
                            {"text": "取消", "callback_data": f"quarkcancel:{key}"},
                        ],
                    ],
                )
            r.QUARK_CONFIRM_PENDING.pop(key, None)
            r.save_quark_confirm_pending()
            return result
        if data.startswith("quarkedit:"):
            key = data.split(":", 1)[1]
            pending = r.QUARK_CONFIRM_PENDING.pop(key, None)
            r.save_quark_confirm_pending()
            if not pending:
                return r.send(chat_id, "这个确认已过期，请重新发送分享链接。", r.home_keyboard())
            return r.request_quark_title(chat_id, pending["url"], pending["media_title"], "请修正媒体身份")
        if data.startswith("quarkcancel:"):
            key = data.split(":", 1)[1]
            r.QUARK_CONFIRM_PENDING.pop(key, None)
            r.save_quark_confirm_pending()
            return r.send(chat_id, "已取消这次夸克下载。", r.home_keyboard())
        if data.startswith("quarkselect:"):
            _, key, index_text = data.split(":", 2)
            pending = r.QUARK_PENDING.pop(key, None)
            r.save_quark_pending()
            if not pending:
                return r.send(chat_id, "这个夸克选择已过期，请重新发送分享链接。", r.home_keyboard())
            try:
                folder = pending["folders"][int(index_text)]
            except (ValueError, IndexError):
                return r.send(chat_id, "文件夹选择无效，请重新发送分享链接。", r.home_keyboard())
            selected_url = r.selected_share_url(pending["url"], folder)
            title = pending.get("title_hint")
            if not title:
                folder_title = r.folder_media_title(folder["name"])
                try:
                    title = r.moviepilot_media_title(folder_title) if folder_title else None
                except RuntimeError as exc:
                    return r.request_quark_title(
                        chat_id,
                        selected_url,
                        folder["name"],
                        str(exc),
                        pending.get("raw_title") or folder_title,
                    )
            if title:
                return r.confirm_quark_download(chat_id, selected_url, title)
            return r.request_quark_title(
                chat_id,
                selected_url,
                folder["name"],
                pending.get("title_error"),
                pending.get("raw_title"),
            )
        if data.startswith("category:"):
            return r.add_to_qbit(chat_id, user_id, data.split(":", 1)[1])
        if data.startswith("task:"):
            return r.show_task(chat_id, data.split(":", 1)[1])
        if data.startswith("aria:"):
            return r.show_aria_task(chat_id, data.split(":", 1)[1])
        if data.startswith("ariaaction:"):
            _, action, short_gid = data.split(":", 2)
            return r.aria_action(chat_id, action, short_gid)
        if data.startswith("ariadeleteask:"):
            short_gid = data.split(":", 1)[1]
            return r.send(
                chat_id,
                "确定删除 Aria2 任务及 VPS 未完成文件吗？",
                [
                    [
                        {"text": "确认删除", "callback_data": f"ariadeleteyes:{short_gid}"},
                        {"text": "取消", "callback_data": f"aria:{short_gid}"},
                    ]
                ],
            )
        if data.startswith("ariadeleteyes:"):
            return r.aria_delete(chat_id, data.split(":", 1)[1])
        if data.startswith("action:"):
            _, action, short_hash = data.split(":", 2)
            item = r.find_task(short_hash)
            if not item:
                return r.send(chat_id, "任务不存在。", r.home_keyboard())
            r.qbit_action(action, item["hash"])
            if action == "pause" and item["hash"] in r.QUEUE:
                r.QUEUE.remove(item["hash"])
                r.save_queue()
            elif action == "resume" and item.get("progress", 0) < 1 and item["hash"] not in r.QUEUE:
                r.QUEUE.append(item["hash"])
                r.save_queue()
            r.run_queue()
            return r.show_task(chat_id, short_hash)
        if data.startswith("deleteask:"):
            short_hash = data.split(":", 1)[1]
            return r.send(
                chat_id,
                "确定删除此任务及 VPS 中已下载文件吗？这个操作无法恢复。",
                [
                    [
                        {"text": "确认删除", "callback_data": f"deleteyes:{short_hash}"},
                        {"text": "取消", "callback_data": f"task:{short_hash}"},
                    ]
                ],
            )
        if data.startswith("deleteyes:"):
            item = r.find_task(data.split(":", 1)[1])
            if not item:
                return r.send(chat_id, "任务不存在。", r.home_keyboard())
            r.qbit("/api/v2/torrents/delete", {"hashes": item["hash"], "deleteFiles": "true"})
            if item["hash"] in r.QUEUE:
                r.QUEUE.remove(item["hash"])
                r.save_queue()
            r.run_queue()
            return r.send(chat_id, "任务及 VPS 文件已删除。", r.home_keyboard())

    def handle(self, update):
        r = self.runtime()
        r.OFFSET = update["update_id"] + 1
        if update.get("callback_query"):
            return self.handle_callback(update["callback_query"])
        message = update.get("message")
        if not message or message.get("from", {}).get("id") != r.OWNER:
            return
        chat_id = message["chat"]["id"]
        text = r.message_text_with_links(message)
        if str(chat_id) in r.ACCOUNT_PENDING:
            if text == "/cancel":
                r.ACCOUNT_PENDING.pop(str(chat_id), None)
                r.save_account_pending()
                return r.send(chat_id, "已取消开号。", r.home_keyboard())
            if not text or text.startswith("/"):
                return r.send(chat_id, "请直接输入用户名，或输入 /cancel 取消。")
            try:
                account = r.EMBY_CLIENT.create_viewer(text, r.SETTINGS.emby_default_password)
            except RuntimeError as exc:
                return r.send(chat_id, f"开号失败：{exc}\n\n请换一个用户名重试，或输入 /cancel 取消。")
            r.ACCOUNT_PENDING.pop(str(chat_id), None)
            r.save_account_pending()
            login = (
                f"\n登录地址：{r.explicit_web_port(r.SETTINGS.emby_public_url)}"
                if r.SETTINGS.emby_public_url else ""
            )
            return r.send(
                chat_id,
                f"✅ Emby 普通观看账号已创建\n\n用户名：{account['username']}\n"
                f"密码：{r.SETTINGS.emby_default_password}{login}\n\n"
                "已关闭管理、删除、下载、字幕管理和共享权限。",
                r.home_keyboard(),
            )
        document = message.get("document")
        if document:
            return r.add_torrent_file(
                chat_id, r.OWNER, document, message["message_id"],
                r.extract_post_title(message.get("caption", "")),
            )
        title_pending = r.QUARK_TITLE_PENDING.get(str(chat_id))
        if title_pending:
            if text == "/cancel":
                r.QUARK_TITLE_PENDING.pop(str(chat_id), None)
                r.save_quark_title_pending()
                return r.send(chat_id, "已取消这次夸克下载。", r.home_keyboard())
            repeated_quark_share = r.extract_quark_share(text)
            if repeated_quark_share:
                r.QUARK_TITLE_PENDING.pop(str(chat_id), None)
                r.save_quark_title_pending()
                return r.add_quark_share(
                    chat_id, repeated_quark_share, message["message_id"], r.extract_post_title(text)
                )
            if text and not text.startswith("/") and not text.startswith("magnet:"):
                try:
                    title = r.resolve_pending_media_text(text, title_pending)
                except RuntimeError as exc:
                    return r.send(chat_id, str(exc))
                r.QUARK_TITLE_PENDING.pop(str(chat_id), None)
                r.save_quark_title_pending()
                return r.confirm_quark_download(chat_id, title_pending["url"], title)
        quark_share = r.extract_quark_share(text)
        if quark_share:
            return r.add_quark_share(chat_id, quark_share, message["message_id"], r.extract_post_title(text))
        magnet = r.extract_magnet(text)
        if magnet:
            return r.add_magnet(chat_id, r.OWNER, magnet, message["message_id"], r.extract_post_title(text))
        if text in {"/start", "/menu"}:
            return r.home(chat_id, message["from"].get("first_name", ""))
        if text == "/help":
            return r.send(
                chat_id,
                f"发送 magnet、夸克分享链接或上传 .torrent 文件即可。\n系统会智能分类，同时最多下载 {r.MAX_ACTIVE_DOWNLOADS} 个任务。\n"
                "下载完成后由 MoviePilot 自动整理、命名并上传。\n\n/tasks 可查看任务。",
            )
        if self.legacy_command(chat_id, text):
            return
        if any(word in text for word in ("空间", "硬盘", "云盘", "容量", "服务器状态")):
            return r.server_status(chat_id)
        if any(word in text for word in ("任务", "队列", "下载进度")):
            return r.show_tasks(chat_id)
        detected_title = r.extract_post_title(text)
        if detected_title or any(marker in text for marker in ("投稿ID", "资源信息", "投稿来源")):
            title_line = f"\n\n识别到标题：{detected_title}" if detected_title else ""
            return r.send(
                chat_id,
                f"⚠️ 未检测到下载链接{title_line}\n\n"
                "请发送包含夸克链接、magnet 链接的完整消息，或上传 .torrent 文件。",
                r.home_keyboard(),
            )
        def send_ai_reply(cid=chat_id, question=text):
            r.send(cid, r.ai_reply(question))
        threading.Thread(target=send_ai_reply, daemon=True).start()
