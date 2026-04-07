"""Fribb anime-lists client.

Downloads and caches the Fribb anime-mapping project's full list, which maps
AniList / MAL IDs to TVDB seasons and TMDB IDs.  Used to resolve SIMKL history
episodes to the correct TVDB season before applying PMDB anime-seasons mappings.

Source: https://github.com/Fribb/anime-lists
"""

import logging
import threading

import requests

logger = logging.getLogger(__name__)

_LIST_URL = (
    "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
)

# Module-level caches populated once per process.
_by_anilist: dict[int, dict] = {}
_by_mal: dict[int, dict] = {}
_loaded = False
_lock = threading.Lock()


def _ensure_loaded() -> bool:
    global _loaded
    if _loaded:
        return True
    with _lock:
        if _loaded:
            return True
        try:
            logger.info("Downloading Fribb anime-lists from GitHub…")
            resp = requests.get(_LIST_URL, timeout=60)
            resp.raise_for_status()
            data: list[dict] = resp.json()
            for entry in data:
                anilist_id = entry.get("anilist_id")
                mal_id = entry.get("mal_id")
                if anilist_id:
                    try:
                        _by_anilist[int(anilist_id)] = entry
                    except (TypeError, ValueError):
                        pass
                if mal_id:
                    try:
                        _by_mal[int(mal_id)] = entry
                    except (TypeError, ValueError):
                        pass
            _loaded = True
            logger.info(
                "Fribb anime-lists loaded: %d anilist entries, %d mal entries",
                len(_by_anilist),
                len(_by_mal),
            )
            return True
        except Exception as exc:
            logger.warning("Failed to load Fribb anime-lists: %s", exc)
            return False


def lookup_by_anilist(anilist_id: int) -> dict | None:
    """Return the Fribb entry for an AniList ID, or None if not found."""
    if not _ensure_loaded():
        return None
    return _by_anilist.get(int(anilist_id))


def lookup_by_mal(mal_id: int) -> dict | None:
    """Return the Fribb entry for a MAL ID, or None if not found."""
    if not _ensure_loaded():
        return None
    return _by_mal.get(int(mal_id))
