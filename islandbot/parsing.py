"""Pure parsers for Telegram resource posts and download links."""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from .media import clean_title


QUARK_URL_RE = re.compile(
    r"https?://pan\.quark\.cn/s/[A-Za-z0-9]+(?:\?[^\s]*)?",
    re.IGNORECASE,
)
MAGNET_RE = re.compile(
    r"magnet:\?xt=urn:btih:[A-Za-z0-9]+[^\s]*",
    re.IGNORECASE,
)
TRAILING_PUNCTUATION = ".,，。;；)"


def is_quark_share(text: str) -> bool:
    parsed = urllib.parse.urlparse(text)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.lower() == "pan.quark.cn"
        and parsed.path.startswith("/s/")
    )


def extract_quark_share(text: str) -> str | None:
    match = QUARK_URL_RE.search(text or "")
    return match.group(0).rstrip(TRAILING_PUNCTUATION) if match else None


def extract_magnet(text: str) -> str | None:
    match = MAGNET_RE.search(text or "")
    return match.group(0).rstrip(TRAILING_PUNCTUATION) if match else None


def message_text_with_links(message: dict[str, Any]) -> str:
    """Include hidden Telegram text-link entity URLs."""
    text = (message.get("text") or message.get("caption") or "").strip()
    entities = message.get("entities") or message.get("caption_entities") or []
    urls = [item["url"] for item in entities if item.get("url")]
    return "\n".join([text, *urls]).strip()


def extract_post_title(text: str) -> str | None:
    """Extract only the explicit title/year line, never descriptions."""
    ignored_labels = ("发布时间", "资源大小", "链接状态", "投稿ID")
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not re.search(r"[\u4e00-\u9fff]", line):
            continue
        match = re.search(r"(.+?[（(]\d{4}[)）])", line)
        if not match:
            continue
        title = re.sub(
            r"^[^\w\u4e00-\u9fff]+",
            "",
            match.group(1),
        ).strip()
        if any(label in title for label in ignored_labels):
            continue
        try:
            return clean_title(title)
        except RuntimeError:
            return None
    return None
