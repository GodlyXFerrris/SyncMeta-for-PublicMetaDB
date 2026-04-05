"""SIMKL API client for fetching user watchlists."""

import logging
import time
import webbrowser
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import SimklConfig

logger = logging.getLogger(__name__)

# Status codes from SIMKL that map to our list names
SIMKL_STATUS_WATCHING = "watching"
SIMKL_STATUS_PLAN_TO_WATCH = "plantowatch"
SIMKL_STATUS_COMPLETED = "completed"
SIMKL_STATUS_ON_HOLD = "hold"
SIMKL_STATUS_DROPPED = "dropped"

SIMKL_API_STATUS_MAP = {
    SIMKL_STATUS_WATCHING: "watching",
    SIMKL_STATUS_PLAN_TO_WATCH: "plan to watch",
    SIMKL_STATUS_COMPLETED: "completed",
    SIMKL_STATUS_ON_HOLD: "on hold",
    SIMKL_STATUS_DROPPED: "dropped",
}

SIMKL_API_TYPE_MAP = {
    "shows": "tv",
    "movies": "movie",
    "anime": "anime",
}


class SimklClient:
    """Client for the SIMKL API v2."""

    def __init__(self, config: SimklConfig):
        self._config = config
        self._session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "Content-Type": "application/json",
            "simkl-api-key": self._config.client_id,
        })
        if self._config.access_token:
            session.headers["Authorization"] = f"Bearer {self._config.access_token}"

        retry = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _get(self, path: str, params: dict | None = None) -> dict | list | None:
        url = f"{self._config.base_url}{path}"
        logger.debug("GET %s params=%s", url, params)
        resp = self._session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.text:
            return None
        return resp.json()

    # ── Authentication (PIN flow) ──────────────────────────────────

    def request_pin(self) -> dict:
        """Request a SIMKL PIN/device-code payload."""
        data = self._get(f"/oauth/pin?client_id={self._config.client_id}")
        if not data or data.get("result") != "OK":
            raise RuntimeError(f"Failed to request PIN: {data}")
        return data

    def check_pin(self, user_code: str) -> dict | None:
        """Check whether a SIMKL PIN has been approved yet."""
        return self._get(f"/oauth/pin/{user_code}?client_id={self._config.client_id}")

    def authenticate_pin(self) -> str:
        """Run the SIMKL PIN authentication flow. Returns an access token."""
        data = self.request_pin()

        user_code = data["user_code"]
        verification_url = data["verification_url"]
        interval = data.get("interval", 5)
        expires_in = data.get("expires_in", 900)

        print(f"\n  1. Go to: {verification_url}")
        print(f"  2. Enter code: {user_code}\n")

        try:
            webbrowser.open(f"{verification_url}{user_code}")
        except Exception:
            pass  # Non-critical if browser doesn't open

        deadline = time.time() + expires_in
        while time.time() < deadline:
            time.sleep(interval)
            check = self.check_pin(user_code)
            if check and check.get("result") == "OK":
                token = check["access_token"]
                logger.info("Authentication successful")
                return token
            msg = check.get("message", "") if check else ""
            if msg == "Slow down":
                interval += 1
            logger.debug("Waiting for user authorization... (%s)", msg)

        raise TimeoutError("PIN authentication timed out")

    # ── Watchlist fetching ─────────────────────────────────────────

    def get_watching(self, media_types: list[str] | None = None) -> dict[str, list[dict]]:
        """Fetch items with status 'watching', grouped by media type."""
        return self.get_status(SIMKL_STATUS_WATCHING, media_types)

    def get_plan_to_watch(self, media_types: list[str] | None = None) -> dict[str, list[dict]]:
        """Fetch items with status 'plantowatch', grouped by media type."""
        return self.get_status(SIMKL_STATUS_PLAN_TO_WATCH, media_types)

    def get_status(self, status: str, media_types: list[str] | None = None) -> dict[str, list[dict]]:
        """Fetch items for any SIMKL watchlist status."""
        return self._fetch_list(status, media_types)

    def _fetch_list(self, status: str, media_types: list[str] | None = None) -> dict[str, list[dict]]:
        """Fetch and normalize items for a given status, grouped by SIMKL media type."""
        grouped: dict[str, list[dict]] = {}
        types_to_process = media_types or ["shows", "movies", "anime"]
        api_status = self._api_status(status)

        for media_type in types_to_process:
            api_type = self._api_type(media_type)
            raw = self._get(f"/sync/all-items/{api_type}/{quote(api_status, safe='')}")
            if not raw:
                logger.info("No items found for status '%s' and type '%s'", status, media_type)
                continue
            raw_items = raw.get(media_type, [])
            items = []
            for entry in raw_items:
                normalized = self._normalize_item(entry, media_type)
                if normalized:
                    items.append(normalized)
            if items:
                grouped[media_type] = items

        total = sum(len(v) for v in grouped.values())
        logger.info("Fetched %d items for status '%s' across %s", total, status, list(grouped.keys()))
        return grouped

    @staticmethod
    def _api_status(status: str) -> str:
        return SIMKL_API_STATUS_MAP.get(status, status.replace("_", " "))

    @staticmethod
    def _api_type(media_type: str) -> str:
        if media_type not in SIMKL_API_TYPE_MAP:
            raise ValueError(f"Unsupported SIMKL media type: {media_type}")
        return SIMKL_API_TYPE_MAP[media_type]

    def _normalize_item(self, entry: dict, media_type: str) -> dict | None:
        """Normalize a SIMKL list entry to a common format."""
        # Shows and anime use "show" key, movies use "movie" key
        if media_type == "movies":
            media = entry.get("movie")
        else:
            media = entry.get("show")

        if not media:
            return None

        ids = media.get("ids", {})

        # Determine the PublicMetaDB-compatible media type
        if media_type == "movies":
            pmdb_type = "movie"
        else:
            pmdb_type = "tv"  # Both shows and anime map to "tv"

        return {
            "title": media.get("title", "Unknown"),
            "year": media.get("year"),
            "media_type": pmdb_type,
            "simkl_type": media_type,
            "imdb_id": ids.get("imdb"),
            "tmdb_id": str(ids["tmdb"]) if ids.get("tmdb") else None,
            "mal_id": str(ids["mal"]) if ids.get("mal") else None,
            "anilist_id": str(ids["anilist"]) if ids.get("anilist") else None,
            "anidb_id": str(ids["anidb"]) if ids.get("anidb") else None,
            "tvdb_id": str(ids["tvdb"]) if ids.get("tvdb") else None,
            "ids": ids,
            "status": entry.get("status"),
            "added_at": entry.get("added_to_watchlist_at"),
        }

    # ── Activities (for delta sync) ───────────────────────────────

    def get_activities(self) -> dict | None:
        """Fetch last activity timestamps (used for incremental sync)."""
        return self._get("/sync/activities")

    def get_watched_history(self) -> list[dict]:
        """Fetch SIMKL completed history as watched-once records."""
        history: list[dict] = []
        history.extend(self._get_completed_movie_history())
        history.extend(self._get_completed_show_history("shows"))
        history.extend(self._get_completed_show_history("anime"))
        return history

    def get_playback_progress(self) -> list[dict]:
        """Fetch SIMKL playback progress records."""
        raw = self._get("/sync/playback")
        if not raw:
            return []

        if isinstance(raw, list):
            raw_items = raw
        elif isinstance(raw, dict):
            raw_items = list(raw.get("movies", [])) + list(raw.get("shows", [])) + list(raw.get("anime", [])) + list(raw.get("items", []))
        else:
            raw_items = []

        entries: list[dict] = []
        for entry in raw_items:
            normalized = self._normalize_playback_entry(entry)
            if normalized:
                entries.append(normalized)
        return entries

    def _get_completed_movie_history(self) -> list[dict]:
        raw = self._get("/sync/all-items/movie/completed", params={"extended": "full"})
        items = raw.get("movies", []) if isinstance(raw, dict) else []
        history: list[dict] = []
        for entry in items:
            movie = entry.get("movie") if isinstance(entry, dict) else None
            if not isinstance(movie, dict):
                continue
            ids = movie.get("ids", {}) or {}
            tmdb_id = ids.get("tmdb")
            if not tmdb_id:
                continue
            watched_at = (
                entry.get("watched_at")
                or entry.get("last_watched_at")
                or entry.get("last_watched")
                or movie.get("watched_at")
            )
            history.append({
                "tmdb_id": int(tmdb_id),
                "media_type": "movie",
                "watched_at": watched_at,
                "title": movie.get("title", "Unknown"),
            })
        return history

    def _get_completed_show_history(self, media_key: str) -> list[dict]:
        api_type = self._api_type(media_key)
        raw = self._get(
            f"/sync/all-items/{api_type}/completed",
            params={"extended": "full", "episode_watched_at": "yes"},
        )
        items = raw.get(media_key, []) if isinstance(raw, dict) else []
        history: list[dict] = []
        for entry in items:
            show = entry.get("show") if isinstance(entry, dict) else None
            if not isinstance(show, dict):
                continue
            ids = show.get("ids", {}) or {}
            tmdb_id = ids.get("tmdb")
            if not tmdb_id:
                continue
            history.extend(self._extract_episode_history(entry, int(tmdb_id), show.get("title", "Unknown")))
        return history

    def _extract_episode_history(self, entry: dict, tmdb_id: int, title: str) -> list[dict]:
        history: list[dict] = []
        seen: set[tuple[int, int]] = set()

        def add_episode(season: int | None, episode: int | None, watched_at: str | None) -> None:
            if season is None or episode is None:
                return
            key = (int(season), int(episode))
            if key in seen:
                return
            seen.add(key)
            history.append({
                "tmdb_id": tmdb_id,
                "media_type": "tv",
                "season": int(season),
                "episode": int(episode),
                "watched_at": watched_at,
                "title": title,
            })

        for season_entry in entry.get("seasons", []) or []:
            season_number = season_entry.get("number") or season_entry.get("season")
            for episode_entry in season_entry.get("episodes", []) or []:
                add_episode(
                    season_number,
                    episode_entry.get("number") or episode_entry.get("episode"),
                    episode_entry.get("watched_at") or episode_entry.get("last_watched_at"),
                )

        for episode_entry in entry.get("episodes", []) or []:
            add_episode(
                episode_entry.get("season"),
                episode_entry.get("number") or episode_entry.get("episode"),
                episode_entry.get("watched_at") or episode_entry.get("last_watched_at"),
            )

        return history

    def _normalize_playback_entry(self, entry: dict) -> dict | None:
        if not isinstance(entry, dict):
            return None

        movie = entry.get("movie")
        show = entry.get("show")
        episode = entry.get("episode")

        if isinstance(movie, dict):
            ids = movie.get("ids", {}) or {}
            tmdb_id = ids.get("tmdb")
            runtime_minutes = entry.get("runtime") or movie.get("runtime")
            progress = self._playback_progress_percent(entry)
            if not tmdb_id or runtime_minutes in (None, 0) or progress is None:
                return None
            runtime_ms = int(float(runtime_minutes) * 60_000)
            position_ms = int(round(runtime_ms * (progress / 100.0)))
            return {
                "tmdb_id": int(tmdb_id),
                "media_type": "movie",
                "position_ms": position_ms,
                "runtime_ms": runtime_ms,
                "progress": progress,
                "paused_at": entry.get("updated_at") or entry.get("paused_at"),
                "title": movie.get("title", "Unknown"),
            }

        if isinstance(show, dict) and isinstance(episode, dict):
            show_ids = show.get("ids", {}) or {}
            tmdb_id = show_ids.get("tmdb")
            runtime_minutes = entry.get("runtime") or episode.get("runtime")
            progress = self._playback_progress_percent(entry)
            season = episode.get("season")
            number = episode.get("number") or episode.get("episode")
            if not tmdb_id or runtime_minutes in (None, 0) or progress is None or season is None or number is None:
                return None
            runtime_ms = int(float(runtime_minutes) * 60_000)
            position_ms = int(round(runtime_ms * (progress / 100.0)))
            return {
                "tmdb_id": int(tmdb_id),
                "media_type": "tv",
                "season": int(season),
                "episode": int(number),
                "position_ms": position_ms,
                "runtime_ms": runtime_ms,
                "progress": progress,
                "paused_at": entry.get("updated_at") or entry.get("paused_at"),
                "title": show.get("title", "Unknown"),
            }

        return None

    @staticmethod
    def _playback_progress_percent(entry: dict) -> float | None:
        for key in ("progress", "percent"):
            value = entry.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        position = entry.get("position")
        duration = entry.get("duration") or entry.get("runtime")
        try:
            if position is not None and duration not in (None, 0):
                return (float(position) / float(duration)) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        return None
