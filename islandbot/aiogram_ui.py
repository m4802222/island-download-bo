"""aiogram 3 + aiogram-dialog preview UI.

The download/media business rules stay in :mod:`islandbot.runtime`.  This
module only owns the Telegram interaction layer, so switching back to the
legacy poller does not change task behaviour.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, StartMode, Window, setup_dialogs
from aiogram_dialog.widgets.kbd import Button, Column, Row, Select
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.text import Const, Format

from . import runtime

LOGGER = logging.getLogger(__name__)


class MainSG(StatesGroup):
    home = State()
    tasks = State()
    task = State()
    delete = State()
    status = State()
    account = State()


ACCOUNT_WAITING: set[int] = set()
router = Router(name="island-download-bot-input")


def _owner(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == runtime.OWNER)


async def _answer(callback: CallbackQuery) -> None:
    try:
        await callback.answer()
    except Exception:
        pass


def _qbit_row(item: dict) -> dict:
    progress = float(item.get("progress") or 0) * 100
    state = item.get("state") or "未知"
    speed = int(item.get("dlspeed") or 0) / 1024 / 1024
    return {
        "id": f"qbit:{item['hash'][:8]}",
        "display": f"⬇️ {str(item.get('name') or '未命名')[:42]} · {progress:.0f}% · {state} · {speed:.1f} MiB/s",
    }


def _aria_row(item: dict) -> dict:
    return {
        "id": f"aria:{str(item.get('gid') or '')[:8]}",
        "display": f"⚡ {runtime.aria2_name(item)[:42]} · {runtime.aria2_percent(item):.0f}% · {item.get('status', '未知')}",
    }


def _load_task_view() -> dict:
    rows: list[dict] = []
    try:
        qbit_items = [item for item in runtime.task_list() if float(item.get("progress") or 0) < 1]
        rows.extend(_qbit_row(item) for item in qbit_items[:20])
    except Exception as exc:
        LOGGER.warning("qBittorrent task list failed: %s", exc)
    try:
        aria_items = [
            item for item in runtime.aria2_recent()
            if item.get("gid") in runtime.ARIA2_TRACKED
            and item.get("status") in {"active", "waiting", "paused"}
        ]
        rows.extend(_aria_row(item) for item in aria_items[:20])
    except Exception as exc:
        LOGGER.warning("Aria2 task list failed: %s", exc)
    completed = 0
    try:
        cutoff = runtime.time.time() - 24 * 60 * 60
        completed = sum(
            1 for item in runtime.task_list()
            if float(item.get("progress") or 0) >= 1
            and float(item.get("completion_on") or 0) >= cutoff
        )
    except Exception:
        pass
    completed += sum(1 for item in runtime.ARIA2_TRACKED.values() if item.get("notified"))
    queue_count = len(runtime.QUARK_QUEUE) + (1 if runtime.QUARK_ACTIVE else 0)
    return {
        "task_rows": rows,
        "task_summary": (
            "📥 下载任务\n\n"
            + ("当前有下载任务，点击任意任务查看暂停/继续/删除。" if rows else "当前无下载任务")
            + f"\n夸克转存队列：{queue_count}"
            + (f"\n最近完成：{completed} 项" if completed else "")
        ),
    }


def _task_detail(task_id: str) -> dict:
    kind, _, short_id = task_id.partition(":")
    if kind == "aria":
        item = runtime.find_aria_task(short_id)
        if not item:
            return {"task_text": "任务不存在或已被清理。", "task_control": "返回任务列表", "can_delete": False}
        total = int(item.get("totalLength") or 0) / runtime.GIB
        done = int(item.get("completedLength") or 0) / runtime.GIB
        speed = int(item.get("downloadSpeed") or 0) / 1024 / 1024
        status = item.get("status", "未知")
        control = "暂停" if status == "active" else "继续"
        return {
            "task_text": f"⚡ {runtime.aria2_name(item)}\n\n进度：{runtime.aria2_percent(item):.1f}%\n状态：{status}\n大小：{done:.2f} / {total:.2f} GB\n速度：{speed:.2f} MiB/s",
            "task_control": control,
            "can_delete": status in {"active", "waiting", "paused"},
            "task_id": task_id,
        }
    item = runtime.find_task(short_id)
    if not item:
        return {"task_text": "任务不存在或已被清理。", "task_control": "返回任务列表", "can_delete": False}
    progress = float(item.get("progress") or 0) * 100
    state = item.get("state", "未知")
    size = int(item.get("size") or 0) / runtime.GIB
    control = "暂停" if state not in {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"} else "继续"
    return {
        "task_text": f"⬇️ {item.get('name', '')}\n\n进度：{progress:.1f}%\n状态：{state}\n大小：{size:.2f} GB\n哈希：{item['hash'][:8]}",
        "task_control": control,
        "can_delete": True,
        "task_id": task_id,
    }


async def tasks_getter(**_: object) -> dict:
    return await asyncio.to_thread(_load_task_view)


async def task_getter(dialog_manager: DialogManager, **_: object) -> dict:
    task_id = str(dialog_manager.dialog_data.get("task_id") or "")
    return await asyncio.to_thread(_task_detail, task_id)


async def status_getter(**_: object) -> dict:
    text = await asyncio.to_thread(runtime.server_status_text)
    return {"status_text": text}


async def go_home(callback: CallbackQuery, _: Button, manager: DialogManager) -> None:
    await _answer(callback)
    await manager.switch_to(MainSG.home)


async def open_tasks(callback: CallbackQuery, _: Button, manager: DialogManager) -> None:
    await _answer(callback)
    await manager.switch_to(MainSG.tasks)


async def open_status(callback: CallbackQuery, _: Button, manager: DialogManager) -> None:
    await _answer(callback)
    await manager.switch_to(MainSG.status)


async def open_account(callback: CallbackQuery, _: Button, manager: DialogManager) -> None:
    await _answer(callback)
    chat_id = callback.message.chat.id if callback.message else 0
    ACCOUNT_WAITING.add(chat_id)
    await manager.switch_to(MainSG.account)


async def task_selected(callback: CallbackQuery, _: Select, manager: DialogManager, item_id: str) -> None:
    await _answer(callback)
    manager.dialog_data["task_id"] = item_id
    await manager.switch_to(MainSG.task)


async def task_toggle(callback: CallbackQuery, _: Button, manager: DialogManager) -> None:
    await _answer(callback)
    task_id = str(manager.dialog_data.get("task_id") or "")

    def action() -> None:
        kind, _, short_id = task_id.partition(":")
        if kind == "aria":
            item = runtime.find_aria_task(short_id)
            if not item:
                return
            method = "aria2.forcePause" if item.get("status") == "active" else "aria2.unpause"
            runtime.aria2_rpc(method, [item["gid"]])
            return
        item = runtime.find_task(short_id)
        if not item:
            return
        running = item.get("state") not in {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}
        runtime.qbit_action("pause" if running else "resume", item["hash"])
        if running and item["hash"] in runtime.QUEUE:
            runtime.QUEUE.remove(item["hash"])
            runtime.save_queue()
        elif not running and float(item.get("progress") or 0) < 1 and item["hash"] not in runtime.QUEUE:
            runtime.QUEUE.append(item["hash"])
            runtime.save_queue()
        runtime.run_queue()

    await asyncio.to_thread(action)
    await manager.switch_to(MainSG.task)


async def ask_delete(callback: CallbackQuery, _: Button, manager: DialogManager) -> None:
    await _answer(callback)
    await manager.switch_to(MainSG.delete)


async def delete_task(callback: CallbackQuery, _: Button, manager: DialogManager) -> None:
    await _answer(callback)
    task_id = str(manager.dialog_data.get("task_id") or "")

    def action() -> None:
        kind, _, short_id = task_id.partition(":")
        if kind == "aria":
            item = runtime.find_aria_task(short_id)
            if item:
                runtime.aria2_rpc("aria2.forceRemove", [item["gid"]])
                runtime.delete_aria_files(item)
                runtime.ARIA2_TRACKED.pop(item["gid"], None)
                runtime.save_aria2_tracked()
            return
        item = runtime.find_task(short_id)
        if item:
            runtime.qbit("/api/v2/torrents/delete", {"hashes": item["hash"], "deleteFiles": "true"})
            if item["hash"] in runtime.QUEUE:
                runtime.QUEUE.remove(item["hash"])
                runtime.save_queue()
            runtime.run_queue()

    await asyncio.to_thread(action)
    await manager.switch_to(MainSG.tasks)


async def account_cancel(callback: CallbackQuery, _: Button, manager: DialogManager) -> None:
    await _answer(callback)
    chat_id = callback.message.chat.id if callback.message else 0
    ACCOUNT_WAITING.discard(chat_id)
    await manager.switch_to(MainSG.home)


async def account_message(message: Message, manager: DialogManager) -> None:
    if not _owner(message):
        return
    chat_id = message.chat.id
    text = (message.text or "").strip()
    if chat_id not in ACCOUNT_WAITING:
        return
    if text == "/cancel":
        ACCOUNT_WAITING.discard(chat_id)
        await manager.switch_to(MainSG.home)
        return
    if not text or text.startswith("/"):
        await message.answer("请直接输入用户名，或输入 /cancel 取消。")
        return

    def create() -> dict:
        return runtime.EMBY_CLIENT.create_viewer(text, runtime.SETTINGS.emby_default_password)

    try:
        account = await asyncio.to_thread(create)
    except RuntimeError as exc:
        await message.answer(f"开号失败：{exc}\n请换一个用户名重试，或输入 /cancel 取消。")
        return
    ACCOUNT_WAITING.discard(chat_id)
    login = (
        f"\n登录地址：{runtime.explicit_web_port(runtime.SETTINGS.emby_public_url)}"
        if runtime.SETTINGS.emby_public_url else ""
    )
    await message.answer(
        f"✅ Emby 普通观看账号已创建\n\n用户名：{account['username']}\n"
        f"密码：{runtime.SETTINGS.emby_default_password}{login}\n\n"
        "已关闭管理、删除、下载、字幕管理和共享权限。"
    )
    await manager.switch_to(MainSG.home)


@router.message(CommandStart())
@router.message(Command("menu"))
async def start(message: Message, dialog_manager: DialogManager) -> None:
    if _owner(message):
        await dialog_manager.start(MainSG.home, mode=StartMode.RESET_STACK)


@router.message()
async def incoming_message(message: Message, dialog_manager: DialogManager) -> None:
    """Forward links/files to the proven business handler.

    The UI layer never reimplements Quark/qBittorrent parsing.  This keeps the
    preview small and makes rollback to the legacy UI safe.
    """
    if not _owner(message):
        return
    if message.chat.id in ACCOUNT_WAITING:
        await account_message(message, dialog_manager)
        return
    if not message.text and not message.document:
        return
    raw = message.model_dump(by_alias=True, exclude_none=True)
    await asyncio.to_thread(runtime.handle, {"update_id": 0, "message": raw})


async def dialog_message_input(
    message: Message,
    _: MessageInput,
    dialog_manager: DialogManager,
) -> None:
    """Keep the active dialog from swallowing ordinary download messages."""
    await incoming_message(message, dialog_manager)


dialog = Dialog(
    Window(
        Const("🏝 Island Download\n\n发送 magnet、.torrent 或夸克分享链接。\n下载完成后 MoviePilot 自动整理并上传。"),
        MessageInput(dialog_message_input),
        Row(
            Button(Const("👤 开号"), id="account", on_click=open_account),
            Button(Const("📋 我的任务"), id="tasks", on_click=open_tasks),
        ),
        Button(Const("🖥 状态与设置"), id="status", on_click=open_status),
        state=MainSG.home,
    ),
    Window(
        Format("{task_summary}"),
        MessageInput(dialog_message_input),
        Column(Select(Format("{item[display]}"), id="task_select", item_id_getter=lambda item: item["id"], items="task_rows", on_click=task_selected)),
        Row(Button(Const("刷新"), id="refresh", on_click=open_tasks), Button(Const("← 主菜单"), id="home", on_click=go_home)),
        state=MainSG.tasks,
        getter=tasks_getter,
    ),
    Window(
        Format("{task_text}"),
        MessageInput(dialog_message_input),
        Button(Format("{task_control}"), id="toggle", on_click=task_toggle),
        Button(Const("删除"), id="delete", on_click=ask_delete),
        Button(Const("← 任务列表"), id="back", on_click=open_tasks),
        state=MainSG.task,
        getter=task_getter,
    ),
    Window(
        Const("确定删除这个任务及 VPS 文件吗？这个操作无法恢复。"),
        MessageInput(dialog_message_input),
        Row(Button(Const("确认删除"), id="yes", on_click=delete_task), Button(Const("取消"), id="no", on_click=lambda c, w, m: m.switch_to(MainSG.task))),
        state=MainSG.delete,
    ),
    Window(
        Format("{status_text}"),
        MessageInput(dialog_message_input),
        Row(Button(Const("刷新"), id="refresh", on_click=open_status), Button(Const("← 主菜单"), id="home", on_click=go_home)),
        state=MainSG.status,
        getter=status_getter,
    ),
    Window(
        Const("👤 开号\n\n请输入新 Emby 用户名。\n密码使用系统默认值，仅有普通观看权限。\n输入 /cancel 可取消。"),
        MessageInput(dialog_message_input),
        Button(Const("取消"), id="cancel", on_click=account_cancel),
        state=MainSG.account,
    ),
)


async def _maintenance_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(runtime.service_tick)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("maintenance tick failed: %s", exc)
        await asyncio.sleep(5)


async def _run() -> None:
    bot = Bot(token=runtime.TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(dialog)
    setup_dialogs(dp)
    # Messages not consumed by a window's MessageInput are handled by the
    # compatibility router (for example before a dialog has been started).
    dp.include_router(router)
    maintenance = asyncio.create_task(_maintenance_loop())
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        maintenance.cancel()
        await maintenance
        await bot.session.close()


def run() -> None:
    """Synchronous entry point used by :mod:`islandbot.app`."""
    print("Island Download Bot aiogram-dialog UI started", flush=True)
    asyncio.run(_run())
