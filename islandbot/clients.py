"""Adapters for external services used by the bot."""

from __future__ import annotations

import http.cookiejar
import json
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .config import Settings
from .http import Response, fetch, require_ok


class TelegramClient:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        response = require_ok(fetch(f"{self.base}/{method}", form=data or {}), "Telegram 请求")
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description") or "Telegram 请求失败")
        return payload


class EmbyClient:
    """Create owner-managed, playback-only Emby users."""

    VIEWER_POLICY = {
        "IsAdministrator": False,
        "IsHidden": False,
        "IsDisabled": False,
        "EnableUserPreferenceAccess": False,
        "EnableRemoteControlOfOtherUsers": False,
        "EnableSharedDeviceControl": False,
        "EnableRemoteAccess": True,
        "EnableLiveTvManagement": False,
        "EnableLiveTvAccess": False,
        "EnableContentDeletion": False,
        "EnableContentDeletionFromFolders": [],
        "EnableContentDownloading": False,
        "EnableSubtitleDownloading": False,
        "EnableSubtitleManagement": False,
        "EnableSyncTranscoding": False,
        "EnableMediaConversion": False,
        "EnableMediaPlayback": True,
        "EnableAudioPlaybackTranscoding": True,
        "EnableVideoPlaybackTranscoding": True,
        "EnablePlaybackRemuxing": True,
        "EnableAllDevices": True,
        "EnableAllFolders": True,
        "EnableAllChannels": False,
        "EnablePublicSharing": False,
        "AllowCameraUpload": False,
        "AllowSharingPersonalItems": False,
    }

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _request(
        self,
        path: str,
        *,
        method: str | None = None,
        json_body: Any = None,
        action: str,
    ) -> Response:
        if not self.api_key:
            raise RuntimeError("Emby API 密钥尚未配置")
        return require_ok(
            fetch(
                f"{self.base_url}/emby{path}",
                method=method,
                json_body=json_body,
                headers={"X-Emby-Token": self.api_key, "Accept": "application/json"},
                timeout=20,
            ),
            action,
        )

    def users(self) -> list[dict[str, Any]]:
        payload = self._request("/Users", action="读取 Emby 用户").json()
        return payload if isinstance(payload, list) else []

    def create_viewer(self, username: str, password: str) -> dict[str, str]:
        username = username.strip()
        if not username or len(username) > 32:
            raise RuntimeError("用户名长度必须为 1 到 32 个字符")
        if any(character in username for character in "/\\\r\n\t"):
            raise RuntimeError("用户名不能包含斜杠、反斜杠或换行")
        if any(
            str(user.get("Name", "")).casefold() == username.casefold()
            for user in self.users()
        ):
            raise RuntimeError(f"Emby 用户“{username}”已存在")

        created = self._request(
            "/Users/New",
            json_body={"Name": username},
            action="创建 Emby 用户",
        ).json()
        user_id = str(created.get("Id") or "")
        if not user_id:
            raise RuntimeError("Emby 创建用户后没有返回用户 ID")

        try:
            self._request(
                f"/Users/{urllib.parse.quote(user_id)}/Password",
                json_body={
                    "Id": user_id,
                    "CurrentPw": "",
                    "NewPw": password,
                    "ResetPassword": False,
                },
                action="设置 Emby 密码",
            )
            policy = dict(created.get("Policy") or {})
            policy.update(self.VIEWER_POLICY)
            self._request(
                f"/Users/{urllib.parse.quote(user_id)}/Policy",
                json_body=policy,
                action="设置 Emby 观看权限",
            )
        except Exception:
            fetch(
                f"{self.base_url}/emby/Users/{urllib.parse.quote(user_id)}",
                method="DELETE",
                headers={"X-Emby-Token": self.api_key},
                timeout=20,
            )
            raise
        return {"id": user_id, "username": username}


class Aria2Client:
    def __init__(self, url: str, secret: str):
        self.url = url
        self.secret = secret

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        if not self.secret:
            raise RuntimeError("Aria2 密钥尚未配置")
        arguments = [f"token:{self.secret}", *(params or [])]
        response = require_ok(
            fetch(
                self.url,
                json_body={
                    "jsonrpc": "2.0",
                    "id": "island-download-bot",
                    "method": method,
                    "params": arguments,
                },
                timeout=20,
            ),
            "Aria2 RPC",
        )
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(payload["error"].get("message") or "Aria2 RPC 失败")
        return payload.get("result")

    def recent(self) -> list[dict[str, Any]]:
        keys = [
            "gid",
            "status",
            "totalLength",
            "completedLength",
            "downloadSpeed",
            "errorMessage",
            "files",
        ]
        return (
            self.call("aria2.tellActive", [keys])
            + self.call("aria2.tellWaiting", [0, 100, keys])
            + self.call("aria2.tellStopped", [0, 100, keys])
        )


class QasClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url
        self.username = username
        self.password = password

    def request(self, path: str, payload: Any = None, timeout: int = 45) -> Response:
        if not self.username or not self.password:
            raise RuntimeError("夸克模块尚未配置 QAS 账号")
        last_error = ""
        for attempt in range(2):
            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar)
            )
            login = fetch(
                f"{self.base_url}/login",
                form={"username": self.username, "password": self.password},
                timeout=20,
                opener=opener,
            )
            if login.status >= 300:
                last_error = login.text
                continue
            response = fetch(
                f"{self.base_url}{path}",
                json_body=payload,
                timeout=timeout,
                opener=opener,
            )
            if response.status != 599:
                return response
            last_error = response.text
            if attempt == 0:
                time.sleep(2)
        raise RuntimeError(f"夸克解析超时：{last_error[:120]}")

    def share_detail(self, share_url: str, stoken: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"shareurl": share_url}
        if stoken:
            payload["stoken"] = stoken
        response = require_ok(self.request("/get_share_detail", payload), "读取夸克目录")
        result = response.json()
        if not result.get("success"):
            error = (
                result.get("data", {}).get("error")
                or result.get("message")
                or "夸克链接解析失败"
            )
            raise RuntimeError(error)
        return result.get("data") or {}


class QBitClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url
        self.username = username
        self.password = password
        self.cookie = ""

    def login(self) -> None:
        last_status = 0
        for attempt in range(3):
            response = fetch(
                f"{self.base_url}/api/v2/auth/login",
                form={"username": self.username, "password": self.password},
            )
            last_status = response.status
            if response.status in {200, 204}:
                self.cookie = (
                    response.headers.get("Set-Cookie", "").split(";", 1)[0]
                )
                if self.cookie:
                    return
            if attempt < 2:
                time.sleep(attempt + 1)
        if last_status in {401, 403}:
            raise RuntimeError("qBittorrent 账号或密码被拒绝")
        raise RuntimeError(f"qBittorrent 登录失败（HTTP {last_status}）")

    def request(
        self,
        path: str,
        *,
        form: dict[str, Any] | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        if not self.cookie:
            self.login()
        request_headers = {"Cookie": self.cookie, **(headers or {})}
        response = fetch(
            f"{self.base_url}{path}",
            form=form,
            body=body,
            headers=request_headers,
        )
        if response.status in {401, 403}:
            self.cookie = ""
            self.login()
            request_headers["Cookie"] = self.cookie
            response = fetch(
                f"{self.base_url}{path}",
                form=form,
                body=body,
                headers=request_headers,
            )
        return require_ok(response, "qBittorrent 请求")

    def action(self, action: str, hashes: str, delete_files: bool = False) -> str:
        endpoint = {"pause": "stop", "resume": "start"}.get(action, action)
        form: dict[str, Any] = {"hashes": hashes}
        if delete_files:
            form["deleteFiles"] = "true"
        return self.request(f"/api/v2/torrents/{endpoint}", form=form).text

    def files(self, torrent_hash: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"hash": torrent_hash})
        payload = self.request(f"/api/v2/torrents/files?{query}").json()
        return payload if isinstance(payload, list) else []

    def add_torrent(self, filename: str, content: bytes, category: str) -> None:
        boundary = f"----IslandDownload{uuid.uuid4().hex}"
        body = bytearray()

        def field(name: str, value: str) -> None:
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            )
            body.extend(value.encode())
            body.extend(b"\r\n")

        safe_name = Path(filename).name.replace('"', "_")
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            (
                'Content-Disposition: form-data; name="torrents"; '
                f'filename="{safe_name}"\r\n'
            ).encode()
        )
        body.extend(b"Content-Type: application/x-bittorrent\r\n\r\n")
        body.extend(content)
        body.extend(b"\r\n")
        field("tags", "islandbot")
        field("autoTMM", "false")
        field("stopCondition", "MetadataReceived")
        if category != "__auto__":
            field("category", category)
        body.extend(f"--{boundary}--\r\n".encode())
        self.request(
            "/api/v2/torrents/add",
            body=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )


class MoviePilotClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.token = token

    def _require_token(self) -> None:
        if not self.token:
            raise RuntimeError("MoviePilot API_TOKEN 尚未连接")

    def recognize(self, title: str) -> dict[str, Any] | None:
        self._require_token()
        query = urllib.parse.urlencode({"title": title, "token": self.token})
        response = require_ok(
            fetch(f"{self.base_url}/api/v1/media/recognize2?{query}"),
            "MoviePilot 识别",
        )
        payload = response.json()
        media = payload.get("media_info") if isinstance(payload, dict) else None
        return media if isinstance(media, dict) else None

    def tmdb(self, tmdb_id: str, type_name: str, title: str = "") -> dict[str, Any] | None:
        self._require_token()
        query = urllib.parse.urlencode(
            {"type_name": type_name, "title": title, "token": self.token}
        )
        response = fetch(
            f"{self.base_url}/api/v1/media/tmdb:{tmdb_id}?{query}",
            timeout=30,
        )
        if response.status in {404, 422}:
            return None
        require_ok(response, f"MoviePilot 查询 TMDB {tmdb_id}")
        payload = response.json()
        return payload if isinstance(payload, dict) else None


def clients(
    settings: Settings,
) -> tuple[
    TelegramClient,
    Aria2Client,
    QasClient,
    QBitClient,
    MoviePilotClient,
]:
    return (
        TelegramClient(settings.bot_token),
        Aria2Client(settings.aria2_url, settings.aria2_secret),
        QasClient(settings.qas_url, settings.qas_username, settings.qas_password),
        QBitClient(
            settings.qbit_url,
            settings.qbit_username,
            settings.qbit_password,
        ),
        MoviePilotClient(settings.moviepilot_url, settings.moviepilot_token),
    )
