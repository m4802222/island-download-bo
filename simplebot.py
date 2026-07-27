import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import re
import urllib.error
import http.cookiejar
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER_ID"])
QBIT_URL = os.environ.get("QBIT_URL", "http://qbittorrent:8080").rstrip("/")
QBIT_USER = os.environ["QBIT_USERNAME"]
QBIT_PASSWORD = os.environ["QBIT_PASSWORD"]
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:1.7b")
QAS_URL = os.environ.get("QAS_URL", "http://quark-auto-save:5005").rstrip("/")
QAS_USER = os.environ.get("QAS_USERNAME", "")
QAS_PASSWORD = os.environ.get("QAS_PASSWORD", "")
QUARK_SAVE_PATH = os.environ.get("QUARK_SAVE_PATH", "/IslandDownloadBot")
ARIA2_URL = os.environ.get("ARIA2_URL", "http://aria2:6800/jsonrpc")
ARIA2_SECRET = os.environ.get("ARIA2_SECRET", "")
MOVIEPILOT_URL = os.environ.get("MOVIEPILOT_URL", "http://moviepilot:3001").rstrip("/")
MOVIEPILOT_TOKEN = os.environ.get("MOVIEPILOT_TOKEN", "").strip()
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
MAX_ACTIVE_DOWNLOADS = max(1, int(os.environ.get("MAX_ACTIVE_DOWNLOADS", "2")))
GIB = 1024 * 1024 * 1024
EXPIRY_FILE = DATA_DIR / "expiry.json"
EXPIRING = list(json.loads(EXPIRY_FILE.read_text())) if EXPIRY_FILE.exists() else []
INCOMING_DIR = DATA_DIR / "incoming"
INCOMING_DIR.mkdir(exist_ok=True)
MAX_TORRENT_BYTES = 20 * 1024 * 1024
QUARK_QUEUE_FILE = DATA_DIR / "quark_queue.json"
QUARK_QUEUE = list(json.loads(QUARK_QUEUE_FILE.read_text())) if QUARK_QUEUE_FILE.exists() else []
QUARK_PENDING_FILE = DATA_DIR / "quark_pending.json"
QUARK_PENDING = json.loads(QUARK_PENDING_FILE.read_text()) if QUARK_PENDING_FILE.exists() else {}
QUARK_TITLE_PENDING_FILE = DATA_DIR / "quark_title_pending.json"
QUARK_TITLE_PENDING = json.loads(QUARK_TITLE_PENDING_FILE.read_text()) if QUARK_TITLE_PENDING_FILE.exists() else {}
QUARK_CONFIRM_PENDING_FILE = DATA_DIR / "quark_confirm_pending.json"
QUARK_CONFIRM_PENDING = json.loads(QUARK_CONFIRM_PENDING_FILE.read_text()) if QUARK_CONFIRM_PENDING_FILE.exists() else {}
QUARK_ACTIVE = False
QUARK_LOCK = threading.Lock()
ARIA2_FILE = DATA_DIR / "aria2.json"
ARIA2_TRACKED = json.loads(ARIA2_FILE.read_text()) if ARIA2_FILE.exists() else {}
HERMES_INBOX_FILE = DATA_DIR / "hermes_inbox.json"
HERMES_INBOX_LOCK = threading.Lock()
MEDIA_ID_CACHE_FILE = DATA_DIR / "media_id_cache.json"
try:
    MEDIA_ID_CACHE = json.loads(MEDIA_ID_CACHE_FILE.read_text()) if MEDIA_ID_CACHE_FILE.exists() else {}
except (OSError, json.JSONDecodeError):
    MEDIA_ID_CACHE = {}

CATEGORIES = ["国产电影", "国产动漫", "国产剧集", "港台剧集", "欧美电影", "欧美剧集", "日韩电影", "日韩剧集", "日韩动漫"]
QUARK_URL_RE = re.compile(r"https?://pan\.quark\.cn/s/[A-Za-z0-9]+(?:\?[^\s]*)?", re.IGNORECASE)
MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[A-Za-z0-9]+[^\s]*", re.IGNORECASE)


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
    QUARK_QUEUE_FILE.write_text(json.dumps(QUARK_QUEUE, ensure_ascii=False))


def save_quark_pending():
    QUARK_PENDING_FILE.write_text(json.dumps(QUARK_PENDING, ensure_ascii=False))


def save_quark_title_pending():
    QUARK_TITLE_PENDING_FILE.write_text(json.dumps(QUARK_TITLE_PENDING, ensure_ascii=False))


def save_quark_confirm_pending():
    QUARK_CONFIRM_PENDING_FILE.write_text(json.dumps(QUARK_CONFIRM_PENDING, ensure_ascii=False))


def save_aria2_tracked():
    ARIA2_FILE.write_text(json.dumps(ARIA2_TRACKED, ensure_ascii=False))


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
    if not ARIA2_SECRET:
        raise RuntimeError("Aria2 密钥尚未配置")
    arguments = list(params or [])
    arguments.insert(0, f"token:{ARIA2_SECRET}")
    status, body, _ = json_request(
        ARIA2_URL,
        {"jsonrpc": "2.0", "id": "island-download-bot", "method": method, "params": arguments},
        timeout=20,
    )
    if status >= 300:
        raise RuntimeError(f"Aria2 HTTP {status}")
    payload = json.loads(body)
    if payload.get("error"):
        raise RuntimeError(payload["error"].get("message", "Aria2 RPC error"))
    return payload.get("result")


def aria2_recent():
    keys = ["gid", "status", "totalLength", "completedLength", "downloadSpeed", "errorMessage", "files"]
    active = aria2_rpc("aria2.tellActive", [keys])
    waiting = aria2_rpc("aria2.tellWaiting", [0, 30, keys])
    stopped = aria2_rpc("aria2.tellStopped", [0, 30, keys])
    return active + waiting + stopped


def aria2_name(item):
    files = item.get("files") or []
    if files:
        return Path(files[0].get("path") or "Aria2 文件").name
    return "Aria2 文件"


def aria2_percent(item):
    total = int(item.get("totalLength") or 0)
    done = int(item.get("completedLength") or 0)
    return (done / total * 100) if total else 0


def is_quark_share(text):
    parsed = urllib.parse.urlparse(text)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "pan.quark.cn" and parsed.path.startswith("/s/")


def extract_quark_share(text):
    """Find a Quark link even when it is embedded in a forwarded post."""
    match = QUARK_URL_RE.search(text)
    return match.group(0).rstrip(".,，。;；)") if match else None


def extract_magnet(text):
    """Find a magnet link even when it is inside a forwarded media post."""
    match = MAGNET_RE.search(text)
    return match.group(0).rstrip(".,，。;；)") if match else None


def extract_post_title(text):
    """Read the title and year from a forwarded channel post.

    Telegram forwarding can alter or omit the leading movie emoji, so do not
    rely on the emoji alone. The first ordinary line containing Chinese text
    plus a four-digit year is the explicit metadata supplied by the post.
    """
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not re.search(r"[\u4e00-\u9fff]", line):
            continue
        match = re.search(r"(.+?[（(]\d{4}[)）])", line)
        if not match:
            continue
        title = match.group(1)
        title = re.sub(r"^[^\w\u4e00-\u9fff]+", "", title).strip()
        # Ignore metadata lines such as '发布时间：2026-07-24'.
        if any(label in title for label in ("发布时间", "资源大小", "链接状态", "投稿ID")):
            continue
        try:
            return media_folder_name(title)
        except RuntimeError:
            return None
    return None


def qas_open(path, payload=None, timeout=45):
    """Log in to QAS for one request and return its response."""
    if not QAS_USER or not QAS_PASSWORD:
        raise RuntimeError("夸克模块尚未配置 QAS 账号")
    last_error = None
    # QAS can occasionally leave one Quark request hanging. Retry once with a
    # fresh web session instead of making the Telegram user resend the post.
    for attempt in range(2):
        try:
            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            login_body = urllib.parse.urlencode({"username": QAS_USER, "password": QAS_PASSWORD}).encode()
            opener.open(urllib.request.Request(f"{QAS_URL}/login", login_body), timeout=20).read()
            headers = {"Content-Type": "application/json"}
            body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
            return opener.open(urllib.request.Request(f"{QAS_URL}{path}", body, headers), timeout=timeout)
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(2)
    raise RuntimeError(f"夸克解析超时：{last_error}")


def media_folder_name(title):
    """Make a safe, human-readable parent folder for MoviePilot recognition."""
    name = re.sub(r'[\\/:*?"<>|]+', " ", title).strip()
    name = re.sub(r"\s+", " ", name)
    if not name:
        raise RuntimeError("剧名不能为空")
    return name[:96]


def checked_media_query(title):
    """Return a safe title for automatic TMDB resolution, or stop safely.

    Quark folder names are often release labels (or even one-character
    placeholders such as ``G!``).  Sending those to a fuzzy TMDB search can
    produce a perfectly valid, but completely unrelated, media item.  A
    failed validation deliberately stops before any QAS/Aria2 task is made.
    """
    original = media_folder_name(title)
    probe = re.sub(r"[（(]\d{4}[)）]", " ", original)
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", probe))
    english_words = re.findall(r"[A-Za-z0-9]{3,}", probe)
    if len(chinese) >= 2 or english_words:
        return original
    raise RuntimeError(
        f"无法确认媒体标题“{original}”，未开始下载。\n"
        "请发送包含剧名和年份的完整资源帖，或直接回复剧名，例如：龙之家族 (2022)"
    )


def identity_name(title):
    """Reduce a release/post title to the words that identify the media."""
    value = re.sub(r"\{tmdb-\d+\}", " ", title, flags=re.IGNORECASE)
    value = re.sub(r"[（(]\d{4}[)）]", " ", value)
    value = re.sub(r"第\s*[一二三四五六七八九十\d]+\s*季", " ", value)
    value = re.sub(r"(?i)\bseason\s*\d+\b", " ", value)
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", value))
    english = " ".join(re.findall(r"[a-z0-9]{3,}", value.lower()))
    return chinese, english


def media_identities_match(source, result):
    """Reject a valid-looking TMDB result when its title is unrelated."""
    source_cn, source_en = identity_name(source)
    result_cn, result_en = identity_name(result)
    if len(source_cn) >= 2:
        return len(result_cn) >= 2 and (source_cn in result_cn or result_cn in source_cn)
    source_words = set(source_en.split())
    result_words = set(result_en.split())
    return bool(source_words & result_words)


def safe_confirmed_media_title(title):
    """A confirmation card must never accept a one-character placeholder."""
    chinese, english = identity_name(title)
    return len(chinese) >= 2 or bool(english)


def save_media_id_cache():
    temporary = MEDIA_ID_CACHE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(MEDIA_ID_CACHE, ensure_ascii=False))
    temporary.replace(MEDIA_ID_CACHE_FILE)


def moviepilot_media_title(title):
    """Resolve explicit post metadata through MoviePilot before any Quark download.

    The returned folder deliberately includes TMDB's numeric identity.  It is
    an instruction for MoviePilot, not a guessed title, so later Aria2 files
    remain recognisable even when their release names contain no show name.
    """
    original = checked_media_query(title)
    cached = MEDIA_ID_CACHE.get(original)
    if cached and cached.get("title") and cached.get("tmdb_id"):
        if media_identities_match(original, cached["title"]):
            return cached["title"]
        MEDIA_ID_CACHE.pop(original, None)
        save_media_id_cache()
    if not MOVIEPILOT_TOKEN:
        raise RuntimeError("MoviePilot API_TOKEN 尚未连接，未开始下载")
    query = urllib.parse.urlencode({"title": original, "token": MOVIEPILOT_TOKEN})
    url = f"{MOVIEPILOT_URL}/api/v1/media/recognize2?{query}"
    status, body, _ = request(url)
    if status >= 300:
        raise RuntimeError(f"MoviePilot 识别请求失败（HTTP {status}），未开始下载")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError("MoviePilot 识别返回异常，未开始下载")
    media = payload.get("media_info") or {}
    tmdb_id = media.get("tmdb_id")
    name = media.get("title")
    year = media.get("year") or media.get("release_year")
    if not tmdb_id or not name:
        raise RuntimeError(f"MoviePilot 未确认“{original}”的 TMDB 信息，未开始下载")
    canonical = media_folder_name(f"{name} ({year}) {{tmdb-{tmdb_id}}}" if year else f"{name} {{tmdb-{tmdb_id}}}")
    if not media_identities_match(original, canonical):
        raise RuntimeError(
            f"MoviePilot 返回了不相关结果“{canonical}”，未开始下载。"
            "请回复更准确的剧名和首播年份。"
        )
    MEDIA_ID_CACHE[original] = {"title": canonical, "tmdb_id": str(tmdb_id), "updated_at": time.time()}
    save_media_id_cache()
    return canonical


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


def request_quark_title(chat_id, share_url, folder_name, reason=None):
    QUARK_TITLE_PENDING[str(chat_id)] = {
        "url": share_url,
        "folder": folder_name,
        "created_at": time.time(),
    }
    save_quark_title_pending()
    reason_line = f"\n原因：{reason}\n" if reason else ""
    return send(
        chat_id,
        f"⚠️ 暂未开始下载\n\n已选择：{folder_name}{reason_line}\n"
        "请直接回复正确剧名和首播年份，例如：\n光阴之外 (2025)\n\n输入 /cancel 可取消。",
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
    return send(
        chat_id,
        f"🎬 已确认媒体身份\n\n{media_title}\n\n系统将只下载 Google Drive 中缺少的集数。",
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
        aria_save_path = f"incoming/{media_folder_name(media_title)}"
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
    payload = json.loads(response.read().decode(errors="replace"))
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


VIDEO_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".avi", ".ts", ".mov")


def episode_key(text):
    """Normalize S01E02-like labels so release names can be compared exactly."""
    match = re.search(r"(?i)\bS(\d{1,2})E(\d{1,3})\b", text)
    if not match:
        return None
    return f"S{int(match.group(1)):02d}E{int(match.group(2)):03d}"


def episode_keys(text):
    return {
        f"S{int(season):02d}E{int(episode):03d}"
        for season, episode in re.findall(r"(?i)\bS(\d{1,2})E(\d{1,3})\b", text)
    }


def qas_share_video_files(share_url):
    """Return direct video file names in the chosen Quark directory."""
    response = qas_open("/get_share_detail", {"shareurl": share_url})
    payload = json.loads(response.read().decode(errors="replace"))
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
    """Read successful MoviePilot transfers, which are the upload-complete source of truth."""
    database = Path("/moviepilot-config/user.db")
    if not database.is_file():
        raise RuntimeError("MoviePilot 整理历史不可读取，已停止提交下载以防重复")
    title = re.sub(r"\s*\{tmdb-\d+\}\s*$", "", media_title, flags=re.IGNORECASE)
    title = re.sub(r"\s*[（(]\d{4}[)）]\s*$", "", title).strip()
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
    episodes = set()
    for row in rows:
        for value in row:
            if value:
                episodes.update(episode_keys(str(value)))
    return episodes, bool(rows)


def google_drive_existing(media_title):
    """Read the real Google Drive library instead of trusting history alone."""
    canonical = re.sub(r"\s*\{tmdb-\d+\}\s*$", "", media_title, flags=re.IGNORECASE).strip()
    bare_title = re.sub(r"\s*[（(]\d{4}[)）]\s*$", "", canonical).strip()
    try:
        result = subprocess.run(
            [
                "rclone",
                "--config",
                "/rclone/rclone.conf",
                "lsf",
                "MP:Media",
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
        if any(component == canonical or component.startswith(f"{bare_title} (") for component in components):
            matched.append(path)
    episodes = set()
    for path in matched:
        episodes.update(episode_keys(path))
    return episodes, bool(matched)


def quark_missing_plan(share_url, media_title):
    """Build a QAS include regex using the actual Drive library as truth."""
    files = qas_share_video_files(share_url)
    if not files:
        return "", 0, 0
    history_episodes, has_history = moviepilot_existing(media_title)
    drive_episodes, has_drive_media = google_drive_existing(media_title)
    existing_episodes = history_episodes | drive_episodes
    episode_files = [(name, episode_key(name)) for name in files]
    if any(key for _, key in episode_files):
        missing = [name for name, key in episode_files if not key or key not in existing_episodes]
    elif has_drive_media or has_history:
        # Movies do not have episode IDs. A real Drive file or a successful
        # transfer record for the same title means that movie already exists.
        missing = []
    else:
        missing = files
    skipped = len(files) - len(missing)
    if not missing:
        return None, len(files), skipped
    if skipped:
        return r"^(?:" + "|".join(re.escape(name) for name in missing) + r")$", len(files), skipped
    return "", len(files), 0


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
        if QUARK_ACTIVE or not QUARK_QUEUE:
            return
        task = QUARK_QUEUE.pop(0)
        save_quark_queue()
        QUARK_ACTIVE = True

    def worker():
        global QUARK_ACTIVE
        try:
            before = {item.get("gid") for item in aria2_recent()}
            response = qas_open("/run_script_now", {"tasklist": [qas_task(task["url"], task["name"], task.get("media_title"), task.get("pattern", ""))]})
            output = response.read().decode(errors="replace")
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
                    return request_quark_title(chat_id, base_url, "分享根目录", title_error)
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
                return request_quark_title(chat_id, selected_url, folder["name"], title_error)
            key = uuid.uuid4().hex[:10]
            QUARK_PENDING[key] = {
                "url": base_url,
                "folders": folders,
                "title_hint": title_hint,
                "title_error": title_error,
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


def qbit_action(action, hashes, delete_files=False):
    """Use qBittorrent 5 API names while keeping friendly bot action names."""
    endpoint = {"pause": "stop", "resume": "start"}.get(action, action)
    data = {"hashes": hashes}
    if delete_files:
        data["deleteFiles"] = "true"
    return qbit(f"/api/v2/torrents/{endpoint}", data)


def qbit_add_torrent_file(filename, content, category):
    """Upload a .torrent file to qBittorrent using its multipart API."""
    global COOKIE
    boundary = f"----IslandDownload{uuid.uuid4().hex}"
    body = bytearray()

    def field(name, value):
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(str(value).encode())
        body.extend(b"\r\n")

    body.extend(f"--{boundary}\r\n".encode())
    safe_name = Path(filename).name.replace('"', "_")
    body.extend(f'Content-Disposition: form-data; name="torrents"; filename="{safe_name}"\r\n'.encode())
    body.extend(b"Content-Type: application/x-bittorrent\r\n\r\n")
    body.extend(content)
    body.extend(b"\r\n")
    field("tags", "islandbot")
    field("autoTMM", "false")
    # Let qBittorrent obtain metadata, then stop before file data downloads.
    # This gives us a safe point to deselect episodes already uploaded by
    # MoviePilot.  qBittorrent Web API supports this stop condition.
    field("stopCondition", "MetadataReceived")
    if category != "__auto__":
        field("category", category)
    body.extend(f"--{boundary}--\r\n".encode())
    headers = {"Cookie": COOKIE, "Content-Type": f"multipart/form-data; boundary={boundary}"}
    status, response, _ = raw_request(f"{QBIT_URL}/api/v2/torrents/add", bytes(body), headers)
    if status in (401, 403):
        COOKIE = ""
        qbit_login()
        headers["Cookie"] = COOKIE
        status, response, _ = raw_request(f"{QBIT_URL}/api/v2/torrents/add", bytes(body), headers)
    if status >= 300:
        raise RuntimeError(response.decode(errors="replace")[:180])


def qbit_files(torrent_hash):
    """Return qBittorrent's indexed file list for one torrent."""
    return json.loads(qbit(f"/api/v2/torrents/files?hash={torrent_hash}"))


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
    """Disable already-uploaded video episodes before the torrent is queued.

    MoviePilot's successful transfer history is the source of truth: only files
    with an SxxExx key already recorded there are made unwanted in qBittorrent.
    A movie without episode labels is skipped only when the same title has a
    successful history record.
    """
    files = wait_qbit_files(torrent_hash)
    videos = [
        item for item in files
        if str(item.get("name", "")).lower().endswith(VIDEO_EXTENSIONS)
    ]
    if not videos:
        return {"total": 0, "skipped": 0, "remaining": 0}
    if not media_title:
        # A raw magnet normally has no trustworthy title. Do not guess and do
        # not risk excluding the wrong media; channel posts supply this title.
        return {"total": len(videos), "skipped": 0, "remaining": len(videos)}

    existing_episodes, has_history = moviepilot_existing(media_title)
    skip_ids = []
    has_episode_labels = any(episode_key(item.get("name", "")) for item in videos)
    for item in videos:
        key = episode_key(item.get("name", ""))
        if (key and key in existing_episodes) or (not has_episode_labels and has_history):
            skip_ids.append(str(item.get("index")))

    if skip_ids:
        qbit(
            "/api/v2/torrents/filePrio",
            {"hash": torrent_hash, "id": "|".join(skip_ids), "priority": "0"},
        )
    return {
        "total": len(videos),
        "skipped": len(skip_ids),
        "remaining": len(videos) - len(skip_ids),
    }


def telegram(method, data):
    status, body, _ = request(f"https://api.telegram.org/bot{TOKEN}/{method}", data)
    if status >= 300:
        raise RuntimeError(body)
    return json.loads(body)


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
    send(chat_id, f"{greeting}\n\nIsland Download\n发送 magnet、.torrent 或夸克分享链接。\n系统会智能分类，最多同时下载 {MAX_ACTIVE_DOWNLOADS} 个任务。\n也可以直接发送中文问题给 AI 助手。", home_keyboard())


def task_list():
    return json.loads(qbit("/api/v2/torrents/info?tag=islandbot&sort=added_on&reverse=true"))


def save_queue():
    QUEUE_FILE.write_text(json.dumps(QUEUE))


def save_blocked():
    BLOCKED_FILE.write_text(json.dumps(list(BLOCKED)))


def file_size(item):
    if "amount_left" in item:
        return int(item.get("amount_left") or 0)
    return int(item.get("total_size") or item.get("size") or 0)


def has_enough_space(item, free=None):
    """Return whether the next download fits, with a fixed safety reserve."""
    size = file_size(item)
    if size <= 0:
        # A magnet can need a short metadata phase before qBittorrent knows size.
        return True, size, free if free is not None else shutil.disk_usage("/downloads").free
    free = free if free is not None else shutil.disk_usage("/downloads").free
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

    active_hashes = QUEUE[:MAX_ACTIVE_DOWNLOADS]
    # Every later item stays paused, even after a container restart.
    for task_hash in QUEUE[MAX_ACTIVE_DOWNLOADS:]:
        item = task_by_hash.get(task_hash)
        if item and item.get("state") not in {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}:
            qbit_action("pause", task_hash)
    free = shutil.disk_usage("/downloads").free
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
        disk = shutil.disk_usage("/downloads")
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
    if data == "home:add":
        return send(chat_id, "请发送 magnet、夸克分享链接，或上传 .torrent 种子文件。\n系统会自动交给 MoviePilot 智能分类。", [[{"text": "返回主页", "callback_data": "home:home"}]])
    if data == "home:tasks":
        return show_tasks(chat_id)
    if data == "home:completed":
        return show_recent_completed(chat_id)
    if data == "home:server":
        return server_status(chat_id)
    if data.startswith("quarkconfirm:"):
        key = data.split(":", 1)[1]
        pending = QUARK_CONFIRM_PENDING.pop(key, None)
        save_quark_confirm_pending()
        if not pending:
            return send(chat_id, "这个确认已过期，请重新发送分享链接。", home_keyboard())
        if not safe_confirmed_media_title(pending["media_title"]):
            return request_quark_title(
                chat_id,
                pending["url"],
                pending["media_title"],
                "旧识别结果无效，已禁止下载",
            )
        return enqueue_quark_task(chat_id, pending["url"], pending["media_title"])
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
                return request_quark_title(chat_id, selected_url, folder["name"], str(exc))
        if title:
            return confirm_quark_download(chat_id, selected_url, title)
        return request_quark_title(
            chat_id,
            selected_url,
            folder["name"],
            pending.get("title_error"),
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
    text = message.get("text", "").strip()
    title_pending = QUARK_TITLE_PENDING.get(str(chat_id))
    if title_pending:
        if text == "/cancel":
            QUARK_TITLE_PENDING.pop(str(chat_id), None)
            save_quark_title_pending()
            return send(chat_id, "已取消这次夸克下载。", home_keyboard())
        if text and not text.startswith("/") and not is_quark_share(text) and not text.startswith("magnet:"):
            try:
                title = moviepilot_media_title(text)
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
    send(chat_id, ai_reply(text))


def watch_completed():
    global SEEN
    for item in task_list():
        if item.get("progress", 0) < 1 or item["hash"] in SEEN:
            continue
        SEEN.add(item["hash"])
        DONE_FILE.write_text(json.dumps(list(SEEN)))
        send_temporary(OWNER, f"✅ 下载完成\n\n{item.get('name', '')}\n分类：{item.get('category') or '智能分类（MoviePilot）'}\n\nMoviePilot 将自动识别、整理并上传到 Google Drive。")
    run_queue()


def watch_aria2_completed():
    changed = False
    for gid, tracked in list(ARIA2_TRACKED.items()):
        try:
            item = aria2_rpc("aria2.tellStatus", [gid, ["gid", "status", "errorMessage", "files"]])
        except Exception as exc:
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
        elif status == "error":
            send(OWNER, f"⚠️ Aria2 下载失败\n\n{tracked.get('name', aria2_name(item))}\n{item.get('errorMessage') or '请在我的任务中检查。'}")
            ARIA2_TRACKED.pop(gid, None)
            changed = True
    if changed:
        save_aria2_tracked()


while True:
    try:
        updates = telegram("getUpdates", {"offset": OFFSET, "timeout": 25, "allowed_updates": json.dumps(["message", "callback_query"])})
        for update in updates.get("result", []):
            handle(update)
        process_hermes_inbox()
        watch_completed()
        watch_aria2_completed()
        delete_expired_messages()
    except Exception as exc:
        print("error:", exc, flush=True)
        time.sleep(5)
