"""Hermes gateway plugin: deterministically route owner's media links."""

import os
import re
import subprocess
import urllib.parse
import urllib.request

OWNER = "746833285"
BACKEND = "/usr/local/bin/island-media"
QUARK = re.compile(r"https?://pan[.]quark[.]cn/s/[A-Za-z0-9_-]+")
MAGNET = re.compile(r"magnet:[^\s<>]+", re.I)


def value(obj, name):
    return getattr(obj, name, None) if obj is not None else None


def source(event):
    return value(event, "source")


def chat_id(event):
    return value(source(event), "chat_id") or value(event, "chat_id")


def user_id(event):
    return value(source(event), "user_id") or value(event, "user_id")


def title_from(text, url):
    rows = [row.strip() for row in text.split(url, 1)[0].splitlines() if row.strip()]
    ignored = ("原帖入口", "投稿ID", "发布时间", "资源大小", "标签", "链接状态", "对应的资源链接")
    for row in rows:
        if not any(item in row for item in ignored) and "http" not in row:
            return row[:160]
    return ""


def reply(chat, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token or not chat:
        return
    data = urllib.parse.urlencode({"chat_id": str(chat), "text": text}).encode()
    try:
        urllib.request.urlopen(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            data,
            timeout=12,
        ).read()
    except Exception:
        pass


def route(event, **kwargs):
    del kwargs
    text = value(event, "text") or ""
    if str(user_id(event)) != OWNER:
        return None
    match, kind = QUARK.search(text), "quark"
    if not match:
        match, kind = MAGNET.search(text), "magnet"
    if not match:
        return None
    url = match.group(0)
    title = title_from(text, url)
    args = [BACKEND, "submit", kind, url] + ([title] if title else [])
    try:
        completed = subprocess.run(args, text=True, capture_output=True, timeout=25)
        if completed.returncode == 0:
            kind_name = "夸克" if kind == "quark" else "磁力"
            media = "媒体：" + title if title else "正在识别媒体信息"
            reply(chat_id(event), "已自动提交" + kind_name + "下载。\n" + media + "\n可发送：下载状态")
        else:
            reply(chat_id(event), "自动提交失败：" + (completed.stderr or completed.stdout or "未知错误")[:300])
    except Exception as exc:
        reply(chat_id(event), "自动提交失败：" + str(exc)[:300])
    return {"action": "skip", "reason": "island-media-auto-routed"}


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", route)
