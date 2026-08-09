"""Conservative MoviePilot/TMDB identity resolution."""

from __future__ import annotations

import re
from dataclasses import replace

from .clients import MoviePilotClient
from .media import (
    MediaIdentity,
    checked_query,
    identities_match,
    season_number,
)
from .storage import IdentityStore


class ResolutionError(RuntimeError):
    pass


class MediaResolver:
    def __init__(self, moviepilot: MoviePilotClient, identities: IdentityStore):
        self.moviepilot = moviepilot
        self.identities = identities

    @staticmethod
    def _identity(media: dict, season: int, media_type: str) -> MediaIdentity | None:
        tmdb_id = media.get("tmdb_id")
        title = media.get("title")
        if not tmdb_id or not title:
            return None
        year = media.get("year") or media.get("release_year")
        return MediaIdentity(
            title=str(title).strip(),
            tmdb_id=str(tmdb_id),
            year=int(year) if str(year or "").isdigit() else None,
            season=season,
            media_type=media_type,
        )

    @staticmethod
    def _media_type(media: dict, source: str) -> str:
        value = str(
            media.get("type_name")
            or media.get("media_type")
            or media.get("type")
            or ""
        ).lower()
        if "movie" in value or "电影" in value:
            return "电影"
        if "tv" in value or "电视剧" in value:
            return "电视剧"
        return "电视剧" if re.search(r"季|集|剧|动漫|动画|S\d", source, re.I) else "电影"

    def automatic(self, source_title: str) -> MediaIdentity:
        source = checked_query(source_title)
        requested_season = season_number(source)
        remembered = self.identities.get(source)
        if remembered:
            return replace(remembered, season=requested_season)

        year_match = re.search(r"[（(](\d{4})[)）]", source)
        requested_year = int(year_match.group(1)) if year_match else None
        without_year = re.sub(r"\s*[（(]\d{4}[)）]\s*", " ", source).strip()
        without_season = re.sub(
            r"第\s*[一二三四五六七八九十\d]+\s*季|(?i:\bS(?:eason)?\s*\d+\b)",
            " ",
            without_year,
        ).strip()
        queries = list(dict.fromkeys(filter(None, (source, without_year, without_season))))

        candidates: list[MediaIdentity] = []
        for query in queries:
            media = self.moviepilot.recognize(query)
            if not media:
                continue
            identity = self._identity(
                media,
                requested_season,
                self._media_type(media, source),
            )
            if not identity or not identities_match(source, identity.folder):
                continue
            if requested_year and identity.year and requested_year != identity.year:
                continue
            if identity not in candidates:
                candidates.append(identity)
        if requested_season > 1:
            parent_candidates = [
                item
                for item in candidates
                if season_number(item.title, default=0) == 0
            ]
            if parent_candidates:
                return parent_candidates[0]
            # A TMDB item literally named “作品 第2季” is often a duplicate
            # standalone show. Never silently bind it as the series parent.
            if candidates:
                raise ResolutionError(
                    f"“{source}”存在独立季条目，无法安全确定主剧 TMDB，未开始下载。\n"
                    "请回复主剧 TMDB 编号和季数，例如：259231 第2季"
                )
        if candidates:
            return candidates[0]
        raise ResolutionError(
            f"MoviePilot 未确认“{source}”的 TMDB 信息，未开始下载。\n"
            "请回复 TMDB 编号；季数可一起写，例如：259231 第2季"
        )

    def manual(
        self,
        source_title: str,
        tmdb_id: str,
        *,
        forced_type: str | None = None,
        season: int | None = None,
    ) -> MediaIdentity:
        if not re.fullmatch(r"\d{2,9}", str(tmdb_id)):
            raise ResolutionError("TMDB 编号格式不正确")
        requested_season = season or season_number(source_title)
        type_names = [forced_type] if forced_type else ["电视剧", "电影"]
        candidates: list[MediaIdentity] = []
        for type_name in type_names:
            media = self.moviepilot.tmdb(str(tmdb_id), type_name, source_title)
            if not media:
                continue
            identity = self._identity(media, requested_season, type_name)
            if identity and identity.tmdb_id == str(tmdb_id):
                candidates.append(identity)
        if not candidates:
            raise ResolutionError(f"MoviePilot 找不到 TMDB {tmdb_id}")
        if len(candidates) > 1 and not forced_type:
            matching = [
                candidate
                for candidate in candidates
                if identities_match(source_title, candidate.folder)
            ]
            if len(matching) == 1:
                candidates = matching
            elif re.search(r"季|集|剧|动漫|动画|S\d", source_title, re.IGNORECASE):
                candidates = [item for item in candidates if item.media_type == "电视剧"]
            else:
                raise ResolutionError(
                    f"TMDB {tmdb_id} 同时存在电影和电视剧，请回复“电视剧 {tmdb_id}”或“电影 {tmdb_id}”"
                )
        identity = candidates[0]
        remember_source = source_title
        if season is not None:
            remember_source = f"{source_title} S{requested_season:02d}"
        self.identities.remember(remember_source, identity)
        return identity

    def reply(self, source_title: str, text: str) -> MediaIdentity:
        value = text.strip()
        match = re.fullmatch(
            r"(?:(电影|电视剧)\s*)?(?:tmdb\s*[:：#-]?\s*)?(\d{2,9})"
            r"(?:\s*(?:第)?([一二三四五六七八九十\d]+)季)?",
            value,
            re.IGNORECASE,
        )
        if match:
            requested = season_number(f"第{match.group(3)}季") if match.group(3) else None
            return self.manual(
                source_title,
                match.group(2),
                forced_type=match.group(1),
                season=requested,
            )
        return self.automatic(value)
