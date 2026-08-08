import json
import shutil
import sqlite3
import subprocess
import threading
import time
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import __version__
from .clients import (
    Aria2Client,
    EmbyClient,
    MoviePilotClient,
    QasClient,
    QBitClient,
    TelegramClient,
)
from .categories import (
    CANONICAL_CATEGORIES,
    LEGACY_QBIT_CATEGORIES,
    download_path,
    load_moviepilot_categories,
    qbit_category_paths,
)
from .cleanup import is_brush_task, safe_to_cleanup, selected_video_sizes
from .library import missing_plan
from .media import (
    VIDEO_EXTENSIONS,
    clean_title,
    episode_key as domain_episode_key,
    episode_keys as domain_episode_keys,
    parse_identity_label,
    season_number,
)
from .resolver import MediaResolver, ResolutionError
from .retry import cloud_block_status
from .storage import IdentityStore, JsonStore
from .transfer_verification import (
    load_successful_transfer_proofs,
    verified_transfer_sources,
)
from .config import Settings, explicit_web_port
from .parsing import (
    extract_magnet,
    extract_post_title,
    extract_quark_share,
    message_text_with_links,
)

SETTINGS = Settings.from_env()
TOKEN = SETTINGS.bot_token
OWNER = SETTINGS.owner_id
QBIT_URL = SETTINGS.qbit_url
QBIT_USER = SETTINGS.qbit_username
QBIT_PASSWORD = SETTINGS.qbit_password
OLLAMA_URL = SETTINGS.ollama_url
OLLAMA_MODEL = SETTINGS.ollama_model
QAS_URL = SETTINGS.qas_url
QAS_USER = SETTINGS.qas_username
QAS_PASSWORD = SETTINGS.qas_password
QUARK_SAVE_PATH = SETTINGS.quark_save_path
ARIA2_URL = SETTINGS.aria2_url
ARIA2_SECRET = SETTINGS.aria2_secret
MOVIEPILOT_URL = SETTINGS.moviepilot_url
MOVIEPILOT_TOKEN = SETTINGS.moviepilot_token
QBIT_SAVE_PATH = SETTINGS.qbit_save_path
OFFSET = 0
PENDING = {}

DATA_DIR = SETTINGS.data_dir
DATA_DIR.mkdir(exist_ok=True)
DONE_FILE = DATA_DIR / "done.json"
DONE_STORE = JsonStore(DONE_FILE, [])
SEEN = set(DONE_STORE.load())
QUEUE_FILE = DATA_DIR / "queue.json"
QUEUE_STORE = JsonStore(QUEUE_FILE, [])
QUEUE = list(QUEUE_STORE.load())
QUEUE_READY = QUEUE_FILE.exists()
CLOUD_UPLOAD_BLOCK_FILE = DATA_DIR / "cloud-upload-block.json"
BLOCKED_FILE = DATA_DIR / "blocked.json"
BLOCKED_STORE = JsonStore(BLOCKED_FILE, [])
BLOCKED = set(BLOCKED_STORE.load())
RESERVE_GIB = SETTINGS.min_free_gib
MAX_ACTIVE_DOWNLOADS = SETTINGS.max_active_downloads
GIB = 1024 * 1024 * 1024
EXPIRY_FILE = DATA_DIR / "expiry.json"
EXPIRY_STORE = JsonStore(EXPIRY_FILE, [])
EXPIRING = list(EXPIRY_STORE.load())
INCOMING_DIR = DATA_DIR / "incoming"
INCOMING_DIR.mkdir(exist_ok=True)
MAX_TORRENT_BYTES = 20 * 1024 * 1024
QUARK_QUEUE_FILE = DATA_DIR / "quark_queue.json"
QUARK_QUEUE_STORE = JsonStore(QUARK_QUEUE_FILE, [])
QUARK_QUEUE = list(QUARK_QUEUE_STORE.load())
QUARK_PENDING_FILE = DATA_DIR / "quark_pending.json"
QUARK_PENDING_STORE = JsonStore(QUARK_PENDING_FILE, {})
QUARK_PENDING = QUARK_PENDING_STORE.load()
QUARK_TITLE_PENDING_FILE = DATA_DIR / "quark_title_pending.json"
QUARK_TITLE_PENDING_STORE = JsonStore(QUARK_TITLE_PENDING_FILE, {})
QUARK_TITLE_PENDING = QUARK_TITLE_PENDING_STORE.load()
QUARK_CONFIRM_PENDING_FILE = DATA_DIR / "quark_confirm_pending.json"
QUARK_CONFIRM_PENDING_STORE = JsonStore(QUARK_CONFIRM_PENDING_FILE, {})
QUARK_CONFIRM_PENDING = QUARK_CONFIRM_PENDING_STORE.load()
QUARK_ACTIVE = False
QUARK_LOCK = threading.Lock()
ARIA2_FILE = DATA_DIR / "aria2.json"
ARIA2_STORE = JsonStore(ARIA2_FILE, {})
ARIA2_TRACKED = ARIA2_STORE.load()
ACCOUNT_PENDING_FILE = DATA_DIR / "account_pending.json"
ACCOUNT_PENDING_STORE = JsonStore(ACCOUNT_PENDING_FILE, {})
ACCOUNT_PENDING = ACCOUNT_PENDING_STORE.load()
HERMES_INBOX_FILE = DATA_DIR / "hermes_inbox.json"
HERMES_INBOX_LOCK = threading.Lock()
IDENTITIES = IdentityStore(DATA_DIR / "identities-v2.json")
RESOLVER = MediaResolver(
    MoviePilotClient(MOVIEPILOT_URL, MOVIEPILOT_TOKEN),
    IDENTITIES,
)

CATEGORIES = list(CANONICAL_CATEGORIES)
TELEGRAM_CLIENT = TelegramClient(TOKEN)
ARIA2_CLIENT = Aria2Client(ARIA2_URL, ARIA2_SECRET)
QAS_CLIENT = QasClient(QAS_URL, QAS_USER, QAS_PASSWORD)
QBIT_CLIENT = QBitClient(QBIT_URL, QBIT_USER, QBIT_PASSWORD)
EMBY_CLIENT = EmbyClient(SETTINGS.emby_url, SETTINGS.emby_api_key)
LAST_QBIT_CLEANUP = 0.0
LAST_CATEGORY_SYNC = 0.0
CATEGORY_SYNC_INTERVAL = 6 * 60 * 60
CATEGORY_SYNC_RETRY_INTERVAL = 5 * 60


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


def raw_request(url, body=None, headers=None, timeout=40):
    """Send binary bodies, used for qBittorrent torrent-file uploads."""
    try:
        req = urllib.request.Request(url, body, headers or {})
        response = urllib.request.urlopen(req, timeout=timeout)
        return response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers
    except Exception as exc:
        return 599, str(exc).encode(), {}


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


def save_quark_queue():
    QUARK_QUEUE_STORE.save(QUARK_QUEUE)


def save_quark_pending():
    QUARK_PENDING_STORE.save(QUARK_PENDING)


def save_quark_title_pending():
    QUARK_TITLE_PENDING_STORE.save(QUARK_TITLE_PENDING)


def save_quark_confirm_pending():
    QUARK_CONFIRM_PENDING_STORE.save(QUARK_CONFIRM_PENDING)


def save_aria2_tracked():
    ARIA2_STORE.save(ARIA2_TRACKED)


def save_account_pending():
    ACCOUNT_PENDING_STORE.save(ACCOUNT_PENDING)


def hermes_jobs():
    """Read the local Hermes inbox. It never contains credentials."""
    try:
        return json.loads(HERMES_INBOX_FILE.read_text()) if HERMES_INBOX_FILE.exists() else []
    except (OSError, json.JSONDecodeError):
        return []


def save_hermes_jobs(jobs):
    temporary = HERMES_INBOX_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(jobs, ensure_ascii=False, indent=2))
    temporary.replace(HERMES_INBOX_FILE)


def aria2_rpc(method, params=None):
    return ARIA2_CLIENT.call(method, list(params or []))


def aria2_recent():
    return ARIA2_CLIENT.recent()


def aria2_name(item):
    files = item.get("files") or []
    if files:
        return Path(files[0].get("path") or "Aria2 文件").name
    return "Aria2 文件"


def aria2_percent(item):
    total = int(item.get("totalLength") or 0)
    done = int(item.get("completedLength") or 0)
    return (done / total * 100) if total else 0


def qas_open(path, payload=None, timeout=45):
    return QAS_CLIENT.request(path, payload, timeout)


def media_folder_name(title):
    """Compatibility wrapper around the domain title normalizer."""
    try:
        return clean_title(title)[:96]
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def safe_confirmed_media_title(title):
    return parse_identity_label(title) is not None


def moviepilot_media_title(title):
    """Resolve to a typed, season-aware canonical task label."""
    try:
        return RESOLVER.automatic(title).task_label
    except (ResolutionError, ValueError, RuntimeError) as exc:
        raise RuntimeError(str(exc)) from exc


def moviepilot_tmdb_title(tmdb_id, preferred_title=None, forced_type=None):
    source = preferred_title or f"TMDB {tmdb_id}"
    try:
        return RESOLVER.manual(
            source,
            str(tmdb_id),
            forced_type=forced_type,
            season=season_number(source),
        ).task_label
    except (ResolutionError, ValueError, RuntimeError) as exc:
        raise RuntimeError(str(exc)) from exc


def resolve_pending_media_text(text, pending):
    source = pending.get("candidate") or pending.get("folder") or text
    try:
        return RESOLVER.reply(source, text).task_label
    except (ResolutionError, ValueError, RuntimeError) as exc:
        raise RuntimeError(str(exc)) from exc


def folder_media_title(folder_name):
    """Return the original folder name only when it actually contains a title.

    Generic names such as ``Season 1 (HQ.DV.60fps)`` have no identity for
    MoviePilot to search, while ``悬案 Season 1 (HQ...)`` does.
    """
    probe = folder_name
    probe = re.sub(r"(?i)\b(?:season|s)\s*\d{1,3}\b", " ", probe)
    probe = re.sub(r"第\s*\d+\s*[季集]|第?[一二三四五六七八九十]+季", " ", probe)
    probe = re.sub(r"(?i)\b(?:2160p|1080p|720p|4k|uhd|web[ ._-]*dl|bluray|remux|h\.?265|hevc|h\.?264|x265|x264|hdr|dv|dovi|60fps|50fps|10bit|8bit|aac|ddp|dts|atmos|hq|sdr)\b", " ", probe)
    probe = re.sub(r"[\[\]()._\-\s]+", "", probe)
    # At least a Chinese character or a three-letter word must remain. This
    # rejects technical-only folder names without guessing a media title.
    if re.search(r"[\u4e00-\u9fff]|[A-Za-z]{3,}", probe):
        return media_folder_name(folder_name)
    return None


def request_quark_title(chat_id, share_url, folder_name, reason=None, candidate=None):
    candidate = media_folder_name(candidate) if candidate else None
    QUARK_TITLE_PENDING[str(chat_id)] = {
        "url": share_url,
        "folder": folder_name,
        "candidate": candidate,
        "created_at": time.time(),
    }
    save_quark_title_pending()
    reason_line = f"\n原因：{reason}\n" if reason else ""
    buttons = None
    if candidate and safe_confirmed_media_title(candidate):
        buttons = [
            [{"text": f"使用“{candidate[:28]}”下载", "callback_data": "quarkusecandidate"}],
            [{"text": "取消", "callback_data": "quarkcancelcandidate"}],
        ]
    return send(
        chat_id,
        f"⚠️ 暂未开始下载\n\n已选择：{folder_name}{reason_line}\n"
        "请回复 TMDB 编号，例如：85937\n"
        "也可以回复正确剧名，年份可选，例如：光阴之外\n\n"
        "输入 /cancel 可取消。",
        buttons,
    )


def confirm_quark_download(chat_id, share_url, media_title):
    """Require one explicit identity confirmation before a large cloud download."""
    if not safe_confirmed_media_title(media_title):
        return request_quark_title(
            chat_id,
            share_url,
            media_title,
            "识别结果像占位符，已禁止下载",
        )
    key = uuid.uuid4().hex[:10]
    QUARK_CONFIRM_PENDING[key] = {
        "url": share_url,
        "media_title": media_title,
        "created_at": time.time(),
    }
    save_quark_confirm_pending()
    identity = parse_identity_label(media_title)
    if identity:
        identity_text = (
            f"名称：{identity.title}\n"
            f"年份：{identity.year or '—'}\n"
            f"TMDB：{identity.tmdb_id}\n"
            f"季数：第 {identity.season} 季"
        )
    else:
        identity_text = media_title
    return send(
        chat_id,
        f"🎬 已确认媒体身份\n\n{identity_text}\n\n"
        "确认无误后才会创建下载任务；系统将跳过 Google Drive 中已有的集数。",
        [
            [{"text": "确认下载", "callback_data": f"quarkconfirm:{key}"}],
            [
                {"text": "修改剧名", "callback_data": f"quarkedit:{key}"},
                {"text": "取消", "callback_data": f"quarkcancel:{key}"},
            ],
        ],
    )


def qas_task(share_url, task_name, media_title=None, pattern=""):
    # QAS's Aria2 plugin flattens the source tree when save_path is set. Put a
    # title in the destination path so MoviePilot sees e.g. "剧名 (年份)/S01E02".
    aria_save_path = "incoming"
    if media_title:
        identity = parse_identity_label(media_title)
        if not identity:
            raise RuntimeError("媒体身份格式异常，未提交 QAS")
        # Keep the season in the first path component. The Aria2 completion
        # hook uses this component when it prefixes bare files such as 01.mkv.
        aria_save_path = f"incoming/{identity.task_label}"
    return {
        "taskname": task_name,
        "shareurl": share_url,
        # Each request gets its own Quark temporary folder. That prevents a
        # repeat share from being mistaken for an already-processed task.
        "savepath": f"{QUARK_SAVE_PATH}/{task_name}",
        "pattern": pattern,
        "replace": "",
        "addition": {
            "aria2": {
                "auto_download": True,
                "download_subdir": True,
                "save_path": aria_save_path,
                "pause": False,
            }
        },
    }


def qas_share_folders(share_url, stoken=None):
    """Return the first-level folders of a shared Quark link."""
    payload = {"shareurl": share_url}
    if stoken:
        payload["stoken"] = stoken
    response = qas_open("/get_share_detail", payload)
    payload = response.json()
    if not payload.get("success"):
        error = payload.get("data", {}).get("error") or payload.get("message") or "夸克链接解析失败"
        raise RuntimeError(error)
    entries = payload.get("data", {}).get("list", [])
    folders = [
        {"fid": item["fid"], "name": item["file_name"]}
        for item in entries
        if item.get("dir") and item.get("fid") and item.get("file_name")
    ]
    return folders, payload.get("data", {}).get("stoken")


def episode_key(text, default_season=None):
    return domain_episode_key(text, default_season)


def episode_keys(text, default_season=None):
    return domain_episode_keys(text, default_season)


def qas_share_video_files(share_url):
    """Return direct video file names in the chosen Quark directory."""
    response = qas_open("/get_share_detail", {"shareurl": share_url})
    payload = response.json()
    if not payload.get("success"):
        error = payload.get("data", {}).get("error") or payload.get("message") or "夸克目录读取失败"
        raise RuntimeError(error)
    return [
        item["file_name"]
        for item in payload.get("data", {}).get("list", [])
        if not item.get("dir")
        and item.get("file_name", "").lower().endswith(VIDEO_EXTENSIONS)
    ]


def moviepilot_existing(media_title):
    """Return successful MoviePilot transfer paths for one canonical title."""
    database = SETTINGS.moviepilot_db
    if not database.is_file():
        raise RuntimeError("MoviePilot 整理历史不可读取，已停止提交下载以防重复")
    identity = parse_identity_label(media_title)
    if not identity:
        raise RuntimeError("媒体身份格式异常，未检查 MoviePilot 历史")
    title = identity.title
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        rows = connection.execute(
            "SELECT title, episodes, dest, files FROM transferhistory "
            "WHERE status = 1 AND (title LIKE ? OR dest LIKE ?)",
            (f"%{title}%", f"%{title}%"),
        ).fetchall()
        connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError(f"MoviePilot 整理历史读取失败，未提交下载：{exc}")
    paths = []
    for row in rows:
        for value in row:
            if value:
                paths.append(str(value))
    return paths, bool(rows)


def google_drive_existing(media_title):
    """Read the real Google Drive library instead of trusting history alone."""
    identity = parse_identity_label(media_title)
    if not identity:
        raise RuntimeError("媒体身份格式异常，未检查 Google Drive")
    try:
        result = subprocess.run(
            [
                "rclone",
                "--config",
                str(SETTINGS.rclone_config),
                "lsf",
                SETTINGS.drive_remote,
                "--recursive",
                "--files-only",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Google Drive 媒体库读取失败，未提交下载：{exc}")
    if result.returncode != 0:
        raise RuntimeError(f"Google Drive 媒体库读取失败，未提交下载：{result.stderr.strip()[:120]}")
    matched = []
    for path in result.stdout.splitlines():
        components = path.split("/")
        if any(
            component == identity.title
            or component.startswith(f"{identity.title} (")
            for component in components
        ):
            matched.append(path)
    return matched, bool(matched)


def quark_missing_plan(share_url, media_title):
    """Build a QAS include regex using the actual Drive library as truth."""
    files = qas_share_video_files(share_url)
    if not files:
        return "", 0, 0
    identity = parse_identity_label(media_title)
    if not identity:
        raise RuntimeError("媒体身份格式异常，未进行缺集判断")
    history_paths, _ = moviepilot_existing(media_title)
    drive_paths, _ = google_drive_existing(media_title)
    plan = missing_plan(files, [*history_paths, *drive_paths], identity)
    if plan.complete:
        return None, plan.total, plan.skipped
    return plan.include_regex, plan.total, plan.skipped


def qas_download_choices(share_url, max_depth=5):
    """Walk harmless wrapper folders until a real choice is reached.

    A common Quark layout is: share root -> "Drama name" ->
    "Season 1 (HQ)" / "Season 1 (SDR)". The former is a wrapper, not a
    download choice. Keep its title as a hint for MoviePilot.
    """
    current_url = share_url
    title_hint = None
    stoken = None
    for _ in range(max_depth):
        folders, stoken = qas_share_folders(current_url, stoken)
        if len(folders) != 1:
            return current_url, folders, title_hint
        wrapper = folders[0]
        inferred = folder_media_title(wrapper["name"])
        if inferred:
            title_hint = inferred
        current_url = selected_share_url(current_url, wrapper)
    # Never choose an arbitrary nested folder if the hierarchy is unusually
    # deep; let the user see the final folder instead.
    folders, _ = qas_share_folders(current_url, stoken)
    return current_url, folders, title_hint


def selected_share_url(share_url, folder):
    """QAS supports a share URL with a folder fid in its fragment."""
    base = share_url.split("#", 1)[0]
    name = urllib.parse.quote(folder["name"], safe="")
    return f"{base}#/list/share/{folder['fid']}-{name}"


def enqueue_quark_task(chat_id, share_url, media_title=None):
    pattern = ""
    total = skipped = 0
    if media_title:
        pattern, total, skipped = quark_missing_plan(share_url, media_title)
        if pattern is None:
            return send_temporary(
                chat_id,
                f"✅ 已跳过下载\n\n{media_title} 共 {total} 个视频文件，均已由 MoviePilot 整理并上传。",
                lifetime_seconds=15,
            )
    task = {
        "url": share_url,
        "name": f"夸克任务-{time.strftime('%m%d-%H%M%S')}",
        "media_title": media_title,
        "pattern": pattern,
    }
    with QUARK_LOCK:
        QUARK_QUEUE.append(task)
        save_quark_queue()
        position = len(QUARK_QUEUE) + (1 if QUARK_ACTIVE else 0)
    skipped_line = f"\n已跳过已入库：{skipped} 个" if skipped else ""
    send_temporary(chat_id, f"☁️ 夸克链接已进入队列\n\n队列位置：{position}{skipped_line}\n夸克转存 → Aria2 → MoviePilot → Google Drive", lifetime_seconds=12)
    run_quark_queue()


def run_quark_queue():
    """Run one QAS transfer at a time; QAS then dispatches files to Aria2."""
    global QUARK_ACTIVE
    with QUARK_LOCK:
        if (
            QUARK_ACTIVE
            or not QUARK_QUEUE
            or cloud_block_status(CLOUD_UPLOAD_BLOCK_FILE).get("active")
        ):
            return
        task = QUARK_QUEUE.pop(0)
        save_quark_queue()
        QUARK_ACTIVE = True

    def worker():
        global QUARK_ACTIVE
        try:
            before = {item.get("gid") for item in aria2_recent()}
            response = qas_open("/run_script_now", {"tasklist": [qas_task(task["url"], task["name"], task.get("media_title"), task.get("pattern", ""))]})
            output = response.text
            if "❌" in output or "任务执行失败" in output:
                raise RuntimeError("QAS 未能完成转存，请检查夸克链接或 QAS 日志")
            added = [item for item in aria2_recent() if item.get("gid") not in before]
            for item in added:
                ARIA2_TRACKED[item["gid"]] = {"name": aria2_name(item), "notified": False}
            save_aria2_tracked()
            title_line = f"\n媒体：{task['media_title']}" if task.get("media_title") else ""
            send_temporary(OWNER, f"☁️ 已转存并交给 Aria2 下载\n\n{task['name']}{title_line}\n\nMoviePilot 会在完成后自动整理并上传 Google Drive。", lifetime_seconds=12)
        except Exception as exc:
            print("quark-queue error:", exc, flush=True)
            send(OWNER, f"⚠️ 夸克任务失败\n\n{task['name']}\n{str(exc)[:160]}")
        finally:
            with QUARK_LOCK:
                QUARK_ACTIVE = False
            run_quark_queue()

    threading.Thread(target=worker, daemon=True).start()


def add_quark_share(chat_id, share_url, source_message_id, post_title=None):
    try:
        telegram("deleteMessage", {"chat_id": chat_id, "message_id": source_message_id})
    except Exception as exc:
        print("delete-quark-source error:", exc, flush=True)
    def worker():
        try:
            base_url, folders, folder_title_hint = qas_download_choices(share_url)
            # The post title is explicit user-provided metadata and always wins
            # over a folder name, which can be malformed or release-only text.
            raw_title = post_title or folder_title_hint
            # Never begin a cloud transfer from an unverified release name.
            # MoviePilot provides the canonical Chinese title/year/TMDB ID.
            title_hint = None
            title_error = None
            if raw_title:
                try:
                    title_hint = moviepilot_media_title(raw_title)
                except RuntimeError as exc:
                    title_error = str(exc)
            if len(folders) <= 1:
                if not folders:
                    if title_hint:
                        return confirm_quark_download(chat_id, base_url, title_hint)
                    return request_quark_title(
                        chat_id,
                        base_url,
                        "分享根目录",
                        title_error,
                        raw_title,
                    )
                folder = folders[0]
                selected_url = selected_share_url(base_url, folder)
                # A title extracted from the forwarded post is explicit
                # metadata; never replace it with a malformed Quark folder.
                title = title_hint
                if not title:
                    folder_title = folder_media_title(folder["name"])
                    if folder_title:
                        try:
                            title = moviepilot_media_title(folder_title)
                        except RuntimeError as exc:
                            title_error = str(exc)
                if title:
                    return confirm_quark_download(chat_id, selected_url, title)
                return request_quark_title(
                    chat_id,
                    selected_url,
                    folder["name"],
                    title_error,
                    raw_title or folder_media_title(folder["name"]),
                )
            key = uuid.uuid4().hex[:10]
            QUARK_PENDING[key] = {
                "url": base_url,
                "folders": folders,
                "title_hint": title_hint,
                "title_error": title_error,
                "raw_title": raw_title,
                "created_at": time.time(),
            }
            save_quark_pending()
            buttons = [
                [{"text": f"📁 {folder['name'][:42]}", "callback_data": f"quarkselect:{key}:{index}"}]
                for index, folder in enumerate(folders)
            ]
            return send(chat_id, "发现多个文件夹，请选择要下载的版本：", buttons)
        except Exception as exc:
            print("quark-share-parse error:", exc, flush=True)
            return send(chat_id, f"⚠️ 夸克链接解析失败\n\n{str(exc)[:160]}", home_keyboard())

    send_temporary(chat_id, "☁️ 正在解析夸克分享内容…", lifetime_seconds=12)
    threading.Thread(target=worker, daemon=True).start()


def process_hermes_inbox():
    """Execute local Hermes jobs without sending messages from this bot.

    A job can pause at ``needs_folder`` or ``needs_title``.  Hermes reads that
    state and asks the user in its own Telegram chat, then updates the same job
    with ``folder_index`` or ``media_title`` for the next pass.
    """
    with HERMES_INBOX_LOCK:
        jobs = hermes_jobs()
        changed = False
        for job in jobs:
            state = job.get("state", "pending")
            kind = job.get("kind")
            title = job.get("media_title")
            try:
                if state == "pending" and kind == "magnet":
                    add_magnet(0, OWNER, job["url"], 0, title)
                    job.update({"state": "submitted", "updated_at": time.time()})
                    changed = True
                elif kind == "quark" and state in {"pending", "needs_folder", "needs_title"}:
                    if state == "needs_title" and not title:
                        continue
                    base_url, folders, folder_title_hint = qas_download_choices(job["url"])
                    raw_title = title or folder_title_hint
                    title = moviepilot_media_title(raw_title) if raw_title else None
                    selected_index = job.get("folder_index")
                    if len(folders) > 1 and selected_index is None:
                        job.update({
                            "state": "needs_folder",
                            "options": [folder["name"] for folder in folders],
                            "updated_at": time.time(),
                        })
                        changed = True
                        continue
                    if folders:
                        if selected_index is None:
                            selected_index = 0
                        selected_index = int(selected_index)
                        if selected_index < 0 or selected_index >= len(folders):
                            raise RuntimeError("文件夹选择无效")
                        folder = folders[selected_index]
                        selected_url = selected_share_url(base_url, folder)
                        if not title:
                            folder_title = folder_media_title(folder["name"])
                            title = moviepilot_media_title(folder_title) if folder_title else None
                    else:
                        selected_url = base_url
                    if not title:
                        job.update({"state": "needs_title", "updated_at": time.time()})
                        changed = True
                        continue
                    enqueue_quark_task(0, selected_url, title)
                    job.update({"state": "submitted", "media_title": title, "updated_at": time.time()})
                    changed = True
            except Exception as exc:
                job.update({"state": "error", "error": str(exc)[:180], "updated_at": time.time()})
                changed = True
        if changed:
            save_hermes_jobs(jobs)


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


def qbit(path, data=None):
    return QBIT_CLIENT.request(path, form=data).text


def qbit_action(action, hashes, delete_files=False):
    return QBIT_CLIENT.action(action, hashes, delete_files)


def qbit_add_torrent_file(filename, content, category):
    return QBIT_CLIENT.add_torrent(
        filename,
        content,
        category,
        download_path(category, QBIT_SAVE_PATH, CATEGORIES),
    )


def synchronize_media_categories():
    """Keep the bot and qBittorrent aligned with MoviePilot's nine categories."""

    global LAST_CATEGORY_SYNC
    categories = load_moviepilot_categories(
        SETTINGS.moviepilot_category_file,
        required=True,
    )
    result = QBIT_CLIENT.sync_categories(
        qbit_category_paths(categories, QBIT_SAVE_PATH),
        set(LEGACY_QBIT_CATEGORIES),
    )
    CATEGORIES[:] = categories
    LAST_CATEGORY_SYNC = time.monotonic()
    print(
        "MEDIA_CATEGORIES_READY "
        f"created={len(result['created'])} "
        f"updated={len(result['updated'])} "
        f"removed={len(result['removed'])} "
        f"kept={len(result['kept'])}",
        flush=True,
    )
    return result


def qbit_files(torrent_hash):
    return QBIT_CLIENT.files(torrent_hash)


def wait_qbit_files(torrent_hash, attempts=40):
    """Wait only for torrent metadata; never start payload downloading here."""
    last_error = None
    for _ in range(attempts):
        try:
            files = qbit_files(torrent_hash)
            if files:
                return files
        except Exception as exc:
            last_error = exc
        time.sleep(1)
    raise RuntimeError(f"种子元数据未在 {attempts} 秒内就绪：{last_error or '未返回文件列表'}")


def apply_moviepilot_dedupe(torrent_hash, media_title):
    """Disable only files already present in the exact title and season."""
    files = wait_qbit_files(torrent_hash)
    videos = [
        item for item in files
        if str(item.get("name", "")).lower().endswith(VIDEO_EXTENSIONS)
    ]
    if not videos:
        return {"total": 0, "skipped": 0, "remaining": 0}
    if not media_title:
        return {"total": len(videos), "skipped": 0, "remaining": len(videos)}
    identity = parse_identity_label(media_title)
    if not identity:
        return {"total": len(videos), "skipped": 0, "remaining": len(videos)}
    history_paths, _ = moviepilot_existing(media_title)
    drive_paths, _ = google_drive_existing(media_title)
    source_names = [str(item.get("name", "")) for item in videos]
    plan = missing_plan(source_names, [*history_paths, *drive_paths], identity)
    missing_names = set(plan.missing_names)
    skip_ids = [
        str(item.get("index"))
        for item in videos
        if str(item.get("name", "")) not in missing_names
    ]

    if skip_ids:
        qbit(
            "/api/v2/torrents/filePrio",
            {"hash": torrent_hash, "id": "|".join(skip_ids), "priority": "0"},
        )
    return {
        "total": len(videos),
        "skipped": len(skip_ids),
        "remaining": plan.remaining,
    }


def telegram(method, data):
    return TELEGRAM_CLIENT.call(method, data)


def send(chat_id, text, keyboard=None):
    # Hermes submits jobs through a local file inbox.  Those jobs must not
    # produce a second Telegram conversation from IslandDownloadBot.
    if not chat_id:
        return {"result": {}}
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard}, ensure_ascii=False)
    return telegram("sendMessage", payload)


def send_temporary(chat_id, text, lifetime_seconds=300):
    if not chat_id:
        return
    response = send(chat_id, text)
    message_id = response.get("result", {}).get("message_id")
    if message_id:
        EXPIRING.append({"chat_id": chat_id, "message_id": message_id, "delete_at": time.time() + lifetime_seconds})
        EXPIRY_STORE.save(EXPIRING)


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
        EXPIRY_STORE.save(EXPIRING)


def answer(callback_id, text=None):
    data = {"callback_query_id": callback_id}
    if text:
        data["text"] = text
    telegram("answerCallbackQuery", data)


def home_keyboard():
    return [
        [{"text": "👤 开号", "callback_data": "account:create"}, {"text": "📋 我的任务", "callback_data": "home:tasks"}],
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
    send(chat_id, f"{greeting}\n\nIsland Download\n发送 magnet、.torrent 或夸克分享链接。\n系统会智能分类，最多同时下载 {MAX_ACTIVE_DOWNLOADS} 个任务。\n也可以直接发送中文问题给 AI 助手。", home_keyboard())


def task_list():
    return json.loads(qbit("/api/v2/torrents/info?tag=islandbot&sort=added_on&reverse=true"))


def save_queue():
    QUEUE_STORE.save(QUEUE)


def save_blocked():
    BLOCKED_STORE.save(list(BLOCKED))


def file_size(item):
    if "amount_left" in item:
        return int(item.get("amount_left") or 0)
    return int(item.get("total_size") or item.get("size") or 0)


def has_enough_space(item, free=None):
    """Return whether the next download fits, with a fixed safety reserve."""
    size = file_size(item)
    if size <= 0:
        # A magnet can need a short metadata phase before qBittorrent knows size.
        return True, size, free if free is not None else shutil.disk_usage(SETTINGS.downloads_dir).free
    free = free if free is not None else shutil.disk_usage(SETTINGS.downloads_dir).free
    return size <= max(0, free - RESERVE_GIB * GIB), size, free


def google_drive_capacity():
    """Read the configured Google Drive quota through MoviePilot's rclone remote."""
    try:
        result = subprocess.run(
            ["rclone", "--config", str(SETTINGS.rclone_config), "about", SETTINGS.drive_remote.split(":", 1)[0] + ":"],
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


def moviepilot_transfer_now():
    """Ask MoviePilot to scan completed external downloads immediately."""
    if not MOVIEPILOT_TOKEN:
        raise RuntimeError("MoviePilot API_TOKEN 尚未连接")
    query = urllib.parse.urlencode({"token": MOVIEPILOT_TOKEN})
    status, body, _ = request(f"{MOVIEPILOT_URL}/api/v1/transfer/now?{query}")
    if status >= 300:
        raise RuntimeError(f"MoviePilot 立即整理失败（HTTP {status}）：{body[:100]}")


def run_queue():
    """Keep up to MAX_ACTIVE_DOWNLOADS bot tasks downloading in queue order."""
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

    # A retained MoviePilot upload failure must be cleared before ordinary
    # downloads consume more disk. Brush tasks are always outside this gate.
    cloud_block = cloud_block_status(CLOUD_UPLOAD_BLOCK_FILE)
    if cloud_block.get("active"):
        for task_hash in QUEUE:
            item = task_by_hash.get(task_hash)
            if (
                item
                and not is_brush_task(item)
                and item.get("state")
                not in {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}
            ):
                qbit_action("pause", task_hash)
        return

    active_hashes = QUEUE[:MAX_ACTIVE_DOWNLOADS]
    # Every later item stays paused, even after a container restart.
    for task_hash in QUEUE[MAX_ACTIVE_DOWNLOADS:]:
        item = task_by_hash.get(task_hash)
        if item and item.get("state") not in {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}:
            qbit_action("pause", task_hash)
    free = shutil.disk_usage(SETTINGS.downloads_dir).free
    available = max(0, free - RESERVE_GIB * GIB)
    for task_hash in active_hashes:
        item = task_by_hash.get(task_hash)
        if not item:
            continue
        fits, size, _ = has_enough_space(item, available + RESERVE_GIB * GIB)
        if not fits:
            if item.get("state") not in {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}:
                qbit_action("pause", task_hash)
            if task_hash not in BLOCKED:
                BLOCKED.add(task_hash)
                save_blocked()
                send(OWNER, f"⚠️ 队列暂停：空间不足\n\n{item.get('name', '')[:55]}\n剩余需下载：{size / GIB:.1f} GB\n当前可用：{free / GIB:.1f} GB\n安全预留：{RESERVE_GIB} GB\n\n等待 MoviePilot 上传并清理文件后，将自动继续。")
            continue
        available = max(0, available - size)
        if task_hash in BLOCKED:
            BLOCKED.remove(task_hash)
            save_blocked()
        if item.get("state") in {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}:
            qbit_action("resume", task_hash)


def show_tasks(chat_id):
    tasks = task_list()
    try:
        aria_items = [item for item in aria2_recent() if item.get("gid") in ARIA2_TRACKED and item.get("status") in {"active", "waiting", "paused"}]
    except Exception as exc:
        print("aria2-task-list error:", exc, flush=True)
        aria_items = []
    active_qbit = [item for item in tasks if item.get("progress", 0) < 1]
    cutoff = time.time() - 24 * 60 * 60
    completed_qbit = [item for item in tasks if item.get("progress", 0) >= 1 and item.get("completion_on", 0) >= cutoff]
    completed_aria = [item for item in ARIA2_TRACKED.values() if item.get("notified")]
    completed_count = len(completed_qbit) + len(completed_aria)
    if not active_qbit and not QUARK_QUEUE and not QUARK_ACTIVE and not aria_items and not completed_count:
        return send(chat_id, "暂无机器人添加的任务。\n\n点击“添加下载”或直接发送 magnet 链接、.torrent 文件。", home_keyboard())
    lines = []
    buttons = []
    if QUARK_ACTIVE:
        lines.append("☁️ 夸克转存 · 正在处理\n完成后会自动交给 Aria2")
    for position, item in enumerate(QUARK_QUEUE, start=1):
        lines.append(f"☁️ 夸克队列 {position}\n{item.get('name', '夸克任务')}")
    for item in aria_items[:6]:
        buttons.append([{"text": f"⚡ {aria2_name(item)[:28]} · {aria2_percent(item):.0f}%", "callback_data": f"aria:{item['gid'][:8]}"}])
    for item in active_qbit[:6]:
        short_hash = item["hash"][:8]
        buttons.append([{"text": f"⬇️ {item.get('name', '')[:28]} · {item.get('progress', 0) * 100:.0f}%", "callback_data": f"task:{short_hash}"}])
    if completed_count:
        buttons.append([{"text": f"已完成 {completed_count} 项", "callback_data": "home:completed"}])
    buttons.append([{"text": "刷新", "callback_data": "home:tasks"}, {"text": "← 主菜单", "callback_data": "home:home"}])
    is_downloading = bool(active_qbit or QUARK_QUEUE or QUARK_ACTIVE or aria_items)
    send(chat_id, "📥 下载中" if is_downloading else "当前无下载任务", buttons)


def show_recent_completed(chat_id):
    cutoff = time.time() - 24 * 60 * 60
    items = [item for item in task_list() if item.get("progress", 0) >= 1 and item.get("completion_on", 0) >= cutoff]
    lines = ["✅ 最近 24 小时完成"]
    for item in sorted(items, key=lambda value: value.get("completion_on", 0), reverse=True)[:8]:
        lines.append(f"{item.get('name', '')[:52]}\n已完成")
    for item in list(ARIA2_TRACKED.values())[-8:]:
        if item.get("notified"):
            lines.append(f"{item.get('name', 'Aria2 文件')[:52]}\n已交给 MoviePilot")
    if len(lines) == 1:
        lines.append("暂无最近完成的任务。")
    send(chat_id, "\n\n".join(lines), [[{"text": "← 下载任务", "callback_data": "home:tasks"}]])


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
        f"分类：{item.get('category') or '智能分类（MoviePilot）'}\n"
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


def show_aria_task(chat_id, short_gid):
    matches = [item for item in aria2_recent() if item.get("gid", "").startswith(short_gid)]
    if len(matches) != 1:
        return send(chat_id, "Aria2 任务不存在或已被清理。", [[{"text": "← 下载任务", "callback_data": "home:tasks"}]])
    item = matches[0]
    total = int(item.get("totalLength") or 0) / GIB
    done = int(item.get("completedLength") or 0) / GIB
    speed = int(item.get("downloadSpeed") or 0) / 1024 / 1024
    text = (
        f"⚡ {aria2_name(item)}\n\n"
        f"进度：{aria2_percent(item):.1f}%\n"
        f"状态：{item.get('status', '未知')}\n"
        f"大小：{done:.2f} / {total:.2f} GB\n"
        f"速度：{speed:.2f} MiB/s"
    )
    status = item.get("status")
    if status == "active":
        control = {"text": "暂停", "callback_data": f"ariaaction:pause:{item['gid'][:8]}"}
    elif status in {"waiting", "paused"}:
        control = {"text": "继续", "callback_data": f"ariaaction:resume:{item['gid'][:8]}"}
    else:
        control = None
    keyboard = []
    if control:
        keyboard.append([control, {"text": "删除", "callback_data": f"ariadeleteask:{item['gid'][:8]}"}])
    keyboard.append([{"text": "← 下载任务", "callback_data": "home:tasks"}])
    send(chat_id, text, keyboard)


def find_aria_task(short_gid):
    matches = [item for item in aria2_recent() if item.get("gid", "").startswith(short_gid)]
    return matches[0] if len(matches) == 1 else None


def delete_aria_files(item):
    """Remove the task's local Aria2 files, including files moved to complete."""
    names = set()
    for entry in item.get("files") or []:
        source = entry.get("path") or ""
        if not source.startswith("/downloads/"):
            continue
        local = Path("/aria2-downloads") / source.removeprefix("/downloads/")
        names.add(local.name)
        local.unlink(missing_ok=True)
        Path(str(local) + ".aria2").unlink(missing_ok=True)

    # The completion hook moves successful downloads from incoming to complete.
    # Delete only files with the exact task filenames from that bot-owned folder.
    complete_dir = Path("/aria2-downloads/complete")
    if complete_dir.exists():
        for name in names:
            for local in complete_dir.rglob(name):
                if local.is_file():
                    local.unlink(missing_ok=True)
                    Path(str(local) + ".aria2").unlink(missing_ok=True)


def aria_action(chat_id, action, short_gid):
    item = find_aria_task(short_gid)
    if not item:
        return send(chat_id, "Aria2 任务不存在或已被清理。", home_keyboard())
    method = "aria2.forcePause" if action == "pause" else "aria2.unpause"
    aria2_rpc(method, [item["gid"]])
    return show_aria_task(chat_id, short_gid)


def aria_delete(chat_id, short_gid):
    item = find_aria_task(short_gid)
    if not item:
        return send(chat_id, "Aria2 任务不存在或已被清理。", home_keyboard())
    aria2_rpc("aria2.forceRemove", [item["gid"]])
    delete_aria_files(item)
    ARIA2_TRACKED.pop(item["gid"], None)
    save_aria2_tracked()
    return send(chat_id, "已删除 Aria2 任务及 VPS 上该任务的文件。", home_keyboard())


def server_status(chat_id):
    try:
        info = json.loads(qbit("/api/v2/transfer/info"))
        tasks = task_list()
        disk = shutil.disk_usage(SETTINGS.downloads_dir)
        aria_stats = aria2_rpc("aria2.getGlobalStat")
    except Exception as exc:
        print("server-status error:", exc, flush=True)
        return send(chat_id, "🖥 状态暂时无法读取。\n请稍后点击 /start 再试。", [[{"text": "← 主菜单", "callback_data": "home:home"}]])
    download = info.get("dl_info_speed", 0) / 1024 / 1024
    upload = info.get("up_info_speed", 0) / 1024 / 1024
    active = sum(1 for item in tasks if item.get("progress", 0) < 1)
    aria_active = int(aria_stats.get("numActive") or 0)
    aria_speed = int(aria_stats.get("downloadSpeed") or 0) / 1024 / 1024
    used = disk.total - disk.free
    drive_capacity = google_drive_capacity()
    text = (
        "🖥 状态与设置\n\n"
        "服务：在线\n"
        "下载器：qBittorrent 已连接\n"
        f"下载速度：{download:.2f} MiB/s\n"
        f"上传速度：{upload:.2f} MiB/s\n"
        f"活动任务：{active}\n"
        f"夸克转存队列：{len(QUARK_QUEUE) + (1 if QUARK_ACTIVE else 0)}\n\n"
        f"Aria2：{aria_active} 个活动任务 · {aria_speed:.2f} MiB/s\n\n"
        f"VPS 下载盘：总 {disk.total / GIB:.1f} GB · 已用 {used / GIB:.1f} GB · 可用 {disk.free / GIB:.1f} GB\n"
        f"安全预留：{RESERVE_GIB} GB\n\n"
        f"Google Drive：{drive_capacity}\n\n"
        f"账号：管理员（{OWNER}）\n"
        "权限：下载、任务管理、服务器状态"
    )
    send(chat_id, text, [[{"text": "刷新", "callback_data": "home:server"}, {"text": "← 主菜单", "callback_data": "home:home"}]])


def add_magnet(chat_id, user_id, magnet, source_message_id, media_title=None):
    old = PENDING.get(user_id)
    if old and old.get("torrent_path"):
        Path(old["torrent_path"]).unlink(missing_ok=True)
    PENDING[user_id] = {
        "magnet": magnet,
        "source_message_id": source_message_id,
        "media_title": media_title,
    }
    return add_to_qbit(chat_id, user_id, "__auto__")


def download_telegram_file(file_id):
    details = telegram("getFile", {"file_id": file_id}).get("result", {})
    file_path = details.get("file_path")
    if not file_path:
        raise RuntimeError("Telegram 未返回文件路径")
    status, content, _ = raw_request(f"https://api.telegram.org/file/bot{TOKEN}/{file_path}", timeout=60)
    if status >= 300:
        raise RuntimeError("Telegram 文件下载失败")
    return content


def add_torrent_file(chat_id, user_id, document, source_message_id, media_title=None):
    filename = document.get("file_name") or "download.torrent"
    size = int(document.get("file_size") or 0)
    if not filename.lower().endswith(".torrent"):
        return send(chat_id, "只支持 .torrent 种子文件。请发送磁力链接或种子文件。", home_keyboard())
    if size <= 0 or size > MAX_TORRENT_BYTES:
        return send(chat_id, "种子文件无效或超过 20MB，无法从 Telegram 获取。", home_keyboard())
    try:
        content = download_telegram_file(document["file_id"])
    except Exception as exc:
        print("telegram-file error:", exc, flush=True)
        return send(chat_id, "种子文件读取失败，请重新发送。", home_keyboard())
    if not content.startswith(b"d"):
        return send(chat_id, "这个文件不是有效的 BitTorrent 种子文件。", home_keyboard())
    old = PENDING.get(user_id)
    if old and old.get("torrent_path"):
        Path(old["torrent_path"]).unlink(missing_ok=True)
    stored = INCOMING_DIR / f"{user_id}-{uuid.uuid4().hex}.torrent"
    stored.write_bytes(content)
    PENDING[user_id] = {
        "torrent_path": str(stored),
        "filename": filename,
        "source_message_id": source_message_id,
        "media_title": media_title,
    }
    return add_to_qbit(chat_id, user_id, "__auto__")


def add_to_qbit(chat_id, user_id, category):
    pending = PENDING.get(user_id)
    if not pending:
        return send(chat_id, "这个下载请求已失效，请重新发送 magnet 链接或 .torrent 文件。", home_keyboard())
    before = {item["hash"] for item in task_list()}
    if "magnet" in pending:
        add_data = {
            "urls": pending["magnet"],
            "tags": "islandbot",
            "autoTMM": "false",
            "stopCondition": "MetadataReceived",
            "savepath": download_path(category, QBIT_SAVE_PATH, CATEGORIES),
        }
        if category != "__auto__":
            add_data["category"] = category
        qbit("/api/v2/torrents/add", add_data)
    else:
        source = Path(pending["torrent_path"])
        if not source.exists():
            return send(chat_id, "种子文件已失效，请重新发送。", home_keyboard())
        qbit_add_torrent_file(pending["filename"], source.read_bytes(), category)
    # qBittorrent needs a moment to register the new torrent.  It will stop at
    # metadata, so no media payload is downloaded before we set file priority.
    time.sleep(1)
    added = [item for item in task_list() if item["hash"] not in before]
    if not added:
        return send(chat_id, "未能确认新任务，请在“我的任务”中检查。", home_keyboard())
    new_task = max(added, key=lambda item: item.get("added_on", 0))
    try:
        plan = apply_moviepilot_dedupe(new_task["hash"], pending.get("media_title"))
    except Exception as exc:
        # Keep it stopped. Starting an unplanned task could re-download a
        # whole season, which is worse than asking the user to retry.
        qbit_action("pause", new_task["hash"])
        return send(chat_id, f"⚠️ 未提交下载\n\n无法核对 MoviePilot 已入库记录：{str(exc)[:150]}\n任务已停止，不会下载。")

    PENDING.pop(user_id, None)
    if pending.get("torrent_path"):
        Path(pending["torrent_path"]).unlink(missing_ok=True)

    if plan["remaining"] == 0 and plan["total"]:
        qbit("/api/v2/torrents/delete", {"hashes": new_task["hash"], "deleteFiles": "true"})
        label = pending.get("media_title") or new_task.get("name", "该资源")
        return send_temporary(
            chat_id,
            f"✅ 已跳过下载\n\n{label}\n共 {plan['total']} 个视频文件，均已由 MoviePilot 整理并上传。",
            lifetime_seconds=15,
        )

    QUEUE.append(new_task["hash"])
    save_queue()
    run_queue()
    if plan["skipped"]:
        send_temporary(
            chat_id,
            f"✅ 已跳过已入库：{plan['skipped']} 个\n将下载缺少的 {plan['remaining']} 个视频文件。",
            lifetime_seconds=15,
        )
    # Keep private magnets and torrent files out of chat history once selected.
    try:
        telegram("deleteMessage", {"chat_id": chat_id, "message_id": pending["source_message_id"]})
    except Exception as exc:
        print("delete-source-message error:", exc, flush=True)


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
    qbit_action(command, item["hash"], delete_files=command == "delete")
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
    if data == "account:create":
        ACCOUNT_PENDING[str(chat_id)] = {"created_at": time.time()}
        save_account_pending()
        return send(
            chat_id,
            "请输入新账号的用户名。\n\n密码固定为 123456，仅有普通观看权限。\n输入 /cancel 可取消。",
            [[{"text": "取消", "callback_data": "account:cancel"}]],
        )
    if data == "account:cancel":
        ACCOUNT_PENDING.pop(str(chat_id), None)
        save_account_pending()
        return home(chat_id, callback["from"].get("first_name", ""))
    if data == "home:tasks":
        return show_tasks(chat_id)
    if data == "home:completed":
        return show_recent_completed(chat_id)
    if data == "home:server":
        return server_status(chat_id)
    if data == "quarkusecandidate":
        pending = QUARK_TITLE_PENDING.get(str(chat_id))
        if not pending or not pending.get("candidate"):
            return send(chat_id, "这个名称确认已过期，请重新发送分享链接。", home_keyboard())
        try:
            title = moviepilot_media_title(pending["candidate"])
        except RuntimeError as exc:
            return send(chat_id, str(exc))
        QUARK_TITLE_PENDING.pop(str(chat_id), None)
        save_quark_title_pending()
        return confirm_quark_download(chat_id, pending["url"], title)
    if data == "quarkcancelcandidate":
        QUARK_TITLE_PENDING.pop(str(chat_id), None)
        save_quark_title_pending()
        return send(chat_id, "已取消这次夸克下载。", home_keyboard())
    if data.startswith("quarkconfirm:"):
        key = data.split(":", 1)[1]
        pending = QUARK_CONFIRM_PENDING.get(key)
        if not pending:
            return send(chat_id, "这个确认已过期，请重新发送分享链接。", home_keyboard())
        if not safe_confirmed_media_title(pending["media_title"]):
            QUARK_CONFIRM_PENDING.pop(key, None)
            save_quark_confirm_pending()
            return request_quark_title(
                chat_id,
                pending["url"],
                pending["media_title"],
                "旧识别结果无效，已禁止下载",
            )
        try:
            result = enqueue_quark_task(
                chat_id,
                pending["url"],
                pending["media_title"],
            )
        except Exception as exc:
            print("quark-confirm error:", exc, flush=True)
            return send(
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
        QUARK_CONFIRM_PENDING.pop(key, None)
        save_quark_confirm_pending()
        return result
    if data.startswith("quarkedit:"):
        key = data.split(":", 1)[1]
        pending = QUARK_CONFIRM_PENDING.pop(key, None)
        save_quark_confirm_pending()
        if not pending:
            return send(chat_id, "这个确认已过期，请重新发送分享链接。", home_keyboard())
        return request_quark_title(chat_id, pending["url"], pending["media_title"], "请修正媒体身份")
    if data.startswith("quarkcancel:"):
        key = data.split(":", 1)[1]
        QUARK_CONFIRM_PENDING.pop(key, None)
        save_quark_confirm_pending()
        return send(chat_id, "已取消这次夸克下载。", home_keyboard())
    if data.startswith("quarkselect:"):
        _, key, index_text = data.split(":", 2)
        pending = QUARK_PENDING.pop(key, None)
        save_quark_pending()
        if not pending:
            return send(chat_id, "这个夸克选择已过期，请重新发送分享链接。", home_keyboard())
        try:
            folder = pending["folders"][int(index_text)]
        except (ValueError, IndexError):
            return send(chat_id, "文件夹选择无效，请重新发送分享链接。", home_keyboard())
        selected_url = selected_share_url(pending["url"], folder)
        title = pending.get("title_hint")
        if not title:
            folder_title = folder_media_title(folder["name"])
            try:
                title = moviepilot_media_title(folder_title) if folder_title else None
            except RuntimeError as exc:
                return request_quark_title(
                    chat_id,
                    selected_url,
                    folder["name"],
                    str(exc),
                    pending.get("raw_title") or folder_title,
                )
        if title:
            return confirm_quark_download(chat_id, selected_url, title)
        return request_quark_title(
            chat_id,
            selected_url,
            folder["name"],
            pending.get("title_error"),
            pending.get("raw_title"),
        )
    if data.startswith("category:"):
        return add_to_qbit(chat_id, user_id, data.split(":", 1)[1])
    if data.startswith("task:"):
        return show_task(chat_id, data.split(":", 1)[1])
    if data.startswith("aria:"):
        return show_aria_task(chat_id, data.split(":", 1)[1])
    if data.startswith("ariaaction:"):
        _, action, short_gid = data.split(":", 2)
        return aria_action(chat_id, action, short_gid)
    if data.startswith("ariadeleteask:"):
        short_gid = data.split(":", 1)[1]
        return send(chat_id, "确定删除 Aria2 任务及 VPS 未完成文件吗？", [[{"text": "确认删除", "callback_data": f"ariadeleteyes:{short_gid}"}, {"text": "取消", "callback_data": f"aria:{short_gid}"}]])
    if data.startswith("ariadeleteyes:"):
        return aria_delete(chat_id, data.split(":", 1)[1])
    if data.startswith("action:"):
        _, action, short_hash = data.split(":", 2)
        item = find_task(short_hash)
        if not item:
            return send(chat_id, "任务不存在。", home_keyboard())
        qbit_action(action, item["hash"])
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
    text = message_text_with_links(message)
    if str(chat_id) in ACCOUNT_PENDING:
        if text == "/cancel":
            ACCOUNT_PENDING.pop(str(chat_id), None)
            save_account_pending()
            return send(chat_id, "已取消开号。", home_keyboard())
        if not text or text.startswith("/"):
            return send(chat_id, "请直接输入用户名，或输入 /cancel 取消。")
        try:
            account = EMBY_CLIENT.create_viewer(
                text,
                SETTINGS.emby_default_password,
            )
        except RuntimeError as exc:
            return send(chat_id, f"开号失败：{exc}\n\n请换一个用户名重试，或输入 /cancel 取消。")
        ACCOUNT_PENDING.pop(str(chat_id), None)
        save_account_pending()
        login = (
            f"\n登录地址：{explicit_web_port(SETTINGS.emby_public_url)}"
            if SETTINGS.emby_public_url
            else ""
        )
        return send(
            chat_id,
            f"✅ Emby 普通观看账号已创建\n\n"
            f"用户名：{account['username']}\n"
            f"密码：{SETTINGS.emby_default_password}"
            f"{login}\n\n"
            "已关闭管理、删除、下载、字幕管理和共享权限。",
            home_keyboard(),
        )
    document = message.get("document")
    if document:
        # A .torrent can carry its media title in the Telegram caption.  Keep
        # that explicit metadata for MoviePilot-history deduplication.
        return add_torrent_file(
            chat_id,
            OWNER,
            document,
            message["message_id"],
            extract_post_title(message.get("caption", "")),
        )
    title_pending = QUARK_TITLE_PENDING.get(str(chat_id))
    if title_pending:
        if text == "/cancel":
            QUARK_TITLE_PENDING.pop(str(chat_id), None)
            save_quark_title_pending()
            return send(chat_id, "已取消这次夸克下载。", home_keyboard())
        # Re-forwarding a complete resource post while a title is pending must
        # restart normal post parsing.  Do not feed its description and links
        # to MoviePilot as though the whole message were a title.
        repeated_quark_share = extract_quark_share(text)
        if repeated_quark_share:
            QUARK_TITLE_PENDING.pop(str(chat_id), None)
            save_quark_title_pending()
            return add_quark_share(
                chat_id,
                repeated_quark_share,
                message["message_id"],
                extract_post_title(text),
            )
        if text and not text.startswith("/") and not text.startswith("magnet:"):
            try:
                title = resolve_pending_media_text(text, title_pending)
            except RuntimeError as exc:
                return send(chat_id, str(exc))
            QUARK_TITLE_PENDING.pop(str(chat_id), None)
            save_quark_title_pending()
            return confirm_quark_download(chat_id, title_pending["url"], title)
    quark_share = extract_quark_share(text)
    if quark_share:
        return add_quark_share(chat_id, quark_share, message["message_id"], extract_post_title(text))
    magnet = extract_magnet(text)
    if magnet:
        return add_magnet(
            chat_id,
            OWNER,
            magnet,
            message["message_id"],
            extract_post_title(text),
        )
    if text in {"/start", "/menu"}:
        return home(chat_id, message["from"].get("first_name", ""))
    if text == "/help":
        return send(chat_id, f"发送 magnet、夸克分享链接或上传 .torrent 文件即可。\n系统会智能分类，同时最多下载 {MAX_ACTIVE_DOWNLOADS} 个任务。\n下载完成后由 MoviePilot 自动整理、命名并上传。\n\n/tasks 可查看任务。")
    if legacy_command(chat_id, text):
        return
    if any(word in text for word in ("空间", "硬盘", "云盘", "容量", "服务器状态")):
        return server_status(chat_id)
    if any(word in text for word in ("任务", "队列", "下载进度")):
        return show_tasks(chat_id)
    detected_title = extract_post_title(text)
    if detected_title or any(marker in text for marker in ("投稿ID", "资源信息", "投稿来源")):
        title_line = f"\n\n识别到标题：{detected_title}" if detected_title else ""
        return send(
            chat_id,
            f"⚠️ 未检测到下载链接{title_line}\n\n"
            "请发送包含夸克链接、magnet 链接的完整消息，或上传 .torrent 文件。",
            home_keyboard(),
        )
    send(chat_id, ai_reply(text))


def watch_completed():
    global SEEN
    for item in task_list():
        if item.get("progress", 0) < 1 or item["hash"] in SEEN:
            continue
        try:
            moviepilot_transfer_now()
        except Exception as exc:
            print(f"moviepilot-transfer-now error: {exc}", flush=True)
            continue
        SEEN.add(item["hash"])
        DONE_STORE.save(list(SEEN))
        send_temporary(OWNER, f"✅ 下载完成\n\n{item.get('name', '')}\n分类：{item.get('category') or '智能分类（MoviePilot）'}\n\nMoviePilot 将自动识别、整理并上传到 Google Drive。")
    run_queue()


def moviepilot_successful_proofs(candidates):
    """Return MoviePilot's latest successful source/destination metadata."""

    return load_successful_transfer_proofs(
        SETTINGS.moviepilot_db,
        set(candidates),
    )


def rclone_destination_size(destination):
    """Return the live byte size for one MoviePilot rclone destination."""

    remote = SETTINGS.drive_remote.split(":", 1)[0] + ":"
    try:
        result = subprocess.run(
            [
                "rclone",
                "--config",
                str(SETTINGS.rclone_config),
                "lsjson",
                "--stat",
                "--no-mimetype",
                "--no-modtime",
                f"{remote}{destination}",
                "--contimeout",
                "10s",
                "--timeout",
                "20s",
                "--retries",
                "1",
                "--low-level-retries",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            return None
        item = json.loads(result.stdout)
        if not isinstance(item, dict) or item.get("IsDir") is not False:
            return None
        return int(item.get("Size"))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError):
        return None


def cleanup_transferred_qbit_tasks():
    """Delete source data only after MoviePilot confirms every video file."""

    global LAST_QBIT_CLEANUP
    if not SETTINGS.auto_cleanup_completed:
        return
    now = time.monotonic()
    if now - LAST_QBIT_CLEANUP < SETTINGS.cleanup_interval_seconds:
        return
    LAST_QBIT_CLEANUP = now

    if cloud_block_status(CLOUD_UPLOAD_BLOCK_FILE).get("active"):
        return

    try:
        tasks = QBIT_CLIENT.request("/api/v2/torrents/info").json()
    except Exception as exc:
        print(f"qbit-cleanup check failed: {exc}", flush=True)
        return

    candidates = []
    expected_sizes = {}
    task_files = {}
    for task in tasks if isinstance(tasks, list) else []:
        if float(task.get("progress") or 0) < 1 or is_brush_task(task):
            continue
        task_hash = str(task.get("hash") or "")
        if not task_hash:
            continue
        try:
            files = QBIT_CLIENT.files(task_hash)
        except Exception as exc:
            print(f"qbit-cleanup files {task_hash[:12]} failed: {exc}", flush=True)
            continue
        sizes = selected_video_sizes(task, files)
        if not sizes or not safe_to_cleanup(task, files, set(sizes)):
            continue
        task_files[task_hash] = files
        candidates.append(task)
        expected_sizes.update(sizes)

    if not candidates:
        return
    try:
        proofs = moviepilot_successful_proofs(expected_sizes)
        sources, rejected = verified_transfer_sources(
            proofs,
            rclone_destination_size,
            expected_sizes,
        )
    except Exception as exc:
        print(f"qbit-cleanup verification failed: {exc}", flush=True)
        return

    missing = set(expected_sizes) - set(proofs)
    if missing or rejected:
        print(
            "qbit-cleanup retained unverified files: "
            f"missing_history={len(missing)} rejected={len(rejected)}",
            flush=True,
        )

    removed = []
    for task in candidates:
        task_hash = str(task.get("hash") or "")
        try:
            files = task_files[task_hash]
            if not safe_to_cleanup(task, files, sources):
                continue
            QBIT_CLIENT.action("delete", task_hash, delete_files=True)
            removed.append(str(task.get("name") or task_hash[:12]))
        except Exception as exc:
            print(f"qbit-cleanup task {task_hash[:12]} failed: {exc}", flush=True)

    if removed:
        print(
            f"qbit-cleanup removed {len(removed)} transferred task(s): "
            + " | ".join(removed),
            flush=True,
        )


def watch_aria2_completed():
    changed = False
    for gid, tracked in list(ARIA2_TRACKED.items()):
        try:
            item = aria2_rpc("aria2.tellStatus", [gid, ["gid", "status", "errorMessage", "files"]])
        except Exception as exc:
            # Aria2 eventually forgets old stopped/completed GIDs. Keeping
            # those entries forever only produces HTTP 400 on every polling
            # cycle and makes real task failures difficult to see.
            if "HTTP 400" in str(exc):
                ARIA2_TRACKED.pop(gid, None)
                changed = True
                continue
            print("aria2-watch error:", exc, flush=True)
            continue
        status = item.get("status")
        if status == "complete" and not tracked.get("notified"):
            tracked["notified"] = True
            changed = True
            try:
                moviepilot_transfer_now()
                result_text = "已通知 MoviePilot 立即识别、整理并上传 Google Drive。"
            except Exception as exc:
                print("moviepilot-transfer-now error:", exc, flush=True)
                result_text = "MoviePilot 立即整理调用失败；文件仍保留在完成目录，请检查整理记录。"
            send_temporary(
                OWNER,
                f"✅ Aria2 下载完成\n\n{tracked.get('name', aria2_name(item))}\n\n{result_text}",
            )
        elif status == "complete" and tracked.get("notified"):
            ARIA2_TRACKED.pop(gid, None)
            changed = True
        elif status == "error":
            send(OWNER, f"⚠️ Aria2 下载失败\n\n{tracked.get('name', aria2_name(item))}\n{item.get('errorMessage') or '请在我的任务中检查。'}")
            ARIA2_TRACKED.pop(gid, None)
            changed = True
    if changed:
        save_aria2_tracked()


def main():
    """Run the Telegram long-polling worker."""
    global LAST_CATEGORY_SYNC
    print(f"Island Download Bot {__version__} started", flush=True)
    while not LAST_CATEGORY_SYNC:
        try:
            synchronize_media_categories()
        except Exception as exc:
            print(f"media-category-sync error: {exc}", flush=True)
            time.sleep(10)
    while True:
        try:
            if time.monotonic() - LAST_CATEGORY_SYNC >= CATEGORY_SYNC_INTERVAL:
                try:
                    synchronize_media_categories()
                except Exception as exc:
                    print(f"media-category-sync error: {exc}", flush=True)
                    LAST_CATEGORY_SYNC = (
                        time.monotonic()
                        - CATEGORY_SYNC_INTERVAL
                        + CATEGORY_SYNC_RETRY_INTERVAL
                    )
            updates = telegram(
                "getUpdates",
                {
                    "offset": OFFSET,
                    "timeout": 25,
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                },
            )
            for update in updates.get("result", []):
                handle(update)
            process_hermes_inbox()
            run_quark_queue()
            watch_completed()
            watch_aria2_completed()
            cleanup_transferred_qbit_tasks()
            delete_expired_messages()
        except Exception as exc:
            print("error:", exc, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
