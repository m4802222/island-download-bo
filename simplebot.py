import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER_ID"])
QBIT_URL = os.environ.get("QBIT_URL", "http://qbittorrent:8080").rstrip("/")
QBIT_USER = os.environ["QBIT_USERNAME"]
QBIT_PASSWORD = os.environ["QBIT_PASSWORD"]
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:1.7b")
COOKIE = ""
OFFSET = 0
PENDING = {}

DATA_DIR = Path("/data")
DATA_DIR.mkdir(exist_ok=True)
DONE_FILE = DATA_DIR / "done.json"
SEEN = set(json.loads(DONE_FILE.read_text())) if DONE_FILE.exists() else set()
QUEUE_FILE = DATA_DIR / "queue.json"
QUEUE = list(json.loads(QUEUE_FILE.read_text())) if QUEUE_FILE.exists() else []
QUEUE_READY = QUEUE_FILE.exists()
BLOCKED_FILE = DATA_DIR / "blocked.json"
BLOCKED = set(json.loads(BLOCKED_FILE.read_text())) if BLOCKED_FILE.exists() else set()
RESERVE_GIB = int(os.environ.get("MIN_FREE_GIB", "10"))
GIB = 1024 * 1024 * 1024
EXPIRY_FILE = DATA_DIR / "expiry.json"
EXPIRING = list(json.loads(EXPIRY_FILE.read_text())) if EXPIRY_FILE.exists() else []

CATEGORIES = ["国产电影", "国产动漫", "国产剧集", "港台剧集", "欧美电影", "欧美剧集", "日韩电影", "日韩剧集", "日韩动漫"]


def request(url, data=None, headers=None):
    try:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, body, headers or {})
        response = urllib.request.urlopen(req, timeout=40)
        return response.status, response.read().decode(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace"), exc.headers
    except Exception as exc:
        return 599, str(exc), {}


def json_request(url, payload, timeout=100):
    try:
        req = urllib.request.Request(
            url,
            json.dumps(payload, ensure_ascii=False).encode(),
            {"Content-Type": "application/json"},
        )
        response = urllib.request.urlopen(req, timeout=timeout)
        return response.status, response.read().decode(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace"), exc.headers
    except Exception as exc:
        return 599, str(exc), {}


def ai_reply(question):
    system = (
        "你是 IslandDownload 的私人中文助手。回答简洁、实用，不超过 120 个汉字。"
        "你可以解释下载队列、MoviePilot、Emby、Google Drive、VPS 日志和媒体整理。"
        "你没有执行删除、下载、重启的权限；涉及这些操作时说明需要用户通过机器人按钮确认。"
        "不知道实时数据时明确说请点击“状态与设置”或“我的任务”，不要编造。"
    )
    status, body, _ = json_request(
        f"{OLLAMA_URL}/api/chat",
        {
            "model": OLLAMA_MODEL,
            "stream": False,
            "think": False,
            "keep_alive": "5m",
            "options": {"temperature": 0.2, "num_predict": 220},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": question}],
        },
    )
    if status >= 300:
        print("ollama error:", body[:300], flush=True)
        return "AI 暂时不可用，请稍后再试。"
    try:
        answer_text = json.loads(body)["message"]["content"].strip()
        return answer_text or "AI 没有返回内容，请换一种问法。"
    except Exception:
        return "AI 返回格式异常，请稍后再试。"


def qbit_login():
    global COOKIE
    last_status = 0
    # qBittorrent 5 returns HTTP 204 for a successful empty login response.
    # Retry transient network/container-startup failures without leaking credentials.
    for attempt in range(3):
        status, _, headers = request(
            f"{QBIT_URL}/api/v2/auth/login",
            {"username": QBIT_USER, "password": QBIT_PASSWORD},
        )
        last_status = status
        if status in (200, 204):
            COOKIE = headers.get("Set-Cookie", "").split(";", 1)[0]
            if COOKIE:
                return
        if attempt < 2:
            time.sleep(attempt + 1)
    if last_status in (401, 403):
        raise RuntimeError("qBittorrent 账号或密码被拒绝")
    raise RuntimeError(f"qBittorrent 登录失败（HTTP {last_status}）")


def qbit(path, data=None):
    global COOKIE
    if not COOKIE:
        qbit_login()
    status, body, _ = request(f"{QBIT_URL}{path}", data, {"Cookie": COOKIE})
    if status in (401, 403):
        COOKIE = ""
        qbit_login()
        status, body, _ = request(f"{QBIT_URL}{path}", data, {"Cookie": COOKIE})
    if status >= 300:
        raise RuntimeError(body[:180])
    return body


def telegram(method, data):
    status, body, _ = request(f"https://api.telegram.org/bot{TOKEN}/{method}", data)
    if status >= 300:
        raise RuntimeError(body)
    return json.loads(body)


def send(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard}, ensure_ascii=False)
    return telegram("sendMessage", payload)


def send_temporary(chat_id, text, lifetime_seconds=300):
    response = send(chat_id, text)
    message_id = response.get("result", {}).get("message_id")
    if message_id:
        EXPIRING.append({"chat_id": chat_id, "message_id": message_id, "delete_at": time.time() + lifetime_seconds})
        EXPIRY_FILE.write_text(json.dumps(EXPIRING))


def delete_expired_messages():
    now = time.time()
    remaining = []
    for item in EXPIRING:
        if item["delete_at"] > now:
            remaining.append(item)
            continue
        try:
            telegram("deleteMessage", {"chat_id": item["chat_id"], "message_id": item["message_id"]})
        except Exception as exc:
            print("delete-expired-message error:", exc, flush=True)
    if len(remaining) != len(EXPIRING):
        EXPIRING[:] = remaining
        EXPIRY_FILE.write_text(json.dumps(EXPIRING))


def answer(callback_id, text=None):
    data = {"callback_query_id": callback_id}
    if text:
        data["text"] = text
    telegram("answerCallbackQuery", data)


def home_keyboard():
    return [
        [{"text": "➕ 添加下载", "callback_data": "home:add"}, {"text": "📋 我的任务", "callback_data": "home:tasks"}],
        [{"text": "🖥 状态与设置", "callback_data": "home:server"}],
    ]


def category_keyboard():
    rows = []
    for index in range(0, len(CATEGORIES), 3):
        rows.append([{"text": item, "callback_data": f"category:{item}"} for item in CATEGORIES[index:index + 3]])
    rows.append([{"text": "🤖 智能分类", "callback_data": "category:__auto__"}])
    rows.append([{"text": "取消", "callback_data": "home:home"}])
    return rows


def home(chat_id, first_name=""):
    greeting = f"你好，{first_name}。" if first_name else "你好。"
    send(chat_id, f"{greeting}\n\nIsland Download\n发送 magnet 链接，或选择操作。\n也可以直接发送中文问题给 AI 助手。", home_keyboard())


def task_list():
    return json.loads(qbit("/api/v2/torrents/info?tag=islandbot&sort=added_on&reverse=true"))


def save_queue():
    QUEUE_FILE.write_text(json.dumps(QUEUE))


def save_blocked():
    BLOCKED_FILE.write_text(json.dumps(list(BLOCKED)))


def file_size(item):
    return int(item.get("total_size") or item.get("size") or 0)


def has_enough_space(item):
    """Return whether the next download fits, with a fixed safety reserve."""
    size = file_size(item)
    if size <= 0:
        # A magnet can need a short metadata phase before qBittorrent knows size.
        return True, size, shutil.disk_usage("/downloads").free
    free = shutil.disk_usage("/downloads").free
    return size <= max(0, free - RESERVE_GIB * GIB), size, free


def google_drive_capacity():
    """Read the configured Google Drive quota through MoviePilot's rclone remote."""
    try:
        result = subprocess.run(
            ["rclone", "--config", "/rclone/rclone.conf", "about", "MP:"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "rclone about failed")
        values = {}
        for line in result.stdout.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                values[key.strip()] = value.strip()
        if not values.get("Total"):
            raise RuntimeError("未返回配额")
        return f"总 {values.get('Total', '—')} · 已用 {values.get('Used', '—')} · 可用 {values.get('Free', '—')}"
    except Exception as exc:
        print("google-drive-capacity error:", exc, flush=True)
        return "暂时无法读取"


def run_queue():
    """Keep exactly one IslandDownloadBot task downloading at a time."""
    global QUEUE_READY
    tasks = task_list()
    task_by_hash = {item["hash"]: item for item in tasks}

    # First upgrade: put pre-existing unfinished bot tasks into the same queue.
    if not QUEUE_READY:
        QUEUE.extend(item["hash"] for item in sorted(tasks, key=lambda item: item.get("added_on", 0)) if item.get("progress", 0) < 1)
        QUEUE_READY = True

    # Finished or deleted tasks no longer block the next download.
    QUEUE[:] = [task_hash for task_hash in QUEUE if task_hash in task_by_hash and task_by_hash[task_hash].get("progress", 0) < 1]
    save_queue()
    stale_blocked = BLOCKED - set(QUEUE)
    if stale_blocked:
        BLOCKED.difference_update(stale_blocked)
        save_blocked()
    if not QUEUE:
        return

    current = QUEUE[0]
    # Every later item stays paused, even after a container restart.
    for task_hash in QUEUE[1:]:
        item = task_by_hash.get(task_hash)
        if item and item.get("state") not in {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}:
            qbit("/api/v2/torrents/pause", {"hashes": task_hash})
    item = task_by_hash.get(current)
    if not item:
        return
    fits, size, free = has_enough_space(item)
    if not fits:
        if item.get("state") not in {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}:
            qbit("/api/v2/torrents/pause", {"hashes": current})
        if current not in BLOCKED:
            BLOCKED.add(current)
            save_blocked()
            send(OWNER, f"⚠️ 队列暂停：空间不足\n\n{item.get('name', '')[:55]}\n资源大小：{size / GIB:.1f} GB\n当前可用：{free / GIB:.1f} GB\n安全预留：{RESERVE_GIB} GB\n\n等待 MoviePilot 上传并清理文件后，将自动继续。")
        return
    if current in BLOCKED:
        BLOCKED.remove(current)
        save_blocked()
    if item.get("state") in {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}:
        qbit("/api/v2/torrents/resume", {"hashes": current})


def show_tasks(chat_id):
    tasks = task_list()
    if not tasks:
        return send(chat_id, "暂无机器人添加的任务。\n\n点击“添加下载”或直接发送 magnet 链接。", home_keyboard())
    lines = []
    buttons = []
    for item in tasks[:6]:
        short_hash = item["hash"][:8]
        progress = item.get("progress", 0) * 100
        state = item.get("state", "未知")
        category = item.get("category") or "未分类"
        position = QUEUE.index(item["hash"]) + 1 if item["hash"] in QUEUE else "—"
        lines.append(f"队列 {position} · {progress:.0f}% · {state}\n{item.get('name', '')[:42]}\n{category}")
        buttons.append([{"text": f"查看 {short_hash}", "callback_data": f"task:{short_hash}"}])
    buttons.append([{"text": "← 主菜单", "callback_data": "home:home"}])
    send(chat_id, "📋 下载任务\n\n" + "\n\n".join(lines), buttons)


def find_task(short_hash):
    matches = [item for item in task_list() if item["hash"].startswith(short_hash)]
    return matches[0] if len(matches) == 1 else None


def show_task(chat_id, short_hash):
    item = find_task(short_hash)
    if not item:
        return send(chat_id, "任务不存在，可能已被删除。", home_keyboard())
    progress = item.get("progress", 0) * 100
    state = item.get("state", "未知")
    size = item.get("size", 0) / 1024 / 1024 / 1024
    text = (
        f"📥 {item.get('name', '')}\n\n"
        f"分类：{item.get('category') or '未分类'}\n"
        f"进度：{progress:.1f}%\n状态：{state}\n大小：{size:.2f} GB\n"
        f"哈希：{item['hash'][:8]}"
    )
    running = state not in {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}
    control = "暂停" if running else "继续"
    action = "pause" if running else "resume"
    keyboard = [
        [{"text": control, "callback_data": f"action:{action}:{short_hash}"}, {"text": "删除", "callback_data": f"deleteask:{short_hash}"}],
        [{"text": "← 任务列表", "callback_data": "home:tasks"}],
    ]
    send(chat_id, text, keyboard)


def server_status(chat_id):
    try:
        info = json.loads(qbit("/api/v2/transfer/info"))
        tasks = task_list()
        disk = shutil.disk_usage("/downloads")
    except Exception as exc:
        print("server-status error:", exc, flush=True)
        return send(chat_id, "🖥 状态暂时无法读取。\n请稍后点击 /start 再试。", [[{"text": "← 主菜单", "callback_data": "home:home"}]])
    download = info.get("dl_info_speed", 0) / 1024 / 1024
    upload = info.get("up_info_speed", 0) / 1024 / 1024
    active = sum(1 for item in tasks if item.get("progress", 0) < 1)
    used = disk.total - disk.free
    drive_capacity = google_drive_capacity()
    text = (
        "🖥 状态与设置\n\n"
        "服务：在线\n"
        "下载器：qBittorrent 已连接\n"
        f"下载速度：{download:.2f} MiB/s\n"
        f"上传速度：{upload:.2f} MiB/s\n"
        f"活动任务：{active}\n\n"
        f"VPS 下载盘：总 {disk.total / GIB:.1f} GB · 已用 {used / GIB:.1f} GB · 可用 {disk.free / GIB:.1f} GB\n"
        f"安全预留：{RESERVE_GIB} GB\n\n"
        f"Google Drive：{drive_capacity}\n\n"
        f"账号：管理员（{OWNER}）\n"
        "权限：下载、任务管理、服务器状态"
    )
    send(chat_id, text, [[{"text": "刷新", "callback_data": "home:server"}, {"text": "← 主菜单", "callback_data": "home:home"}]])


def add_magnet(chat_id, user_id, magnet, source_message_id):
    PENDING[user_id] = {"magnet": magnet, "source_message_id": source_message_id}
    send(chat_id, "选择影视分类：", category_keyboard())


def add_to_qbit(chat_id, user_id, category):
    pending = PENDING.pop(user_id, None)
    if not pending:
        return send(chat_id, "这个下载请求已失效，请重新发送 magnet 链接。", home_keyboard())
    magnet = pending["magnet"]
    before = {item["hash"] for item in task_list()}
    add_data = {"urls": magnet, "tags": "islandbot", "autoTMM": "false", "paused": "true"}
    if category != "__auto__":
        add_data["category"] = category
    qbit("/api/v2/torrents/add", add_data)
    time.sleep(1)
    added = [item for item in task_list() if item["hash"] not in before]
    if not added:
        return send(chat_id, "未能确认新任务，请在“我的任务”中检查。", home_keyboard())
    new_task = max(added, key=lambda item: item.get("added_on", 0))
    QUEUE.append(new_task["hash"])
    save_queue()
    run_queue()
    # Keep private magnets out of chat history once their category is chosen.
    try:
        telegram("deleteMessage", {"chat_id": chat_id, "message_id": pending["source_message_id"]})
    except Exception as exc:
        print("delete-magnet error:", exc, flush=True)


def legacy_command(chat_id, text):
    if text == "/tasks":
        return show_tasks(chat_id)
    parts = text.split(maxsplit=1)
    if len(parts) != 2 or parts[0] not in {"/pause", "/resume", "/delete"}:
        return False
    item = find_task(parts[1].strip())
    if not item:
        send(chat_id, "没有找到唯一任务，请在“我的任务”中选择。")
        return True
    command = parts[0][1:]
    qbit(f"/api/v2/torrents/{command}", {"hashes": item["hash"], **({"deleteFiles": "true"} if command == "delete" else {})})
    if command in {"pause", "delete"} and item["hash"] in QUEUE:
        QUEUE.remove(item["hash"])
        save_queue()
    elif command == "resume" and item.get("progress", 0) < 1 and item["hash"] not in QUEUE:
        QUEUE.append(item["hash"])
        save_queue()
    run_queue()
    send(chat_id, f"已{ '删除' if command == 'delete' else ('暂停' if command == 'pause' else '继续') }：{item.get('name', '')[:45]}")
    return True


def handle_callback(callback):
    user_id = callback["from"]["id"]
    chat_id = callback["message"]["chat"]["id"]
    answer(callback["id"])
    if user_id != OWNER:
        return
    # Telegram has no native single-page UI. Remove the previous bot card before
    # rendering the next one, so the conversation stays clean and app-like.
    try:
        telegram("deleteMessage", {"chat_id": chat_id, "message_id": callback["message"]["message_id"]})
    except Exception:
        pass
    data = callback.get("data", "")
    if data == "home:home":
        return home(chat_id, callback["from"].get("first_name", ""))
    if data == "home:add":
        return send(chat_id, "请直接发送完整 magnet 链接。", [[{"text": "返回主页", "callback_data": "home:home"}]])
    if data == "home:tasks":
        return show_tasks(chat_id)
    if data == "home:server":
        return server_status(chat_id)
    if data.startswith("category:"):
        return add_to_qbit(chat_id, user_id, data.split(":", 1)[1])
    if data.startswith("task:"):
        return show_task(chat_id, data.split(":", 1)[1])
    if data.startswith("action:"):
        _, action, short_hash = data.split(":", 2)
        item = find_task(short_hash)
        if not item:
            return send(chat_id, "任务不存在。", home_keyboard())
        qbit(f"/api/v2/torrents/{action}", {"hashes": item["hash"]})
        if action == "pause" and item["hash"] in QUEUE:
            QUEUE.remove(item["hash"])
            save_queue()
        elif action == "resume" and item.get("progress", 0) < 1 and item["hash"] not in QUEUE:
            QUEUE.append(item["hash"])
            save_queue()
        run_queue()
        return show_task(chat_id, short_hash)
    if data.startswith("deleteask:"):
        short_hash = data.split(":", 1)[1]
        return send(chat_id, "确定删除此任务及 VPS 中已下载文件吗？这个操作无法恢复。", [[{"text": "确认删除", "callback_data": f"deleteyes:{short_hash}"}, {"text": "取消", "callback_data": f"task:{short_hash}"}]])
    if data.startswith("deleteyes:"):
        item = find_task(data.split(":", 1)[1])
        if not item:
            return send(chat_id, "任务不存在。", home_keyboard())
        qbit("/api/v2/torrents/delete", {"hashes": item["hash"], "deleteFiles": "true"})
        if item["hash"] in QUEUE:
            QUEUE.remove(item["hash"])
            save_queue()
        run_queue()
        return send(chat_id, "任务及 VPS 文件已删除。", home_keyboard())


def handle(update):
    global OFFSET
    OFFSET = update["update_id"] + 1
    if update.get("callback_query"):
        return handle_callback(update["callback_query"])
    message = update.get("message")
    if not message or message.get("from", {}).get("id") != OWNER:
        return
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    if text.startswith("magnet:?xt=urn:btih:"):
        return add_magnet(chat_id, OWNER, text, message["message_id"])
    if text in {"/start", "/menu"}:
        return home(chat_id, message["from"].get("first_name", ""))
    if text == "/help":
        return send(chat_id, "发送 magnet 链接后选择分类。\n下载完成后由 MoviePilot 自动整理并上传。\n\n/tasks 可查看任务。")
    if legacy_command(chat_id, text):
        return
    if any(word in text for word in ("空间", "硬盘", "云盘", "容量", "服务器状态")):
        return server_status(chat_id)
    if any(word in text for word in ("任务", "队列", "下载进度")):
        return show_tasks(chat_id)
    send(chat_id, ai_reply(text))


def watch_completed():
    global SEEN
    for item in task_list():
        if item.get("progress", 0) < 1 or item["hash"] in SEEN:
            continue
        SEEN.add(item["hash"])
        DONE_FILE.write_text(json.dumps(list(SEEN)))
        send_temporary(OWNER, f"✅ 下载完成\n\n{item.get('name', '')}\n分类：{item.get('category') or '未分类'}\n\nMoviePilot 将自动识别、整理并上传到 Google Drive。")
    run_queue()


while True:
    try:
        updates = telegram("getUpdates", {"offset": OFFSET, "timeout": 25, "allowed_updates": json.dumps(["message", "callback_query"])})
        for update in updates.get("result", []):
            handle(update)
        watch_completed()
        delete_expired_messages()
    except Exception as exc:
        print("error:", exc, flush=True)
        time.sleep(5)
