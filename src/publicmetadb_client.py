"""PublicMetaDB API client for managing lists and items."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import PublicMetaDBConfig

logger = logging.getLogger(__name__)

# Rate limit: 300 requests per 10 seconds
RATE_LIMIT_MAX = 280  # Leave some headroom
RATE_LIMIT_WINDOW = 10.0


class RateLimiter:
    """Sliding-window rate limiter."""

    def __init__(self, max_requests: int = RATE_LIMIT_MAX, window_seconds: float = RATE_LIMIT_WINDOW):
        self._max = max_requests
        self._window = window_seconds
        self._timestamps: list[float] = []

    def wait(self) -> None:
        now = time.time()
        # Purge timestamps outside the window
        self._timestamps = [t for t in self._timestamps if now - t < self._window]
        if len(self._timestamps) >= self._max:
            sleep_for = self._timestamps[0] + self._window - now + 0.1
            if sleep_for > 0:
                logger.debug("Rate limit: sleeping %.2fs", sleep_for)
                time.sleep(sleep_for)
        self._timestamps.append(time.time())


class PublicMetaDBClient:
    """Client for the PublicMetaDB external API."""

    def __init__(self, config: PublicMetaDBConfig):
        self._config = config
        self._session = self._build_session()
        self._limiter = RateLimiter()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._config.api_key}",
        })

        retry = Retry(
            total=3,
            backoff_factor=2.0,
            status_forcelist=[429, 500, 502, 503],
            allowed_methods=["GET", "POST", "DELETE"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _request(self, method: str, path: str, **kwargs) -> dict | list | None:
        self._limiter.wait()
        url = f"{self._config.base_url}{path}"
        logger.debug("%s %s", method, url)
        resp = self._session.request(method, url, timeout=30, **kwargs)
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.text:
            return None
        return resp.json()

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        return self._request("GET", path, params=params)

    def _post(self, path: str, data: dict) -> dict | None:
        return self._request("POST", path, json=data)

    def _delete(self, path: str) -> dict | None:
        return self._request("DELETE", path)

    # Watch history

    def get_watched_history(self) -> list[dict]:
        return self._get_paginated_items("/api/external/watched")

    def mark_watched(
        self,
        tmdb_id: int,
        media_type: str,
        season: int | None = None,
        episode: int | None = None,
        watched_at: str | None = None,
        dedupe: bool = False,
    ) -> dict | None:
        payload = {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
        }
        if season is not None:
            payload["season"] = season
        if episode is not None:
            payload["episode"] = episode
        if watched_at is not None:
            payload["watched_at"] = watched_at
        path = "/api/external/watched?dedupe=true" if dedupe else "/api/external/watched"
        return self._post(path, payload)

    def delete_watched_entry(self, watched_id: str) -> bool:
        try:
            self._delete(f"/api/external/watched/{watched_id}")
            logger.info("Deleted watched entry %s", watched_id)
            return True
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                logger.info("Watched entry %s was already gone", watched_id)
                return False
            raise

    def bulk_delete_watched(
        self,
        tmdb_id: int,
        media_type: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> bool:
        """Delete all play entries for a title (or specific season/episode) in one call.

        Uses DELETE /api/external/watched?tmdb_id=X&media_type=Y which wipes every
        play record for that title at once — far fewer API calls than deleting by ID.
        """
        params: dict = {"tmdb_id": tmdb_id, "media_type": media_type}
        if season is not None:
            params["season"] = season
        if episode is not None:
            params["episode"] = episode
        try:
            self._request("DELETE", "/api/external/watched", params=params)
            logger.info("Bulk-deleted watched history for tmdb_id=%s (%s)", tmdb_id, media_type)
            return True
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return False
            raise

    def clear_watched_history(self) -> int:
        """Delete all watched history using bulk deletes grouped by title.

        Strategy:
          1. Fetch the full history (paginated) to collect unique (tmdb_id, media_type) pairs.
          2. Issue one bulk DELETE per unique title — this wipes every play record for
             that title in a single API call instead of one call per episode entry.
        This reduces API calls from O(total plays) to O(unique titles).
        """
        all_items = self.get_watched_history()
        if not all_items:
            return 0

        # Collect unique (tmdb_id, media_type) pairs preserving insertion order.
        seen: set[tuple] = set()
        unique_titles: list[tuple[int, str]] = []
        for item in all_items:
            tmdb_id = item.get("tmdb_id")
            media_type = item.get("media_type")
            if not tmdb_id or not media_type:
                continue
            key = (int(tmdb_id), str(media_type))
            if key not in seen:
                seen.add(key)
                unique_titles.append(key)

        if not unique_titles:
            return 0

        # Count total play entries we're about to wipe (for the return value).
        entry_count = len(all_items)

        # Bulk-delete concurrently — each title is fully independent.
        deleted_titles = 0
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {
                pool.submit(self.bulk_delete_watched, tmdb_id, media_type): (tmdb_id, media_type)
                for tmdb_id, media_type in unique_titles
            }
            for future in as_completed(futures):
                tmdb_id, media_type = futures[future]
                try:
                    if future.result():
                        deleted_titles += 1
                except Exception as exc:
                    logger.warning(
                        "Error bulk-deleting watched history for tmdb_id=%s (%s): %s",
                        tmdb_id, media_type, exc,
                    )

        logger.info(
            "Cleared watched history: %d unique titles bulk-deleted (%d total play entries)",
            deleted_titles, entry_count,
        )
        return entry_count

    # Resume / continue watching

    def get_resume_points(self) -> list[dict]:
        return self._get_paginated_items("/api/external/resume")

    def save_resume_point(
        self,
        tmdb_id: int,
        media_type: str,
        position_ms: int,
        runtime_ms: int,
        season: int | None = None,
        episode: int | None = None,
    ) -> dict | None:
        payload = {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "position_ms": position_ms,
            "runtime_ms": runtime_ms,
        }
        if season is not None:
            payload["season"] = season
        if episode is not None:
            payload["episode"] = episode
        return self._post("/api/external/resume", payload)

    def save_resume_points_batch(self, items: list[dict]) -> dict | None:
        return self._post("/api/external/resume/batch", {"items": items})

    def delete_resume_point(self, resume_id: str) -> bool:
        try:
            self._delete(f"/api/external/resume/{resume_id}")
            logger.info("Deleted resume point %s", resume_id)
            return True
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                logger.info("Resume point %s was already gone", resume_id)
                return False
            raise

    def _get_paginated_items(self, path: str, per_page: int = 100) -> list[dict]:
        all_items: list[dict] = []
        page = 1
        while True:
            resp = self._get(path, params={"page": page, "perPage": per_page})
            if not resp:
                break
            if isinstance(resp, list):
                all_items.extend(resp)
                break
            items = list(resp.get("items", []))
            all_items.extend(items)
            total_pages = int(resp.get("totalPages", 1) or 1)
            if page >= total_pages or not items:
                break
            page += 1
        return all_items

    # ── Mapping lookups ────────────────────────────────────────────

    def lookup_by_external_id(self, id_type: str, id_value: str, media_type: str) -> int | None:
        """Resolve an external ID (imdb, mal, anidb, tvdb, etc.) to a TMDB ID."""
        try:
            resp = self._get("/api/external/mappings/lookup", params={
                "id_type": id_type,
                "id_value": id_value,
                "media_type": media_type,
            })
            if resp and resp.get("results"):
                return resp["results"][0].get("tmdb_id")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise
        return None

    def create_id_mapping(
        self,
        tmdb_id: int,
        media_type: str,
        id_type: str,
        id_value: str,
    ) -> bool:
        """Submit a new external ID → TMDB mapping to PMDB for community benefit.

        id_type must be one of: imdb, tvdb, mal, anilist, anidb, trakt.
        Returns True if the mapping was accepted.
        """
        try:
            resp = self._post("/api/external/mappings", {
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "id_type": id_type,
                "id_value": str(id_value),
            })
            return bool(resp and resp.get("success"))
        except requests.HTTPError:
            return False

    # ── Anime seasons ──────────────────────────────────────────────

    def get_anime_seasons(self, tmdb_id: int) -> list[dict]:
        """Fetch PMDB anime season mappings for a TMDB show.

        Each entry maps a logical anime season (season_number) to a TMDB
        season/episode range via tmdb_season, tmdb_episode_start, tmdb_episode_end.
        Results are cached per tmdb_id for the lifetime of this client instance.
        """
        cache = getattr(self, "_anime_seasons_cache", None)
        if cache is None:
            cache = {}
            object.__setattr__(self, "_anime_seasons_cache", cache)
        if tmdb_id in cache:
            return cache[tmdb_id]
        try:
            resp = self._get("/api/external/anime-seasons", params={"tmdb_id": tmdb_id})
            if isinstance(resp, list):
                seasons = resp
            elif isinstance(resp, dict):
                seasons = resp.get("items") or resp.get("seasons") or []
            else:
                seasons = []
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                seasons = []
            else:
                raise
        cache[tmdb_id] = seasons
        return seasons

    # ── List management ────────────────────────────────────────────

    def get_lists(self) -> list[dict]:
        """Fetch all user lists, paginated."""
        all_lists = []
        page = 1
        while True:
            resp = self._get("/api/external/lists", params={"page": page, "perPage": 50})
            if not resp:
                break
            items = resp.get("items", [])
            all_lists.extend(items)
            if page >= resp.get("totalPages", 1):
                break
            page += 1
        return all_lists

    def find_list_by_name(self, name: str) -> dict | None:
        """Find a list by exact name match."""
        for lst in self.get_lists():
            if lst.get("name") == name:
                return lst
        return None

    def create_list(self, name: str, description: str = "", is_public: bool = False) -> dict:
        """Create a new list and return its metadata."""
        resp = self._post("/api/external/lists", data={
            "name": name,
            "description": description,
            "is_public": is_public,
            "type": "custom",
        })
        if resp and resp.get("success"):
            logger.info("Created list '%s' (id=%s)", name, resp["item"]["id"])
            return resp["item"]
        raise RuntimeError(f"Failed to create list '{name}': {resp}")

    def get_or_create_list(self, name: str, description: str = "", is_public: bool = False) -> dict:
        """Find a list by name, or create it if missing."""
        existing = self.find_list_by_name(name)
        if existing:
            logger.debug("Found existing list '%s' (id=%s)", name, existing["id"])
            return existing
        return self.create_list(name, description, is_public=is_public)

    def delete_list(self, list_id: str) -> bool:
        """Delete a list by ID. Returns False if it is already gone."""
        try:
            self._delete(f"/api/external/lists/{list_id}")
            logger.info("Deleted list %s", list_id)
            return True
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                logger.info("List %s was already gone", list_id)
                return False
            raise

    # ── List items ─────────────────────────────────────────────────

    def get_list_items(self, list_id: str) -> list[dict]:
        """Fetch all items in a list, paginated."""
        all_items = []
        page = 1
        while True:
            resp = self._get(f"/api/external/lists/{list_id}/items", params={
                "page": page, "perPage": 100,
            })
            if not resp:
                break
            items = resp.get("items", [])
            all_items.extend(items)
            if page >= resp.get("totalPages", 1):
                break
            page += 1
        return all_items

    def add_item_to_list(self, list_id: str, tmdb_id: int, media_type: str) -> dict | None:
        """Add a single item to a list."""
        resp = self._post(f"/api/external/lists/{list_id}/items", data={
            "tmdb_id": tmdb_id,
            "media_type": media_type,
        })
        if resp and resp.get("success"):
            logger.debug("Added tmdb_id=%s (%s) to list %s", tmdb_id, media_type, list_id)
            return resp.get("item")
        logger.warning("Failed to add tmdb_id=%s to list %s: %s", tmdb_id, list_id, resp)
        return None

    def remove_item_from_list(self, list_id: str, item_id: str) -> None:
        """Remove an item from a list by its list-item ID."""
        self._delete(f"/api/external/lists/{list_id}/items/{item_id}")
        logger.debug("Removed item %s from list %s", item_id, list_id)
