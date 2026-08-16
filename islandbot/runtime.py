import json
import shutil
import sys
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
from .library import missing_plan
from .media import (
    VIDEO_EXTENSIONS,
    clean_title,
    episode_key as domain_episode_key,
    episode_keys as domain_episode_keys,
    explicit_seasons,
    parse_identity_label,
    season_number,
)
from .resolver import MediaResolver, ResolutionError
from .retry import cloud_block_status
from .state import RuntimeState
from .services.normalizer import EpisodeNormalizer
from .services.maintenance import MaintenanceService
from .services.qbit_lifecycle import QBitLifecycle
from .services.drive import DriveService
from .services.cloud_drive_control import CloudDriveControl
from .services.quark import QuarkService
from .services.transfer import TransferService
from .services.telegram_ui import TelegramUI
from .handlers import BotHandlers
from .config import Settings, explicit_web_port
from .parsing import (
    extract_magnet,
    extract_post_title,
    extract_quark_share,
    message_text_with_links,
)

SETTINGS = Settings.from_env()
DRIVE_SERVICE = DriveService(SETTINGS.rclone_config, SETTINGS.drive_remote)
CLOUD_DRIVE_CONTROL = CloudDriveControl(SETTINGS.rclone_config)
TRANSFER_SERVICE = TransferService(SETTINGS.moviepilot_db)
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
QBIT_STAGING_PATH = SETTINGS.qbit_staging_path
OFFSET = 0
PENDING = {}

DATA_DIR = SETTINGS.data_dir
STATE = RuntimeState(DATA_DIR)
DONE_FILE = STATE.done_store.path
DONE_STORE = STATE.done_store
SEEN = STATE.seen
QUEUE_FILE = STATE.queue_file
QUEUE_STORE = STATE.queue_store
QUEUE = STATE.queue
QUEUE_READY = STATE.queue_ready
CLOUD_UPLOAD_BLOCK_FILE = DATA_DIR / "cloud-upload-block.json"
BLOCKED_FILE = STATE.blocked_store.path
BLOCKED_STORE = STATE.blocked_store
BLOCKED = STATE.blocked
RESERVE_GIB = SETTINGS.min_free_gib
MAX_ACTIVE_DOWNLOADS = SETTINGS.max_active_downloads
GIB = 1024 * 1024 * 1024
EXPIRY_FILE = STATE.expiry_store.path
EXPIRY_STORE = STATE.expiry_store
EXPIRING = STATE.expiring
INCOMING_DIR = STATE.incoming_dir
MAX_TORRENT_BYTES = 20 * 1024 * 1024
QUARK_QUEUE_FILE = STATE.quark_queue_store.path
QUARK_QUEUE_STORE = STATE.quark_queue_store
QUARK_QUEUE = STATE.quark_queue
QUARK_PENDING_FILE = STATE.quark_pending_store.path
QUARK_PENDING_STORE = STATE.quark_pending_store
QUARK_PENDING = STATE.quark_pending
QUARK_TITLE_PENDING_FILE = STATE.quark_title_pending_store.path
QUARK_TITLE_PENDING_STORE = STATE.quark_title_pending_store
QUARK_TITLE_PENDING = STATE.quark_title_pending
QUARK_CONFIRM_PENDING_FILE = STATE.quark_confirm_pending_store.path
QUARK_CONFIRM_PENDING_STORE = STATE.quark_confirm_pending_store
QUARK_CONFIRM_PENDING = STATE.quark_confirm_pending
QUARK_ACTIVE = False
QUARK_LOCK = STATE.quark_lock
# Protects QUEUE mutations that may occur from the Hermes background path
# and the main polling loop at the same time.
QUEUE_LOCK = STATE.queue_lock
# Protects EXPIRING list from concurrent access by the AI-reply daemon
# thread (send_temporary) and the main loop (delete_expired_messages).
EXPIRING_LOCK = STATE.expiring_lock
# Tracks when each queued task first appeared at 0% progress.
# Used to detect stalled downloads that never started.
STALL_FIRST_SEEN: dict[str, float] = {}
STALL_THRESHOLD = 30 * 60  # seconds at 0% before auto-pause
STALL_NOTIFIED: set[str] = set()
ARIA2_FILE = STATE.aria2_store.path
ARIA2_STORE = STATE.aria2_store
ARIA2_TRACKED = STATE.aria2_tracked
ACCOUNT_PENDING_FILE = STATE.account_pending_store.path
ACCOUNT_PENDING_STORE = STATE.account_pending_store
ACCOUNT_PENDING = STATE.account_pending
HERMES_INBOX_FILE = STATE.hermes_inbox_file
HERMES_INBOX_LOCK = STATE.hermes_inbox_lock
IDENTITIES = STATE.identities
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
TELEGRAM_UI = TelegramUI(
    lambda method, data: telegram(method, data),
    EXPIRING,
    EXPIRING_LOCK,
    EXPIRY_STORE,
    lambda: CATEGORIES,
    lambda: MAX_ACTIVE_DOWNLOADS,
)
HANDLERS = BotHandlers(lambda: sys.modules[__name__])
NORMALIZER = EpisodeNormalizer(
    QBIT_CLIENT,
    RESOLVER,
    QBIT_STAGING_PATH,
    QBIT_SAVE_PATH,
)
# Compatibility aliases for existing tests and state inspection tools. The
# normalizer service owns these dictionaries; app.py no longer owns the logic.
NORMALIZE_IDENTITY_CACHE = NORMALIZER.identity_cache
NORMALIZE_LAST_ATTEMPT = NORMALIZER.last_attempt
NORMALIZE_RETRY_SECONDS = NORMALIZER.retry_seconds
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
    STATE.save_quark_queue()


def save_quark_pending():
    STATE.save_quark_pending()


def save_quark_title_pending():
    STATE.save_quark_title_pending()


def save_quark_confirm_pending():
    STATE.save_quark_confirm_pending()


def save_aria2_tracked():
    STATE.save_aria2_tracked()


def save_account_pending():
    STATE.save_account_pending()


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


QUARK_SERVICE = QuarkService(
    qas_open,
    QUARK_SAVE_PATH,
    lambda media_title: moviepilot_existing(media_title),
    lambda media_title: google_drive_existing(media_title),
)


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
    return QUARK_SERVICE.task(share_url, task_name, media_title, pattern)


def qas_share_folders(share_url, stoken=None):
    return QUARK_SERVICE.share_folders(share_url, stoken)


def episode_key(text, default_season=None):
    return domain_episode_key(text, default_season)


def episode_keys(text, default_season=None):
    return domain_episode_keys(text, default_season)


def qas_share_video_files(share_url):
    return QUARK_SERVICE.share_video_files(share_url)


def moviepilot_existing(media_title):
    """Compatibility wrapper for MoviePilot history lookup."""
    return TRANSFER_SERVICE.moviepilot_existing(media_title)


def google_drive_existing(media_title):
    """Compatibility wrapper for the live Google Drive library lookup."""
    return DRIVE_SERVICE.existing(media_title)


def quark_missing_plan(share_url, media_title):
    return QUARK_SERVICE.missing_plan(share_url, media_title)


def qas_download_choices(share_url, max_depth=5):
    """Walk harmless wrapper folders until a real choice is reached.

    A common Quark layout is: share root -> "Drama name" ->
    "Season 1 (HQ)" / "Season 1 (SDR)". The former is a wrapper, not a
    download choice. Keep its title as a hint for MoviePilot.
    """
    return QUARK_SERVICE.download_choices(share_url, folder_media_title, max_depth)


def selected_share_url(share_url, folder):
    return QUARK_SERVICE.selected_share_url(share_url, folder)


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
        timeout=15,
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
        download_path(category, QBIT_STAGING_PATH, CATEGORIES),
    )


def synchronize_media_categories():
    """Keep the bot and qBittorrent aligned with MoviePilot's nine categories."""

    global LAST_CATEGORY_SYNC
    categories = load_moviepilot_categories(
        SETTINGS.moviepilot_category_file,
        required=True,
    )
    result = QBIT_CLIENT.sync_categories(
        # These are shared qBittorrent categories.  MoviePilot/PT tasks use
        # them too, so their default paths must remain MoviePilot-visible.
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
    source_seasons = {
        season
        for name in source_names
        for season in explicit_seasons(name)
    }
    if source_seasons and source_seasons != {identity.season}:
        seasons = "、".join(f"第 {season} 季" for season in sorted(source_seasons))
        raise RuntimeError(
            f"种子文件明确标记为 {seasons}，但当前确认身份是第 {identity.season} 季。"
            "已停止下载，请修改媒体身份后重试。"
        )
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
    TELEGRAM_UI.telegram_call = lambda method, data: telegram(method, data)
    return TELEGRAM_UI.send(chat_id, text, keyboard)


def send_temporary(chat_id, text, lifetime_seconds=300):
    TELEGRAM_UI.telegram_call = lambda method, data: telegram(method, data)
    TELEGRAM_UI.expiring = EXPIRING
    TELEGRAM_UI.expiring_lock = EXPIRING_LOCK
    TELEGRAM_UI.expiry_store = EXPIRY_STORE
    return TELEGRAM_UI.send_temporary(chat_id, text, lifetime_seconds)


def delete_expired_messages():
    TELEGRAM_UI.telegram_call = lambda method, data: telegram(method, data)
    TELEGRAM_UI.expiring = EXPIRING
    TELEGRAM_UI.expiring_lock = EXPIRING_LOCK
    TELEGRAM_UI.expiry_store = EXPIRY_STORE
    TELEGRAM_UI.delete_expired_messages()


def answer(callback_id, text=None):
    TELEGRAM_UI.telegram_call = lambda method, data: telegram(method, data)
    TELEGRAM_UI.answer(callback_id, text)


def home_keyboard():
    return TELEGRAM_UI.home_keyboard()


def category_keyboard():
    TELEGRAM_UI.categories = lambda: CATEGORIES
    return TELEGRAM_UI.category_keyboard()


def home(chat_id, first_name=""):
    TELEGRAM_UI.telegram_call = lambda method, data: telegram(method, data)
    TELEGRAM_UI.categories = lambda: CATEGORIES
    TELEGRAM_UI.max_active_downloads = lambda: MAX_ACTIVE_DOWNLOADS
    return TELEGRAM_UI.home(chat_id, first_name)


def task_list():
    return json.loads(qbit("/api/v2/torrents/info?tag=islandbot&sort=added_on&reverse=true"))


def normalize_completed_episode_files(tasks):
    """Compatibility wrapper for the extracted normalizer service."""
    # Keep legacy module-level seams used by tests and integrations working
    # while the service owns its production dependencies.
    NORMALIZER.qbit = QBIT_CLIENT
    NORMALIZER.resolver = RESOLVER
    return NORMALIZER.normalize_completed_episode_files(tasks)


def prepare_completed_tasks(tasks):
    """Compatibility wrapper for staging promotion."""
    NORMALIZER.qbit = QBIT_CLIENT
    NORMALIZER.resolver = RESOLVER
    return NORMALIZER.prepare_completed_tasks(tasks)


def save_queue():
    QUEUE_STORE.save(QUEUE)


def save_blocked():
    BLOCKED_STORE.save(list(BLOCKED))


QBIT_LIFECYCLE = QBitLifecycle(
    QBIT_CLIENT,
    task_list,
    lambda text: send(OWNER, text),
    OWNER,
    QUEUE,
    BLOCKED,
    save_queue,
    save_blocked,
    QUEUE_READY,
    MAX_ACTIVE_DOWNLOADS,
    RESERVE_GIB,
    SETTINGS.downloads_dir,
    CLOUD_UPLOAD_BLOCK_FILE,
    SETTINGS.auto_cleanup_completed,
    SETTINGS.cleanup_interval_seconds,
    lambda candidates: moviepilot_successful_proofs(candidates),
    lambda destination: rclone_destination_size(destination),
    stall_threshold=STALL_THRESHOLD,
)


def google_drive_capacity():
    """Read the configured Google Drive quota through MoviePilot's rclone remote."""
    return DRIVE_SERVICE.capacity()


def moviepilot_transfer_now():
    """Ask MoviePilot to scan completed external downloads immediately."""
    if not MOVIEPILOT_TOKEN:
        raise RuntimeError("MoviePilot API_TOKEN 尚未连接")
    query = urllib.parse.urlencode({"token": MOVIEPILOT_TOKEN})
    status, body, _ = request(f"{MOVIEPILOT_URL}/api/v1/transfer/now?{query}")
    if status >= 300:
        raise RuntimeError(f"MoviePilot 立即整理失败（HTTP {status}）：{body[:100]}")


def run_queue():
    """Compatibility wrapper for the extracted qB lifecycle service."""
    global QUEUE_READY
    QBIT_LIFECYCLE.qbit = QBIT_CLIENT
    QBIT_LIFECYCLE.tasks_loader = task_list
    QBIT_LIFECYCLE.queue = QUEUE
    QBIT_LIFECYCLE.blocked = BLOCKED
    QBIT_LIFECYCLE.queue_ready = QUEUE_READY
    QBIT_LIFECYCLE.send_owner = lambda text: send(OWNER, text)
    QBIT_LIFECYCLE.max_active_downloads = MAX_ACTIVE_DOWNLOADS
    QBIT_LIFECYCLE.reserve_gib = RESERVE_GIB
    QBIT_LIFECYCLE.run_queue()
    QUEUE_READY = QBIT_LIFECYCLE.queue_ready


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


def server_status_text():
    try:
        info = json.loads(qbit("/api/v2/transfer/info"))
        tasks = task_list()
        disk = shutil.disk_usage(SETTINGS.downloads_dir)
        aria_stats = aria2_rpc("aria2.getGlobalStat")
    except Exception as exc:
        print("server-status error:", exc, flush=True)
        return "🖥 状态暂时无法读取。\n请稍后再试。"
    download = info.get("dl_info_speed", 0) / 1024 / 1024
    upload = info.get("up_info_speed", 0) / 1024 / 1024
    active = sum(1 for item in tasks if item.get("progress", 0) < 1)
    aria_active = int(aria_stats.get("numActive") or 0)
    aria_speed = int(aria_stats.get("downloadSpeed") or 0) / 1024 / 1024
    used = disk.total - disk.free
    drive_capacity = google_drive_capacity()
    return (
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


def server_status(chat_id):
    send(
        chat_id,
        server_status_text(),
        [
            [{"text": "☁️ 云盘控制", "callback_data": "drive:open"}],
            [{"text": "刷新", "callback_data": "home:server"}, {"text": "← 主菜单", "callback_data": "home:home"}],
        ],
    )


def cloud_drive_view():
    """Return the live MP alias selection from the shared rclone config."""
    return CLOUD_DRIVE_CONTROL.view()


def cloud_drive_text(notice=None):
    view = cloud_drive_view()
    text = (
        "☁️ 云盘控制\n\n"
        f"MoviePilot 当前入库：{view['current']}\n"
        f"可切换到：{view['target']}\n\n"
        "切换前会先验证目标云盘写入。\n"
        "只影响之后的新入库，不会移动已有文件。"
    )
    if notice:
        text += f"\n\n{notice}"
    return text


def cloud_drive_keyboard():
    view = cloud_drive_view()
    return [
        [
            {"text": "🧪 检测当前云盘", "callback_data": "drive:check"},
            {"text": f"切换到 {view['target']}", "callback_data": f"drive:switchask:{view['target']}"},
        ],
        [{"text": "刷新", "callback_data": "drive:open"}, {"text": "← 状态与设置", "callback_data": "home:server"}],
    ]


def cloud_drive_screen(chat_id, notice=None):
    return send(chat_id, cloud_drive_text(notice), cloud_drive_keyboard())


def cloud_drive_probe(chat_id):
    try:
        result = CLOUD_DRIVE_CONTROL.probe_current()
        notice = CLOUD_DRIVE_CONTROL.probe_message(result)
    except Exception as exc:
        notice = f"❌ 云盘检测失败：{str(exc)[:160]}"
    return cloud_drive_screen(chat_id, notice)


def cloud_drive_confirm(chat_id, target):
    view = cloud_drive_view()
    if target != view["target"]:
        return cloud_drive_screen(chat_id, "⚠️ 当前云盘已变化，请重新选择。")
    return send(
        chat_id,
        f"确认从 {view['current']} 切换到 {target} 吗？\n\n"
        "系统会先检测目标云盘；写入失败时不会切换。",
        [[
            {"text": "确认切换", "callback_data": f"drive:switch:{target}"},
            {"text": "取消", "callback_data": "drive:open"},
        ]],
    )


def cloud_drive_switch(chat_id, target):
    try:
        result = CLOUD_DRIVE_CONTROL.switch(target)
        notice = CLOUD_DRIVE_CONTROL.switch_message(result)
    except Exception as exc:
        notice = f"❌ 云盘切换未完成，请刷新确认当前云盘：{str(exc)[:130]}"
    return cloud_drive_screen(chat_id, notice)


def service_tick():
    """Run one background maintenance cycle without polling Telegram updates."""
    MAINTENANCE.tick()


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
    # --- InfoHash conflict detection ---
    # Check the secondary brush-traffic qBittorrent (if configured) to avoid
    # both clients downloading the same torrent simultaneously.
    if SETTINGS.qbit2_url and "magnet" in pending:
        try:
            qbit2 = QBitClient(
                SETTINGS.qbit2_url,
                SETTINGS.qbit2_username,
                SETTINGS.qbit2_password,
            )
            remote_hashes = {
                str(t.get("hash") or "").lower()
                for t in qbit2.request("/api/v2/torrents/info").json()
                if isinstance(t, dict)
            }
            # Extract infohash from the magnet URI.
            xt_match = re.search(
                r"xt=urn:btih:([a-fA-F0-9]{40})", pending["magnet"]
            )
            magnet_hash = xt_match.group(1).lower() if xt_match else ""
            if not magnet_hash:
                xt_match = re.search(
                    r"xt=urn:btih:([A-Za-z2-7]{32})", pending["magnet"]
                )
                if xt_match:
                    import base64
                    magnet_hash = base64.b32decode(
                        xt_match.group(1).upper()
                    ).hex()
            if magnet_hash and magnet_hash in remote_hashes:
                return send(
                    chat_id,
                    f"⚠️ InfoHash 冲突\n\n"
                    f"此种子已在刷流 qBittorrent 中下载。\n"
                    f"哈希：{magnet_hash[:16]}…\n\n"
                    "为避免双客户端同时下载同一种子，已取消添加。",
                    home_keyboard(),
                )
        except Exception as exc:
            # If the secondary qB is unreachable, proceed normally.
            print(f"qbit2-infohash-check skipped: {exc}", flush=True)
    if "magnet" in pending:
        add_data = {
            "urls": pending["magnet"],
            "tags": "islandbot",
            "autoTMM": "false",
            "stopCondition": "MetadataReceived",
            "savepath": download_path(category, QBIT_STAGING_PATH, CATEGORIES),
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
    # Poll up to 5 s instead of a fixed 1 s sleep so that fast registrations
    # are detected immediately and slow ones don't silently fail.
    added = []
    for _ in range(10):
        added = [item for item in task_list() if item["hash"] not in before]
        if added:
            break
        time.sleep(0.5)
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

    with QUEUE_LOCK:
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
    """Compatibility entry point backed by the extracted Telegram handlers."""
    return HANDLERS.legacy_command(chat_id, text)


def handle_callback(callback):
    """Compatibility entry point backed by the extracted Telegram handlers."""
    return HANDLERS.handle_callback(callback)


def handle(update):
    """Route one Telegram update through the extracted handler service."""
    return HANDLERS.handle(update)


def watch_completed():
    """Compatibility wrapper for completion monitoring."""
    MAINTENANCE.watch_completed()


def moviepilot_successful_proofs(candidates):
    """Return MoviePilot's latest successful source/destination metadata."""
    return TRANSFER_SERVICE.successful_proofs(candidates)


def rclone_destination_size(destination):
    """Return the live byte size for one MoviePilot rclone destination."""
    return DRIVE_SERVICE.destination_size(destination)


def cleanup_transferred_qbit_tasks():
    """Compatibility wrapper for verified qB cleanup."""
    global LAST_QBIT_CLEANUP
    QBIT_LIFECYCLE.qbit = QBIT_CLIENT
    QBIT_LIFECYCLE.auto_cleanup_completed = SETTINGS.auto_cleanup_completed
    QBIT_LIFECYCLE.cleanup_interval_seconds = SETTINGS.cleanup_interval_seconds
    QBIT_LIFECYCLE.last_cleanup = LAST_QBIT_CLEANUP
    QBIT_LIFECYCLE.successful_proofs = lambda candidates: moviepilot_successful_proofs(candidates)
    QBIT_LIFECYCLE.destination_size = lambda destination: rclone_destination_size(destination)
    QBIT_LIFECYCLE.cleanup_transferred()
    LAST_QBIT_CLEANUP = QBIT_LIFECYCLE.last_cleanup


def watch_aria2_completed():
    """Compatibility wrapper for Aria2 completion monitoring."""
    MAINTENANCE.watch_aria2_completed()


def _set_last_category_sync(value):
    global LAST_CATEGORY_SYNC
    LAST_CATEGORY_SYNC = value


MAINTENANCE = MaintenanceService(
    monotonic=time.monotonic,
    wall_time=time.time,
    get_last_category_sync=lambda: LAST_CATEGORY_SYNC,
    set_last_category_sync=_set_last_category_sync,
    category_sync_interval=CATEGORY_SYNC_INTERVAL,
    category_sync_retry_interval=CATEGORY_SYNC_RETRY_INTERVAL,
    synchronize_categories=lambda: synchronize_media_categories(),
    process_inbox=lambda: process_hermes_inbox(),
    run_quark_queue=lambda: run_quark_queue(),
    task_list=lambda: task_list(),
    normalize_completed=lambda tasks: normalize_completed_episode_files(tasks),
    prepare_completed=lambda tasks: prepare_completed_tasks(tasks),
    trigger_transfer=lambda: moviepilot_transfer_now(),
    run_download_queue=lambda: run_queue(),
    send_temporary=lambda owner, text: send_temporary(owner, text),
    owner=OWNER,
    seen=SEEN,
    save_seen=lambda value: DONE_STORE.save(value),
    aria_tracked=ARIA2_TRACKED,
    aria_rpc=lambda method, params: aria2_rpc(method, params),
    aria_name=lambda item: aria2_name(item),
    send=lambda owner, text: send(owner, text),
    save_aria_tracked=lambda: save_aria2_tracked(),
    cleanup_transferred=lambda: cleanup_transferred_qbit_tasks(),
    delete_expired=lambda: delete_expired_messages(),
    log=lambda message: print(message, flush=True),
)
