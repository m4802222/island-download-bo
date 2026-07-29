"""Library indexing and missing-episode decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .media import MediaIdentity, VIDEO_EXTENSIONS, episode_key, episode_keys


@dataclass(frozen=True)
class MissingPlan:
    total: int
    skipped: int
    missing_names: tuple[str, ...]

    @property
    def remaining(self) -> int:
        return len(self.missing_names)

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.remaining == 0

    @property
    def include_regex(self) -> str:
        if not self.skipped:
            return ""
        return r"^(?:" + "|".join(re.escape(name) for name in self.missing_names) + r")$"


def indexed_episodes(paths: Iterable[str], identity: MediaIdentity) -> set[str]:
    """Index only paths belonging to the exact canonical show and season."""

    title = identity.title
    season_dir = f"Season {identity.season}"
    result: set[str] = set()
    for path in paths:
        components = path.split("/")
        if not any(component == title or component.startswith(f"{title} (") for component in components):
            continue
        if season_dir not in components:
            continue
        result.update(episode_keys(path, identity.season))
    return result


def matching_paths(paths: Iterable[str], identity: MediaIdentity) -> tuple[str, ...]:
    """Return paths belonging to the exact canonical title and season."""

    season_dir = f"Season {identity.season}"
    matched = []
    for path in paths:
        components = path.split("/")
        title_match = any(
            component == identity.title or component.startswith(f"{identity.title} (")
            for component in components
        )
        if not title_match:
            continue
        if identity.media_type != "电影" and season_dir not in components:
            continue
        matched.append(path)
    return tuple(matched)


def missing_plan(
    source_files: Iterable[str],
    existing_paths: Iterable[str],
    identity: MediaIdentity,
) -> MissingPlan:
    videos = tuple(
        name for name in source_files if name.lower().endswith(VIDEO_EXTENSIONS)
    )
    existing_paths = tuple(existing_paths)
    existing = indexed_episodes(existing_paths, identity)
    keyed = [(name, episode_key(name, identity.season)) for name in videos]
    if (
        identity.media_type == "电影"
        and videos
        and not any(key for _, key in keyed)
        and matching_paths(existing_paths, identity)
    ):
        return MissingPlan(total=len(videos), skipped=len(videos), missing_names=())
    missing = tuple(name for name, key in keyed if key is None or key not in existing)
    return MissingPlan(
        total=len(videos),
        skipped=len(videos) - len(missing),
        missing_names=missing,
    )
