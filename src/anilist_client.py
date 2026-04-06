"""AniList GraphQL API client for fetching user anime lists."""

import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import AniListConfig

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://graphql.anilist.co"

# AniList statuses we care about
ANILIST_STATUS_WATCHING = "CURRENT"
ANILIST_STATUS_PLAN_TO_WATCH = "PLANNING"
ANILIST_STATUS_COMPLETED = "COMPLETED"
ANILIST_STATUS_PAUSED = "PAUSED"
ANILIST_STATUS_DROPPED = "DROPPED"

_LIST_QUERY = """
query ($userName: String, $status: MediaListStatus) {
  MediaListCollection(userName: $userName, type: ANIME, status: $status) {
    lists {
      entries {
        media {
          id
          idMal
          title {
            romaji
            english
          }
          seasonYear
          format
          episodes
        }
      }
    }
  }
}
"""

_MEDIA_RELATIONS_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    idMal
    episodes
    format
    seasonYear
    startDate {
      year
      month
      day
    }
    title {
      romaji
      english
    }
    relations {
      edges {
        relationType
        node {
          id
          idMal
          episodes
          format
          seasonYear
          startDate {
            year
            month
            day
          }
          title {
            romaji
            english
          }
        }
      }
    }
  }
}
"""

_ROOT_FORMAT_PRIORITY = {
    "TV": 0,
    "TV_SHORT": 0,
    "ONA": 1,
    "OVA": 2,
    "SPECIAL": 3,
    "MOVIE": 4,
}


class AniListClient:
    """Client for the AniList GraphQL API (public, no auth required for public lists)."""

    def __init__(self, config: AniListConfig):
        self._config = config
        self._session = self._build_session()
        self._root_cache: dict[int, dict | None] = {}
        self._root_context_cache: dict[int, dict | None] = {}

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        if self._config.access_token:
            session.headers["Authorization"] = f"Bearer {self._config.access_token}"

        retry = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503],
            allowed_methods=["POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        return session

    def _query(self, query: str, variables: dict) -> dict | None:
        logger.debug("AniList query variables=%s", variables)
        resp = self._session.post(GRAPHQL_URL, json={"query": query, "variables": variables}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            logger.error("AniList GraphQL errors: %s", data["errors"])
            return None
        return data.get("data")

    def get_watching(self) -> list[dict]:
        """Fetch anime with status CURRENT (watching)."""
        return self.get_status(ANILIST_STATUS_WATCHING)

    def get_plan_to_watch(self) -> list[dict]:
        """Fetch anime with status PLANNING (plan to watch)."""
        return self.get_status(ANILIST_STATUS_PLAN_TO_WATCH)

    def get_status(self, status: str) -> list[dict]:
        """Fetch anime for any supported AniList status."""
        return self._fetch_list(status)

    def _fetch_list(self, status: str) -> list[dict]:
        data = self._query(_LIST_QUERY, {"userName": self._config.username, "status": status})
        if not data:
            return []

        collection = data.get("MediaListCollection")
        if not collection:
            return []

        items = []
        for lst in collection.get("lists", []):
            for entry in lst.get("entries", []):
                normalized = self._normalize(entry.get("media", {}))
                if normalized:
                    items.append(normalized)

        logger.info("AniList: fetched %d anime for status '%s'", len(items), status)
        return items

    def _normalize(self, media: dict) -> dict | None:
        if not media:
            return None

        anilist_id = media.get("id")
        mal_id = media.get("idMal")
        title = self._media_title(media)
        ids = {
            "anilist": anilist_id,
            "mal": mal_id,
        }

        root_media = self._get_root_media(anilist_id) if anilist_id else None
        root_anilist_id = None
        root_mal_id = None
        root_title = None
        if root_media and root_media.get("id") != anilist_id:
            root_anilist_id = root_media.get("id")
            root_mal_id = root_media.get("idMal")
            root_title = self._media_title(root_media)
            ids["root_anilist"] = root_anilist_id
            ids["root_mal"] = root_mal_id

        return {
            "title": title,
            "year": media.get("seasonYear"),
            "media_type": "tv",
            "simkl_type": "anime",
            "imdb_id": None,
            "tmdb_id": None,
            "mal_id": str(mal_id) if mal_id else None,
            "anilist_id": str(anilist_id) if anilist_id else None,
            "root_mal_id": str(root_mal_id) if root_mal_id else None,
            "root_anilist_id": str(root_anilist_id) if root_anilist_id else None,
            "root_title": root_title,
            "anidb_id": None,
            "tvdb_id": None,
            "ids": ids,
            "status": None,
            "added_at": None,
        }

    def _get_root_media(self, media_id: int) -> dict | None:
        context = self._get_root_context(media_id)
        return (context or {}).get("root")

    def _get_root_context(self, media_id: int) -> dict | None:
        if media_id in self._root_context_cache:
            return self._root_context_cache[media_id]

        if media_id in self._root_cache:
            root = self._root_cache[media_id]
            context = {"root": root, "episode_offset": 0}
            self._root_context_cache[media_id] = context
            return context

        seen: set[int] = set()
        chain: list[dict] = []
        current_id = media_id

        while current_id and current_id not in seen:
            seen.add(current_id)
            data = self._query(_MEDIA_RELATIONS_QUERY, {"id": current_id})
            media = (data or {}).get("Media")
            if not media:
                break

            chain.append(media)
            prequel = self._pick_prequel(media.get("relations", {}).get("edges", []))
            if not prequel:
                break
            current_id = prequel.get("id")

        root = self._pick_root_candidate(chain)
        chronological_chain = list(reversed(chain))
        running_offset = 0
        context_by_id: dict[int, dict] = {}
        for media in chronological_chain:
            candidate_id = media.get("id")
            if candidate_id:
                context_by_id[int(candidate_id)] = {
                    "root": root,
                    "episode_offset": running_offset,
                }
            try:
                running_offset += int(media.get("episodes") or 0)
            except (TypeError, ValueError):
                pass
        for candidate in chain:
            candidate_id = candidate.get("id")
            if candidate_id:
                self._root_cache[int(candidate_id)] = root
                self._root_context_cache[int(candidate_id)] = context_by_id.get(int(candidate_id), {"root": root, "episode_offset": 0})
        self._root_cache[media_id] = root
        self._root_context_cache[media_id] = context_by_id.get(media_id, {"root": root, "episode_offset": 0})
        return self._root_context_cache[media_id]

    @classmethod
    def _pick_prequel(cls, edges: list[dict]) -> dict | None:
        prequels = [
            edge.get("node")
            for edge in edges
            if edge.get("relationType") == "PREQUEL" and edge.get("node")
        ]
        if not prequels:
            return None
        return min(prequels, key=cls._media_sort_key)

    @classmethod
    def _pick_root_candidate(cls, chain: list[dict]) -> dict | None:
        if not chain:
            return None
        return min(chain, key=cls._media_sort_key)

    @classmethod
    def _media_sort_key(cls, media: dict) -> tuple:
        start_date = media.get("startDate") or {}
        season_year = media.get("seasonYear") or 9999
        return (
            _ROOT_FORMAT_PRIORITY.get(media.get("format"), 9),
            start_date.get("year") or season_year,
            start_date.get("month") or 99,
            start_date.get("day") or 99,
            media.get("id") or 0,
        )

    @staticmethod
    def _media_title(media: dict) -> str:
        titles = media.get("title", {})
        return titles.get("english") or titles.get("romaji") or "Unknown"
