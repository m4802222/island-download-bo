"""Shared media category rules derived from MoviePilot configuration."""

from __future__ import annotations

import posixpath
from pathlib import Path


CANONICAL_CATEGORIES = (
    "华语电影",
    "日韩电影",
    "海外电影",
    "华语动漫",
    "日韩动漫",
    "海外动漫",
    "华语剧集",
    "日韩剧集",
    "海外剧集",
)

LEGACY_QBIT_CATEGORIES = ("欧美电影", "欧美动漫", "欧美剧集")


def load_moviepilot_categories(path: Path, *, required: bool = True) -> list[str]:
    """Read the movie/tv child keys from MoviePilot's small category YAML."""

    if not path.is_file():
        if required:
            raise RuntimeError(f"MoviePilot 分类文件不可读取：{path}")
        return list(CANONICAL_CATEGORIES)

    section = ""
    found: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            key = line.split(":", 1)[0].strip()
            section = key if key in {"movie", "tv"} else ""
            continue
        if section and line.startswith("  ") and not line.startswith("    "):
            stripped = line.strip()
            if stripped.endswith(":"):
                found.append(stripped[:-1].strip())

    duplicates = sorted({name for name in found if found.count(name) > 1})
    actual = set(found)
    expected = set(CANONICAL_CATEGORIES)
    if duplicates or actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RuntimeError(
            "MoviePilot 分类不是约定的九分类："
            f"缺少={missing or '无'}，多出={extra or '无'}，重复={duplicates or '无'}"
        )
    return [name for name in CANONICAL_CATEGORIES if name in actual]


def qbit_category_paths(categories: list[str], inbox: str) -> dict[str, str]:
    root = posixpath.dirname(inbox.rstrip("/"))
    if not root:
        raise RuntimeError("qBittorrent 分类根目录无效")
    return {name: posixpath.join(root, name) for name in categories}


def download_path(category: str, inbox: str, categories: list[str]) -> str:
    if category == "__auto__":
        return inbox
    paths = qbit_category_paths(categories, inbox)
    if category not in paths:
        raise RuntimeError(f"未知下载分类：{category}")
    return paths[category]
