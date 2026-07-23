import json
import os
import shutil
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
COOKIE = ""
OFFSET = 0
PENDING = {}

DATA_DIR = Path("/data")
DATA_DIR.mkdir(exist_ok=True)
DONE_FILE = DATA_DIR / "done.json"
SEEN = set(json.loads(DONE_FILE.read_text())) if DONE_FILE.exists() else set()

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
    telegram("sendMessage", payload)


def answer(callback_id, text=None):
    data = {"callback_query_id": callback_id}
    if text:
        data["text"] = text
    telegram("answerCallbackQuery", data)


def home_keyboard():
    return [
        [{"text": "➕ 添加下载", "callback_data": "home:add"}, {"text": "📋 我的任务", "callback_data": "home:tasks"}],
        [{"text": "🖥 服务器状态", "callback_data": "home:server"}, {"text": "👤 账号与权限", "callback_data": "home:account"}],
        [{"text": "❔ 使用帮助", "callback_data": "home:help"}],
    ]


def category_keyboard():
    rows = []
    for index in range(0, len(CATEGORIES), 3):
        rows.append([{"text": item, "callback_data": f"category:{item}"} for item in CATEGORIES[index:index + 3]])
    rows.append([{"text": "取消", "callback_data": "home:home"}])
    return rows


def home(chat_id, first_name=""):
    greeting = f"你好，{first_name}。" if first_name else "你好。"
    send(chat_id, f"{greeting}\n\nIsland Download 是你的私人媒体下载台。\n选择操作，或直接发送 magnet 链接。", home_keyboard())


def task_list():
    return json.loads(qbit("/api/v2/torrents/info?tag=islandbot&sort=added_on&reverse=true"))


def show_tasks(chat_id):
    tasks = task_list()
    if not tasks:
        return send(chat_id, "暂无机器人添加的任务。\n\n点击“添加下载”或直接发送 magnet 链接。", home_keyboard())
    lines = []
    buttons = []
    for item in tasks[:10]:
        short_hash = item["hash"][:8]
        progress = item.get("progress", 0) * 100
        state = item.get("state", "未知")
        category = item.get("category") or "未分类"
        lines.append(f"{short_hash} · {progress:.0f}% · {state}\n{item.get('name', '')[:42]}\n{category}")
        buttons.append([{"text": f"管理 {short_hash}", "callback_data": f"task:{short_hash}"}])
    buttons.append([{"text": "刷新", "callback_data": "home:tasks"}, {"text": "返回主页", "callback_data": "home:home"}])
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
        [{"text": "返回任务", "callback_data": "home:tasks"}, {"text": "主页", "callback_data": "home:home"}],
    ]
    send(chat_id, text, keyboard)


def server_status(chat_id):
    info = json.loads(qbit("/api/v2/transfer/info"))
    tasks = task_list()
    disk = shutil.disk_usage("/downloads")
    download = info.get("dl_info_speed", 0) / 1024 / 1024
    upload = info.get("up_info_speed", 0) / 1024 / 1024
    active = sum(1 for item in tasks if item.get("progress", 0) < 1)
    text = (
        "🖥 服务器状态\n\n"
        "服务：在线\n"
        "下载器：qBittorrent 已连接\n"
        f"下载速度：{download:.2f} MiB/s\n"
        f"上传速度：{upload:.2f} MiB/s\n"
        f"活动任务：{active}\n"
        f"完成目录可用空间：{disk.free / 1024 / 1024 / 1024:.1f} GB"
    )
    send(chat_id, text, [[{"text": "刷新", "callback_data": "home:server"}, {"text": "返回主页", "callback_data": "home:home"}]])


def account(chat_id):
    send(chat_id, f"👤 账号与权限\n\n你的 Telegram ID：{OWNER}\n角色：系统管理员\n权限：下载、任务管理、服务器状态\n\n此机器人目前为私有模式，未授权用户无法操作。", [[{"text": "返回主页", "callback_data": "home:home"}]])


def help_text(chat_id):
    send(chat_id, "❔ 使用方法\n\n1. 点击“添加下载”，发送 magnet 链接。\n2. 点击影视分类。\n3. qBittorrent 下载完成后，MoviePilot 自动识别、改名、上传 Google Drive。\n4. 成功上传后清理 VPS 源文件。\n\n也可用：\n/tasks 查看任务\n/pause 哈希前8位\n/resume 哈希前8位\n/delete 哈希前8位", [[{"text": "返回主页", "callback_data": "home:home"}]])


def add_magnet(chat_id, user_id, magnet):
    PENDING[user_id] = magnet
    send(chat_id, "选择影视分类：", category_keyboard())


def add_to_qbit(chat_id, user_id, category):
    magnet = PENDING.pop(user_id, None)
    if not magnet:
        return send(chat_id, "这个下载请求已失效，请重新发送 magnet 链接。", home_keyboard())
    qbit("/api/v2/torrents/add", {"urls": magnet, "category": category, "tags": "islandbot", "autoTMM": "false"})
    send(chat_id, f"已加入下载队列\n分类：{category}\n\n完成后会自动交给 MoviePilot 整理。", home_keyboard())


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
    send(chat_id, f"已{ '删除' if command == 'delete' else ('暂停' if command == 'pause' else '继续') }：{item.get('name', '')[:45]}")
    return True


def handle_callback(callback):
    user_id = callback["from"]["id"]
    chat_id = callback["message"]["chat"]["id"]
    answer(callback["id"])
    if user_id != OWNER:
        return
    data = callback.get("data", "")
    if data == "home:home":
        return home(chat_id, callback["from"].get("first_name", ""))
    if data == "home:add":
        return send(chat_id, "请直接发送完整 magnet 链接。", [[{"text": "返回主页", "callback_data": "home:home"}]])
    if data == "home:tasks":
        return show_tasks(chat_id)
    if data == "home:server":
        return server_status(chat_id)
    if data == "home:account":
        return account(chat_id)
    if data == "home:help":
        return help_text(chat_id)
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
        return show_task(chat_id, short_hash)
    if data.startswith("deleteask:"):
        short_hash = data.split(":", 1)[1]
        return send(chat_id, "确定删除此任务及 VPS 中已下载文件吗？这个操作无法恢复。", [[{"text": "确认删除", "callback_data": f"deleteyes:{short_hash}"}, {"text": "取消", "callback_data": f"task:{short_hash}"}]])
    if data.startswith("deleteyes:"):
        item = find_task(data.split(":", 1)[1])
        if not item:
            return send(chat_id, "任务不存在。", home_keyboard())
        qbit("/api/v2/torrents/delete", {"hashes": item["hash"], "deleteFiles": "true"})
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
        return add_magnet(chat_id, OWNER, text)
    if text in {"/start", "/menu"}:
        return home(chat_id, message["from"].get("first_name", ""))
    if text == "/help":
        return help_text(chat_id)
    if legacy_command(chat_id, text):
        return
    send(chat_id, "请点击 /start 打开菜单，或直接发送 magnet 链接。")


def watch_completed():
    global SEEN
    for item in task_list():
        if item.get("progress", 0) < 1 or item["hash"] in SEEN:
            continue
        SEEN.add(item["hash"])
        DONE_FILE.write_text(json.dumps(list(SEEN)))
        send(OWNER, f"✅ 下载完成\n\n{item.get('name', '')}\n分类：{item.get('category') or '未分类'}\n\nMoviePilot 将自动识别、整理并上传到 Google Drive。")


while True:
    try:
        updates = telegram("getUpdates", {"offset": OFFSET, "timeout": 25, "allowed_updates": json.dumps(["message", "callback_query"])})
        for update in updates.get("result", []):
            handle(update)
        watch_completed()
    except Exception as exc:
        print("error:", exc, flush=True)
        time.sleep(5)
