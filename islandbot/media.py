"""Pure media identity, season, and episode parsing.

This module has no network, database, Docker, or Telegram dependencies.  All
decisions made here are deterministic and covered by unit tests.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".avi", ".ts", ".mov")
_CHINESE_NUMBERS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass(frozen=True)
class MediaIdentity:
    """A user-confirmed MoviePilot/TMDB identity."""

    title: str
    tmdb_id: str
    year: int | None = None
    season: int = 1
    media_type: str = "电视剧"

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("媒体名称不能为空")
        if not re.fullmatch(r"\d{2,9}", str(self.tmdb_id)):
            raise ValueError("TMDB 编号格式不正确")
        if self.season < 1 or self.season > 99:
            raise ValueError("季数必须在 1 到 99 之间")

    @property
    def folder(self) -> str:
        year = f" ({self.year})" if self.year else ""
        return f"{self.title}{year} {{tmdb-{self.tmdb_id}}}"

    @property
    def task_label(self) -> str:
        if self.media_type == "电影":
            return self.folder
        return f"{self.folder} S{self.season:02d}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MediaIdentity":
        return cls(
            title=str(value["title"]),
            tmdb_id=str(value["tmdb_id"]),
            year=int(value["year"]) if value.get("year") else None,
            season=int(value.get("season") or 1),
            media_type=str(value.get("media_type") or "电视剧"),
        )


def clean_title(value: str) -> str:
    """Return a safe human-readable title without release-site decoration."""

    name = value.strip()
    name = re.sub(r"^已更新\s*[：:]?\s*", "", name)
    name = re.sub(r"^(?:名称|片名|剧名)\s*[：:]\s*", "", name)
    name = re.sub(r"^[^\w\u4e00-\u9fff]+", "", name)
    name = re.sub(r'[\\/:*?"<>|]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        raise ValueError("剧名不能为空")
    return name[:120]


def checked_query(value: str) -> str:
    """Reject placeholder and technical-only strings before fuzzy lookup."""

    original = clean_title(value)
    probe = re.sub(r"[（(]\d{4}[)）]", " ", original)
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", probe))
    english_words = re.findall(r"[A-Za-z0-9]{3,}", probe)
    if len(chinese) >= 2 or english_words:
        return original
    raise ValueError(f"无法确认媒体标题“{original}”")


def chinese_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if value in _CHINESE_NUMBERS:
        return _CHINESE_NUMBERS[value]
    if len(value) == 2 and value.startswith("十") and value[1] in _CHINESE_NUMBERS:
        return 10 + _CHINESE_NUMBERS[value[1]]
    if len(value) == 2 and value.endswith("十") and value[0] in _CHINESE_NUMBERS:
        return _CHINESE_NUMBERS[value[0]] * 10
    return None


def season_number(value: str, default: int = 1) -> int:
    """Extract a season from Chinese/English release text."""

    patterns = (
        r"(?i)\bS(?:eason)?[ ._-]*(\d{1,2})(?:\b|E\d)",
        r"第\s*([一二三四五六七八九十\d]{1,3})\s*季",
        r"([一二三四五六七八九十\d]{1,3})\s*季",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        parsed = chinese_number(match.group(1))
        if parsed and 1 <= parsed <= 99:
            return parsed
    return default


def explicit_episode_key(value: str) -> str | None:
    match = re.search(r"(?i)(?:^|[^A-Z0-9])S(\d{1,2})E(\d{1,3})(?:[^0-9]|$)", value)
    if not match:
        return None
    return f"S{int(match.group(1)):02d}E{int(match.group(2)):03d}"


def episode_key(value: str, default_season: int | None = None) -> str | None:
    """Extract one episode key, including bare ``01.mkv`` when season is known."""

    explicit = explicit_episode_key(value)
    if explicit:
        return explicit
    if default_season is None:
        return None
    stem = Path(value).stem
    patterns = (
        r"(?i)(?:^|[^A-Z0-9])E(?:P)?[ ._-]*(\d{1,3})(?:[^0-9]|$)",
        r"第\s*(\d{1,3})\s*集",
        r"^\s*0*(\d{1,3})\s*$",
    )
    for pattern in patterns:
        match = re.search(pattern, stem)
        if match:
            return f"S{default_season:02d}E{int(match.group(1)):03d}"
    return None


def episode_keys(value: str, default_season: int | None = None) -> set[str]:
    keys = {
        f"S{int(season):02d}E{int(episode):03d}"
        for season, episode in re.findall(r"(?i)S(\d{1,2})E(\d{1,3})", value)
    }
    if not keys:
        key = episode_key(value, default_season)
        if key:
            keys.add(key)
    return keys


def identity_words(value: str) -> tuple[str, set[str]]:
    """Normalize a release label for conservative title comparison."""

    text = re.sub(r"\{tmdb-\d+\}", " ", value, flags=re.IGNORECASE)
    text = re.sub(r"[（(]\d{4}[)）]", " ", text)
    text = re.sub(r"第\s*[一二三四五六七八九十\d]+\s*季", " ", text)
    text = re.sub(r"(?i)\b(?:season|s)\s*\d+\b", " ", text)
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    english = set(re.findall(r"[a-z0-9]{3,}", text.lower()))
    return chinese, english


def identities_match(source: str, candidate: str) -> bool:
    source_cn, source_en = identity_words(source)
    candidate_cn, candidate_en = identity_words(candidate)
    if len(source_cn) >= 2:
        return len(candidate_cn) >= 2 and (
            source_cn in candidate_cn or candidate_cn in source_cn
        )
    return bool(source_en & candidate_en)


def parse_identity_label(value: str) -> MediaIdentity | None:
    """Parse the canonical label used internally by the bot."""

    match = re.fullmatch(
        r"(.+?)(?:\s+[（(](\d{4})[)）])?\s+\{tmdb-(\d+)\}"
        r"(?:\s+S(\d{1,2}))?",
        value.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None
    return MediaIdentity(
        title=clean_title(match.group(1)),
        year=int(match.group(2)) if match.group(2) else None,
        tmdb_id=match.group(3),
        season=int(match.group(4) or 1),
        media_type="电视剧" if match.group(4) else "电影",
    )


def alias_key(value: str) -> str:
    """Stable cache key that keeps season identity but drops release noise."""

    text = clean_title(value).lower()
    season = season_number(text)
    chinese, english = identity_words(text)
    core = chinese or "-".join(sorted(english))
    return f"{core}|s{season:02d}"
