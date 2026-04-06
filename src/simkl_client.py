"""SIMKL API client for fetching user watchlists."""

import logging
import re
import time
import webbrowser
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import AniListConfig, SimklConfig

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

SIMKL_NORMALIZED_STATUS_MAP = {
    "watching": SIMKL_STATUS_WATCHING,
    "plantowatch": SIMKL_STATUS_PLAN_TO_WATCH,
    "plan to watch": SIMKL_STATUS_PLAN_TO_WATCH,
    "planning": SIMKL_STATUS_PLAN_TO_WATCH,
    "completed": SIMKL_STATUS_COMPLETED,
    "hold": SIMKL_STATUS_ON_HOLD,
    "on hold": SIMKL_STATUS_ON_HOLD,
    "dropped": SIMKL_STATUS_DROPPED,
}

SIMKL_HISTORY_STATUS_SCAN_ORDER = [
    SIMKL_STATUS_WATCHING,
    SIMKL_STATUS_COMPLETED,
    SIMKL_STATUS_ON_HOLD,
    SIMKL_STATUS_DROPPED,
    SIMKL_STATUS_PLAN_TO_WATCH,
]


class SimklClient:
    """Client for the SIMKL API v2."""

    def __init__(self, config: SimklConfig, cancel_requested_callback=None):
        self._config = config
        self._session = self._build_session()
        self._tmdb_season_plan_cache: dict[int, list[tuple[int, int]]] = {}
        self._anime_root_cache: dict[int, dict] = {}
        self._anime_root_client = None
        self._cancel_requested_callback = cancel_requested_callback

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
        requested_status = self._normalize_status(status)

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
                    item_status = self._normalize_status(normalized.get("status"))
                    if item_status and item_status != requested_status:
                        logger.info(
                            "Skipping SIMKL %s entry '%s' because it reported status '%s' during '%s' sync",
                            media_type,
                            normalized.get("title", "Unknown"),
                            normalized.get("status"),
                            status,
                        )
                        continue
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
    def _normalize_status(status: object) -> str:
        return SIMKL_NORMALIZED_STATUS_MAP.get(str(status or "").strip().lower(), "")

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
        root_ids: dict[str, str] = {}
        root_title = None

        # Determine the PublicMetaDB-compatible media type
        if media_type == "movies":
            pmdb_type = "movie"
        elif media_type == "anime":
            anime_type = media.get("anime_type", "")
            if not anime_type:
                # anime_type is only present when SIMKL returns extended data.
                # Fall back to checking IDs: real anime entries have a MAL or AniList ID;
                # non-anime items accidentally added to the anime list typically don't.
                ids_preview = media.get("ids", {})
                if not ids_preview.get("mal") and not ids_preview.get("anilist"):
                    logger.warning(
                        "Skipping SIMKL anime entry '%s' (%s) — no anime_type, no MAL/AniList ID",
                        media.get("title", "Unknown"),
                        media.get("year", ""),
                    )
                    return None
            # Anime movies must be looked up as "movie" in PMDB; everything else is "tv"
            pmdb_type = "movie" if anime_type == "movie" else "tv"
        else:
            pmdb_type = "tv"  # Shows map to "tv"

        # Root IDs are resolved lazily by the matcher only when direct lookup
        # fails, avoiding AniList API calls for every item up front.
        root_ids: dict[str, str] = {}

        return {
            "title": media.get("title", "Unknown"),
            "year": media.get("year"),
            "media_type": pmdb_type,
            "simkl_type": media_type,
            "imdb_id": ids.get("imdb"),
            "tmdb_id": str(ids["tmdb"]) if ids.get("tmdb") else None,
            "mal_id": str(ids["mal"]) if ids.get("mal") else None,
            "anilist_id": str(ids["anilist"]) if ids.get("anilist") else None,
            "root_mal_id": str(root_ids["root_mal"]) if root_ids.get("root_mal") else None,
            "root_anilist_id": str(root_ids["root_anilist"]) if root_ids.get("root_anilist") else None,
            "root_title": root_title,
            "root_episode_offset": int(root_ids["root_episode_offset"]) if root_ids.get("root_episode_offset") else 0,
            "anidb_id": str(ids["anidb"]) if ids.get("anidb") else None,
            "tvdb_id": str(ids["tvdb"]) if ids.get("tvdb") else None,
            "ids": ids,
            "status": entry.get("status"),
            "added_at": entry.get("added_to_watchlist_at"),
        }

    def _resolve_anime_root_ids(self, ids: dict) -> dict[str, str]:
        anilist_id = ids.get("anilist")
        if not anilist_id:
            # Fallback: resolve the AniList ID from the MAL ID so we can still
            # walk the prequel chain for entries where SIMKL omits anilist.
            mal_id = ids.get("mal")
            if mal_id:
                try:
                    anilist_id = self._lookup_anilist_id_by_mal(int(mal_id))
                except (TypeError, ValueError):
                    pass
        if not anilist_id:
            return {}
        try:
            anilist_int = int(anilist_id)
        except (TypeError, ValueError):
            return {}
        if anilist_int in self._anime_root_cache:
            return dict(self._anime_root_cache[anilist_int])
        root_context = self._get_anime_root_context(anilist_int)
        root_media = root_context.get("root") if isinstance(root_context, dict) else None
        if not isinstance(root_media, dict) or root_media.get("id") == anilist_int:
            self._anime_root_cache[anilist_int] = {}
            return {}
        resolved = {
            "root_anilist": str(root_media["id"]) if root_media.get("id") else "",
            "root_mal": str(root_media["idMal"]) if root_media.get("idMal") else "",
            "root_title": self._anime_root_title(root_media),
            "root_episode_offset": str(root_context.get("episode_offset", 0) or 0) if isinstance(root_context, dict) else "0",
        }
        self._anime_root_cache[anilist_int] = resolved
        return dict(resolved)

    def _get_anime_root_context(self, anilist_id: int) -> dict | None:
        if self._anime_root_client is None:
            from .anilist_client import AniListClient

            self._anime_root_client = AniListClient(AniListConfig())
        get_context = getattr(self._anime_root_client, "_get_root_context", None)
        if callable(get_context):
            return get_context(anilist_id)
        root = self._anime_root_client._get_root_media(anilist_id)
        return {"root": root, "episode_offset": 0}

    def _lookup_anilist_id_by_mal(self, mal_id: int) -> int | None:
        """Return the AniList ID for a given MAL ID, or None if not found."""
        if self._anime_root_client is None:
            from .anilist_client import AniListClient

            self._anime_root_client = AniListClient(AniListConfig())
        get_id = getattr(self._anime_root_client, "get_anilist_id_by_mal", None)
        if callable(get_id):
            return get_id(mal_id)
        return None

    def _get_anime_root_media(self, anilist_id: int) -> dict | None:
        context = self._get_anime_root_context(anilist_id)
        return context.get("root") if isinstance(context, dict) else None

    @staticmethod
    def _anime_root_title(media: dict) -> str | None:
        if not isinstance(media, dict):
            return None
        title = media.get("title")
        if isinstance(title, dict):
            return title.get("english") or title.get("romaji")
        return None

    # ── Activities (for delta sync) ───────────────────────────────

    def get_activities(self) -> dict | None:
        """Fetch last activity timestamps (used for incremental sync)."""
        return self._get("/sync/activities")

    def get_watched_history(self, since: str | None = None) -> list[dict]:
        """Fetch SIMKL completed history as watched-once records."""
        history: list[dict] = []
        movie_history = self._get_completed_movie_history(since=since)
        show_history = self._get_show_history("shows", since=since)
        anime_history = self._get_show_history("anime", since=since)
        history.extend(movie_history)
        history.extend(show_history)
        history.extend(anime_history)
        if since:
            history = [item for item in history if self._is_history_after(item, since)]
        deduped = self._dedupe_watched_history(history)
        logger.info(
            "SIMKL watched history summary: movies=%d shows=%d anime=%d total=%d deduped=%d",
            len(movie_history),
            len(show_history),
            len(anime_history),
            len(history),
            len(deduped),
        )
        return deduped

    def get_playback_progress(self, include_next_up_fallback: bool = False) -> list[dict]:
        """Fetch SIMKL playback progress records."""
        raw = self._get("/sync/playback")
        if not raw:
            raw_items = []
        elif isinstance(raw, list):
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
        if include_next_up_fallback:
            entries = self._merge_next_up_resume_fallback(entries)
        return entries

    def _merge_next_up_resume_fallback(self, entries: list[dict]) -> list[dict]:
        merged = list(entries)
        seen = {self._resume_identity_key(item) for item in entries if self._resume_identity_key(item)}
        fallback_items = self._get_next_up_resume_fallback()
        added = 0
        for item in fallback_items:
            key = self._resume_identity_key(item)
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
            added += 1
        logger.info("SIMKL next-up resume fallback added %d entries", added)
        return merged

    def _get_next_up_resume_fallback(self) -> list[dict]:
        entries: list[dict] = []
        for media_key in ("shows", "anime"):
            for status in (SIMKL_STATUS_WATCHING, SIMKL_STATUS_ON_HOLD):
                entries.extend(self._get_next_up_resume_for_status(media_key, status))
        return entries

    def _get_next_up_resume_for_status(self, media_key: str, status: str) -> list[dict]:
        api_type = self._api_type(media_key)
        raw = self._get(
            f"/sync/all-items/{api_type}/{quote(self._api_status(status), safe='')}",
            params={"extended": "full"},
        )
        items = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            if isinstance(raw.get(media_key), list):
                items = raw.get(media_key, [])
            elif media_key == "shows" and isinstance(raw.get("tv"), list):
                items = raw.get("tv", [])
            elif media_key == "shows" and isinstance(raw.get("shows"), list):
                items = raw.get("shows", [])
            elif media_key == "anime" and isinstance(raw.get("anime"), list):
                items = raw.get("anime", [])
            elif media_key == "anime" and isinstance(raw.get("shows"), list):
                items = raw.get("shows", [])

        progress_entries: list[dict] = []
        for entry in items:
            normalized = self._normalize_next_up_resume_entry(entry, media_key)
            if normalized:
                progress_entries.append(normalized)
        logger.info(
            "SIMKL %s next-up fallback status '%s' yielded %d resume entries",
            media_key,
            status,
            len(progress_entries),
        )
        return progress_entries

    def _normalize_next_up_resume_entry(self, entry: dict, media_key: str) -> dict | None:
        show = self._show_payload(entry)
        if not isinstance(show, dict):
            return None

        next_candidate = entry.get("next_to_watch") or show.get("next_to_watch")
        if isinstance(next_candidate, list):
            next_candidate = next((item for item in next_candidate if isinstance(item, dict)), None)
        if not isinstance(next_candidate, dict):
            return None

        season = next_candidate.get("season")
        episode = next_candidate.get("number") or next_candidate.get("episode")
        runtime_minutes = next_candidate.get("runtime") or show.get("runtime") or entry.get("runtime")
        try:
            season = int(season)
            episode = int(episode)
            runtime_minutes = float(runtime_minutes)
        except (TypeError, ValueError):
            return None
        if season <= 0 or episode <= 0 or runtime_minutes <= 0:
            return None

        ids = show.get("ids", {}) or {}
        root_ids: dict[str, str] = {}
        root_title = None
        if media_key == "anime":
            root_ids = self._resolve_anime_root_ids(ids)
            root_title = root_ids.get("root_title")
            if root_ids.get("root_anilist"):
                ids["root_anilist"] = root_ids["root_anilist"]
            if root_ids.get("root_mal"):
                ids["root_mal"] = root_ids["root_mal"]

        runtime_ms = int(runtime_minutes * 60_000)
        # PublicMetaDB ignores very tiny progress values, so keep this as a small
        # "start of next episode" bookmark rather than pretending it's exact playback.
        position_ms = max(1, int(round(runtime_ms * 0.05)))
        return {
            "tmdb_id": int(ids["tmdb"]) if ids.get("tmdb") else None,
            "media_type": "tv",
            "season": season,
            "episode": episode,
            "position_ms": position_ms,
            "runtime_ms": runtime_ms,
            "progress": 5.0,
            "paused_at": (
                entry.get("last_watched_at")
                or entry.get("last_watched")
                or show.get("last_watched_at")
                or show.get("last_watched")
            ),
            "title": show.get("title", "Unknown"),
            "year": show.get("year"),
            "simkl_type": media_key,
            "imdb_id": ids.get("imdb"),
            "mal_id": str(ids["mal"]) if ids.get("mal") else None,
            "anilist_id": str(ids["anilist"]) if ids.get("anilist") else None,
            "root_mal_id": str(root_ids["root_mal"]) if root_ids.get("root_mal") else None,
            "root_anilist_id": str(root_ids["root_anilist"]) if root_ids.get("root_anilist") else None,
            "root_title": root_title,
            "root_episode_offset": int(root_ids["root_episode_offset"]) if root_ids.get("root_episode_offset") else 0,
            "anidb_id": str(ids["anidb"]) if ids.get("anidb") else None,
            "tvdb_id": str(ids["tvdb"]) if ids.get("tvdb") else None,
            "ids": ids,
            "resume_fallback": "next_up",
        }

    @staticmethod
    def _resume_identity_key(item: dict) -> str:
        tmdb_id = item.get("tmdb_id")
        media_type = item.get("media_type")
        if not tmdb_id or not media_type:
            return ""
        season = item.get("season")
        episode = item.get("episode")
        return f"{tmdb_id}:{media_type}:{season if season is not None else ''}:{episode if episode is not None else ''}"

    def _get_completed_movie_history(self, since: str | None = None) -> list[dict]:
        params = {"extended": "full"}
        if since:
            params["date_from"] = since
        raw = self._get("/sync/all-items/movie/completed", params=params)
        items = raw.get("movies", []) if isinstance(raw, dict) else []
        history: list[dict] = []
        for entry in items:
            movie = entry.get("movie") if isinstance(entry, dict) else None
            if not isinstance(movie, dict):
                continue
            ids = movie.get("ids", {}) or {}
            watched_at = (
                entry.get("watched_at")
                or entry.get("last_watched_at")
                or entry.get("last_watched")
                or movie.get("watched_at")
            )
            history.append({
                "tmdb_id": int(ids["tmdb"]) if ids.get("tmdb") else None,
                "media_type": "movie",
                "simkl_type": "movies",
                "watched_at": watched_at,
                "title": movie.get("title", "Unknown"),
                "year": movie.get("year"),
                "imdb_id": ids.get("imdb"),
                "ids": ids,
            })
        return history

    def _get_show_history(self, media_key: str, since: str | None = None) -> list[dict]:
        history: list[dict] = []
        for status in SIMKL_HISTORY_STATUS_SCAN_ORDER:
            history.extend(self._get_show_history_for_status(media_key, status, since=since))
        return history

    def _get_show_history_for_status(self, media_key: str, status: str, since: str | None = None) -> list[dict]:
        api_type = self._api_type(media_key)
        params = {"extended": "full", "episode_watched_at": "yes"}
        if since:
            params["date_from"] = since
        raw = self._get(
            f"/sync/all-items/{api_type}/{quote(self._api_status(status), safe='')}",
            params=params,
        )
        items = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            if isinstance(raw.get(media_key), list):
                items = raw.get(media_key, [])
            elif media_key == "shows" and isinstance(raw.get("tv"), list):
                items = raw.get("tv", [])
            elif media_key == "shows" and isinstance(raw.get("shows"), list):
                items = raw.get("shows", [])
            elif media_key == "anime" and isinstance(raw.get("anime"), list):
                items = raw.get("anime", [])
            elif media_key == "anime" and isinstance(raw.get("shows"), list):
                items = raw.get("shows", [])
        logger.info(
            "SIMKL %s history status '%s': raw_items=%d",
            media_key,
            status,
            len(items),
        )
        if items:
            logger.info(
                "SIMKL %s history sample for '%s': %s",
                media_key,
                status,
                self._describe_history_entry(items[0]),
            )
        history: list[dict] = []
        for entry in items:
            show = self._show_payload(entry)
            if not isinstance(show, dict):
                continue
            history.extend(self._extract_episode_history(entry, show, media_key))
        logger.info(
            "SIMKL %s history status '%s' yielded %d normalized episode entries",
            media_key,
            status,
            len(history),
        )
        return history

    def _extract_episode_history(self, entry: dict, show: dict, media_key: str) -> list[dict]:
        history: list[dict] = []
        seen: set[tuple[int, int]] = set()
        ids = show.get("ids", {}) or {}
        title = show.get("title", "Unknown")
        tmdb_id = int(ids["tmdb"]) if ids.get("tmdb") else None
        root_ids = self._resolve_anime_root_ids(ids) if media_key == "anime" else {}
        if root_ids.get("root_anilist"):
            ids["root_anilist"] = root_ids["root_anilist"]
        if root_ids.get("root_mal"):
            ids["root_mal"] = root_ids["root_mal"]
        fallback_watched_at = (
            entry.get("last_watched_at")
            or entry.get("last_watched")
            or show.get("last_watched_at")
            or show.get("last_watched")
        )

        if media_key == "anime" and self._is_movie_like_anime_history(entry, show):
            return [{
                "tmdb_id": tmdb_id,
                "media_type": "movie",
                "watched_at": fallback_watched_at,
                "title": title,
                "year": show.get("year"),
                "simkl_type": media_key,
                "imdb_id": ids.get("imdb"),
                "mal_id": str(ids["mal"]) if ids.get("mal") else None,
                "anilist_id": str(ids["anilist"]) if ids.get("anilist") else None,
                "root_mal_id": str(root_ids["root_mal"]) if root_ids.get("root_mal") else None,
                "root_anilist_id": str(root_ids["root_anilist"]) if root_ids.get("root_anilist") else None,
                "root_title": root_ids.get("root_title"),
                "root_episode_offset": int(root_ids["root_episode_offset"]) if root_ids.get("root_episode_offset") else 0,
                "anidb_id": str(ids["anidb"]) if ids.get("anidb") else None,
                "tvdb_id": str(ids["tvdb"]) if ids.get("tvdb") else None,
                "ids": ids,
            }]

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
                "year": show.get("year"),
                "simkl_type": media_key,
                "imdb_id": ids.get("imdb"),
                "mal_id": str(ids["mal"]) if ids.get("mal") else None,
                "anilist_id": str(ids["anilist"]) if ids.get("anilist") else None,
                "root_mal_id": str(root_ids["root_mal"]) if root_ids.get("root_mal") else None,
                "root_anilist_id": str(root_ids["root_anilist"]) if root_ids.get("root_anilist") else None,
                "root_title": root_ids.get("root_title"),
                "root_episode_offset": int(root_ids["root_episode_offset"]) if root_ids.get("root_episode_offset") else 0,
                "anidb_id": str(ids["anidb"]) if ids.get("anidb") else None,
                "tvdb_id": str(ids["tvdb"]) if ids.get("tvdb") else None,
                "ids": ids,
            })

        for season_entry in self._history_seasons(entry, show):
            season_number = season_entry.get("number") or season_entry.get("season")
            for episode_entry in season_entry.get("episodes", []) or []:
                add_episode(
                    season_number,
                    episode_entry.get("number") or episode_entry.get("episode"),
                    episode_entry.get("watched_at") or episode_entry.get("last_watched_at") or fallback_watched_at,
                )

        for episode_entry in self._history_episodes(entry, show):
            add_episode(
                episode_entry.get("season"),
                episode_entry.get("number") or episode_entry.get("episode"),
                episode_entry.get("watched_at") or episode_entry.get("last_watched_at") or fallback_watched_at,
            )

        for episode_entry in self._history_last_watched_episodes(entry, show):
            add_episode(
                episode_entry.get("season"),
                episode_entry.get("number") or episode_entry.get("episode"),
                episode_entry.get("watched_at") or episode_entry.get("last_watched_at") or fallback_watched_at,
            )

        synthesized = self._synthesize_episode_history_from_counts(entry, show, media_key)
        if not history:
            for episode in synthesized:
                if episode.get("aggregate_watched_count"):
                    history.append(episode)
                    continue
                add_episode(
                    episode.get("season"),
                    episode.get("number") or episode.get("episode"),
                    episode.get("watched_at") or episode.get("last_watched_at"),
                )
        elif media_key == "anime":
            for episode in synthesized:
                if episode.get("aggregate_watched_count"):
                    continue
                add_episode(
                    episode.get("season"),
                    episode.get("number") or episode.get("episode"),
                    episode.get("watched_at") or episode.get("last_watched_at") or fallback_watched_at,
                )

        return history

    @staticmethod
    def _is_movie_like_anime_history(entry: dict, show: dict) -> bool:
        anime_type = str(entry.get("anime_type") or show.get("anime_type") or show.get("type") or "").strip().lower()
        if anime_type in {"movie", "film"}:
            return True
        episodes = entry.get("episodes") or show.get("episodes")
        seasons = entry.get("seasons") or show.get("seasons")
        try:
            total_episodes = int(entry.get("total_episodes_count") or show.get("total_episodes_count") or 0)
        except (TypeError, ValueError):
            total_episodes = 0
        if total_episodes == 1 and not episodes and not seasons:
            return True
        return False

    def _normalize_playback_entry(self, entry: dict) -> dict | None:
        if not isinstance(entry, dict):
            return None

        movie = entry.get("movie")
        show = self._show_payload(entry)
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
                "tmdb_id": int(tmdb_id) if tmdb_id else None,
                "media_type": "movie",
                "position_ms": position_ms,
                "runtime_ms": runtime_ms,
                "progress": progress,
                "paused_at": entry.get("updated_at") or entry.get("paused_at"),
                "title": movie.get("title", "Unknown"),
                "year": movie.get("year"),
                "imdb_id": ids.get("imdb"),
                "ids": ids,
            }

        if isinstance(show, dict) and isinstance(episode, dict):
            show_ids = show.get("ids", {}) or {}
            tmdb_id = show_ids.get("tmdb")
            runtime_minutes = entry.get("runtime") or episode.get("runtime")
            progress = self._playback_progress_percent(entry)
            season = episode.get("season")
            number = episode.get("number") or episode.get("episode")
            if runtime_minutes in (None, 0) or progress is None or season is None or number is None:
                return None
            runtime_ms = int(float(runtime_minutes) * 60_000)
            position_ms = int(round(runtime_ms * (progress / 100.0)))
            return {
                "tmdb_id": int(tmdb_id) if tmdb_id else None,
                "media_type": "tv",
                "season": int(season),
                "episode": int(number),
                "position_ms": position_ms,
                "runtime_ms": runtime_ms,
                "progress": progress,
                "paused_at": entry.get("updated_at") or entry.get("paused_at"),
                "title": show.get("title", "Unknown"),
                "year": show.get("year"),
                "imdb_id": show_ids.get("imdb"),
                "mal_id": str(show_ids["mal"]) if show_ids.get("mal") else None,
                "anilist_id": str(show_ids["anilist"]) if show_ids.get("anilist") else None,
                "anidb_id": str(show_ids["anidb"]) if show_ids.get("anidb") else None,
                "tvdb_id": str(show_ids["tvdb"]) if show_ids.get("tvdb") else None,
                "ids": show_ids,
            }

        return None

    @staticmethod
    def _watched_history_key(item: dict) -> tuple:
        return (
            item.get("tmdb_id"),
            item.get("media_type"),
            item.get("season"),
            item.get("episode"),
            item.get("title"),
            item.get("watched_at"),
        )

    def _dedupe_watched_history(self, history: list[dict]) -> list[dict]:
        deduped: list[dict] = []
        seen: set[tuple] = set()
        for item in history:
            key = self._watched_history_key(item)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    @staticmethod
    def _parse_history_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        candidate = str(value).strip()
        if not candidate:
            return None
        try:
            if candidate.endswith("Z"):
                candidate = candidate[:-1] + "+00:00"
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _is_history_after(self, item: dict, since: str) -> bool:
        if item.get("cursor_exempt"):
            return True
        watched_at = self._parse_history_datetime(item.get("watched_at"))
        since_dt = self._parse_history_datetime(since)
        if since_dt is None:
            return True
        if watched_at is None:
            return False
        return watched_at > since_dt

    @staticmethod
    def _describe_history_entry(entry: dict) -> dict:
        show = SimklClient._show_payload(entry) if isinstance(entry, dict) else None
        return {
            "entry_keys": sorted(entry.keys()) if isinstance(entry, dict) else [],
            "show_keys": sorted(show.keys()) if isinstance(show, dict) else [],
            "title": show.get("title") if isinstance(show, dict) else None,
            "status": entry.get("status") if isinstance(entry, dict) else None,
            "has_seasons": bool(entry.get("seasons")) if isinstance(entry, dict) else False,
            "season_count": len(entry.get("seasons", []) or []) if isinstance(entry, dict) else 0,
            "has_episodes": bool(entry.get("episodes")) if isinstance(entry, dict) else False,
            "episode_count": len(entry.get("episodes", []) or []) if isinstance(entry, dict) else 0,
            "first_season_keys": sorted((entry.get("seasons", [{}]) or [{}])[0].keys()) if isinstance(entry, dict) and entry.get("seasons") else [],
            "first_episode_keys": sorted((entry.get("episodes", [{}]) or [{}])[0].keys()) if isinstance(entry, dict) and entry.get("episodes") else [],
        }

    @staticmethod
    def _show_payload(entry: dict) -> dict | None:
        for key in ("show", "anime", "series", "tv_show"):
            value = entry.get(key)
            if isinstance(value, dict):
                return value
        if isinstance(entry, dict) and isinstance(entry.get("ids"), dict):
            if entry.get("title") or entry.get("name"):
                return entry
        return None

    @staticmethod
    def _history_seasons(entry: dict, show: dict) -> list[dict]:
        sources = [entry.get("seasons"), show.get("seasons")]
        for value in sources:
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _history_episodes(entry: dict, show: dict) -> list[dict]:
        sources = [entry.get("episodes"), show.get("episodes")]
        for value in sources:
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _history_last_watched_episodes(entry: dict, show: dict) -> list[dict]:
        candidates = [
            entry.get("last_watched_episode"),
            show.get("last_watched_episode"),
            entry.get("last_episode"),
            show.get("last_episode"),
            entry.get("next_to_watch"),
            show.get("next_to_watch"),
        ]
        episodes: list[dict] = []
        for candidate in candidates:
            if isinstance(candidate, dict):
                episodes.append(candidate)
            elif isinstance(candidate, list):
                episodes.extend(item for item in candidate if isinstance(item, dict))
        return episodes

    def _synthesize_episode_history_from_counts(self, entry: dict, show: dict, media_key: str) -> list[dict]:
        if media_key != "anime":
            return []

        ids = show.get("ids", {}) or {}
        root_ids = self._resolve_anime_root_ids(ids)
        if root_ids.get("root_anilist"):
            ids["root_anilist"] = root_ids["root_anilist"]
        if root_ids.get("root_mal"):
            ids["root_mal"] = root_ids["root_mal"]
        watched_count = entry.get("watched_episodes_count")
        total_count = entry.get("total_episodes_count")
        try:
            watched_total = int(watched_count or 0)
        except (TypeError, ValueError):
            return []
        if watched_total <= 0:
            return []

        try:
            total_episodes = int(total_count or watched_total)
        except (TypeError, ValueError):
            total_episodes = watched_total

        watched_total = min(watched_total, total_episodes) if total_episodes > 0 else watched_total
        if watched_total <= 0:
            return []

        watched_at = (
            entry.get("last_watched_at")
            or entry.get("last_watched")
            or show.get("last_watched_at")
            or show.get("last_watched")
        )
        tmdb_raw = ids.get("tmdb")
        try:
            tmdb_id = int(tmdb_raw) if tmdb_raw else None
        except (TypeError, ValueError):
            tmdb_id = None

        if tmdb_id:
            episodes = self._episode_rows_from_tmdb_plan(tmdb_id, watched_total, watched_at)
            if episodes:
                return episodes

        return [{
            "tmdb_id": tmdb_id,
            "media_type": "tv",
            "simkl_type": media_key,
            "watched_at": watched_at,
            "title": show.get("title", "Unknown"),
            "year": show.get("year"),
            "imdb_id": ids.get("imdb"),
            "mal_id": str(ids["mal"]) if ids.get("mal") else None,
            "anilist_id": str(ids["anilist"]) if ids.get("anilist") else None,
            "root_mal_id": str(root_ids["root_mal"]) if root_ids.get("root_mal") else None,
            "root_anilist_id": str(root_ids["root_anilist"]) if root_ids.get("root_anilist") else None,
            "root_title": root_ids.get("root_title"),
            "root_episode_offset": int(root_ids["root_episode_offset"]) if root_ids.get("root_episode_offset") else 0,
            "anidb_id": str(ids["anidb"]) if ids.get("anidb") else None,
            "tvdb_id": str(ids["tvdb"]) if ids.get("tvdb") else None,
            "ids": ids,
            "aggregate_watched_count": watched_total,
            "aggregate_total_episodes": total_episodes,
            "cursor_exempt": True,
        }]

    def expand_aggregate_history_item(self, item: dict) -> list[dict]:
        watched_total = item.get("aggregate_watched_count")
        if watched_total in (None, "", 0):
            return []
        try:
            tmdb_id = int(item.get("tmdb_id") or 0)
            watched_total = int(watched_total)
        except (TypeError, ValueError):
            return []
        if tmdb_id <= 0 or watched_total <= 0:
            return []
        watched_at = item.get("watched_at")
        expanded = self._episode_rows_from_tmdb_plan(tmdb_id, watched_total, watched_at)
        return [
            {
                **item,
                "season": row["season"],
                "episode": row["number"],
            }
            for row in expanded
        ]

    def _episode_rows_from_tmdb_plan(self, tmdb_id: int, watched_total: int, watched_at: str | None) -> list[dict]:
        season_plan = self._get_tmdb_season_plan_cached(tmdb_id)
        if not season_plan:
            return []
        episodes: list[dict] = []
        remaining = watched_total
        positive_seasons = [(season_number, season_episodes) for season_number, season_episodes in season_plan if season_number > 0 and season_episodes > 0]
        for season_number, season_episodes in season_plan:
            if season_number <= 0 or season_episodes <= 0:
                continue
            take = min(remaining, season_episodes)
            episodes.extend(
                {"season": season_number, "number": episode_number, "watched_at": watched_at}
                for episode_number in range(1, take + 1)
            )
            remaining -= take
            if remaining <= 0:
                break
        if remaining > 0:
            if len(positive_seasons) == 1 and positive_seasons[0][0] == 1:
                known_episodes = positive_seasons[0][1]
                logger.info(
                    "Falling back to Season 1 overflow for aggregate SIMKL anime history on TMDB %s (%d known, %d watched, later seasons absent or 0-episode placeholders)",
                    tmdb_id,
                    known_episodes,
                    watched_total,
                )
                episodes.extend(
                    {"season": 1, "number": episode_number, "watched_at": watched_at}
                    for episode_number in range(known_episodes + 1, watched_total + 1)
                )
                return episodes
            logger.info(
                "Skipping aggregate SIMKL anime history for TMDB %s because TMDB season plan only covered %d/%d episodes",
                tmdb_id,
                watched_total - remaining,
                watched_total,
            )
            return []
        return episodes

    @classmethod
    def _get_tmdb_season_plan_cached(cls, tmdb_id: int) -> list[tuple[int, int]]:
        cache = getattr(cls, "_shared_tmdb_season_plan_cache", None)
        if cache is None:
            cache = {}
            setattr(cls, "_shared_tmdb_season_plan_cache", cache)
        if tmdb_id in cache:
            return list(cache[tmdb_id])
        plan = cls._fetch_tmdb_season_plan(tmdb_id)
        cache[tmdb_id] = list(plan)
        return list(plan)

    @staticmethod
    def _fetch_tmdb_season_plan(tmdb_id: int) -> list[tuple[int, int]]:
        url = f"https://www.themoviedb.org/tv/{tmdb_id}/seasons?language=en-US"
        try:
            response = requests.get(
                url,
                timeout=20,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; SyncMeta/1.0)",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            response.raise_for_status()
        except Exception as exc:
            logger.info("Failed to load TMDB season plan for %s: %s", tmdb_id, exc)
            return []

        html = response.text
        blocks = html.split('<div class="season_wrapper">')
        plan: list[tuple[int, int]] = []
        for block in blocks[1:]:
            season_match = re.search(rf'/tv/{tmdb_id}/season/(\d+)(?:\?language=en-US)?', block)
            episodes_match = re.search(r'(\d+)\s+Episodes', block)
            if not season_match or not episodes_match:
                continue
            try:
                season_number = int(season_match.group(1))
                episode_count = int(episodes_match.group(1))
            except (TypeError, ValueError):
                continue
            if episode_count <= 0:
                continue
            plan.append((season_number, episode_count))
        plan.sort(key=lambda item: item[0])
        logger.info("TMDB season plan for %s: %s", tmdb_id, plan)
        return plan

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
