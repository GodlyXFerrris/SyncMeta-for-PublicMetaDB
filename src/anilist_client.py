"""AniList GraphQL API client for fetching user anime lists."""

import logging
import threading
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import AniListConfig

logger = logging.getLogger(__name__)

# Module-level shared cache for AniList root-chain data.
# AniList prequel chains are global facts (not user-specific), so all
# AniListClient instances across all profiles share the same cache.
# This avoids re-fetching the same chain data per user.
_SHARED_ROOT_CACHE: dict[int, dict | None] = {}
_SHARED_ROOT_CONTEXT_CACHE: dict[int, dict | None] = {}
_SHARED_CACHE_LOCK = threading.Lock()

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

_MAL_TO_ANILIST_QUERY = """
query ($idMal: Int) {
  Media(idMal: $idMal, type: ANIME) {
    id
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
        # Point to the module-level shared caches so all instances benefit
        # from chain data already fetched by another user's sync.
        self._root_cache = _SHARED_ROOT_CACHE
        self._root_context_cache = _SHARED_ROOT_CONTEXT_CACHE

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
            # 429 is handled manually in _query to honour the Retry-After header.
            status_forcelist=[500, 502, 503],
            allowed_methods=["POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        return session

    def _query(self, query: str, variables: dict, _retries: int = 3) -> dict | None:
        logger.debug("AniList query variables=%s", variables)
        try:
            resp = self._session.post(GRAPHQL_URL, json={"query": query, "variables": variables}, timeout=30)
            if resp.status_code == 429 and _retries > 0:
                # Honour the server-supplied Retry-After (seconds) rather than
                # using blind exponential backoff.
                retry_after = resp.headers.get("Retry-After") or resp.headers.get("X-RateLimit-Reset-After")
                try:
                    wait = max(1.0, min(float(retry_after), 120.0))
                except (TypeError, ValueError):
                    wait = 60.0
                logger.warning("AniList rate limited; retrying in %.1fs (variables=%s)", wait, variables)
                time.sleep(wait)
                return self._query(query, variables, _retries=_retries - 1)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.warning("AniList request failed for variables=%s: %s", variables, exc)
            return None
        if "errors" in data:
            logger.error("AniList GraphQL errors: %s", data["errors"])
            return None
        return data.get("data")

    def get_anilist_id_by_mal(self, mal_id: int) -> int | None:
        """Resolve a MAL ID to its AniList ID, or None if not found."""
        data = self._query(_MAL_TO_ANILIST_QUERY, {"idMal": mal_id})
        media = (data or {}).get("Media")
        if isinstance(media, dict):
            return media.get("id")
        return None

    def get_watching(self) -> list[dict]:
        """Fetch anime with status CURRENT (watching)."""
        return self.get_status(ANILIST_STATUS_WATCHING)

    def get_plan_to_watch(self) -> list[dict]:
        """Fetch anime with status PLANNING (plan to watch)."""
        return self.get_status(ANILIST_STATUS_PLAN_TO_WATCH)

    # Maps synthetic status keys (used in config/UI) to (base_status, format_filter).
    _FORMAT_FILTER_MAP: dict[str, tuple[str, str]] = {
        "COMPLETED_ONA": ("COMPLETED", "ONA"),
        "COMPLETED_OVA": ("COMPLETED", "OVA"),
        "COMPLETED_MOVIE": ("COMPLETED", "MOVIE"),
    }

    def get_status(self, status: str) -> list[dict]:
        """Fetch anime for any supported AniList status.

        Synthetic statuses like COMPLETED_ONA / COMPLETED_MOVIE fetch the
        underlying base status and post-filter by AniList media format.
        """
        if status in self._FORMAT_FILTER_MAP:
            base_status, fmt = self._FORMAT_FILTER_MAP[status]
            return self._fetch_list(base_status, format_filter=fmt)
        return self._fetch_list(status)

    def _fetch_list(self, status: str, format_filter: str | None = None) -> list[dict]:
        data = self._query(_LIST_QUERY, {"userName": self._config.username, "status": status})
        if not data:
            return []

        collection = data.get("MediaListCollection")
        if not collection:
            return []

        items = []
        for lst in collection.get("lists", []):
            for entry in lst.get("entries", []):
                media = entry.get("media", {})
                if format_filter and media.get("format") != format_filter:
                    continue
                normalized = self._normalize(media)
                if normalized:
                    items.append(normalized)

        label = f"{status}:{format_filter}" if format_filter else status
        logger.info("AniList: fetched %d anime for status '%s'", len(items), label)
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

        # Root IDs are resolved lazily by the matcher only when direct lookup
        # fails, avoiding an AniList API call for every item up front.
        root_anilist_id = None
        root_mal_id = None
        root_title = None

        fmt = str(media.get("format") or "").strip().upper()
        try:
            episodes = int(media.get("episodes") or 0)
        except (TypeError, ValueError):
            episodes = 0

        # AniList ONA/OVA/SPECIAL entries are mixed: some are episodic series,
        # others are effectively standalone films that PMDB indexes as movies.
        # Treat single-episode entries as movies so PMDB community mappings can
        # hit the correct target for cases like Star Fox Zero.
        if fmt == "MOVIE":
            media_type = "movie"
        elif fmt in {"ONA", "OVA", "SPECIAL"} and episodes == 1:
            media_type = "movie"
        else:
            media_type = "tv"

        return {
            "title": title,
            "year": media.get("seasonYear"),
            "media_type": media_type,
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
        # Fast path: check shared cache without acquiring the lock.
        if media_id in self._root_context_cache:
            return self._root_context_cache[media_id]

        # Acquire the shared lock before any cache write so concurrent syncs
        # for different users don't race on the same chain-walk.
        with _SHARED_CACHE_LOCK:
            # Re-check after acquiring the lock — another thread may have
            # already resolved this chain while we were waiting.
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
