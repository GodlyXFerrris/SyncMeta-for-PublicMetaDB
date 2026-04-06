"""Matching logic to resolve SIMKL items to PublicMetaDB TMDB IDs."""

import logging

from .publicmetadb_client import PublicMetaDBClient

logger = logging.getLogger(__name__)

# External ID types to try, in order, when TMDB is missing
_LOOKUP_CHAIN = [
    ("imdb", "imdb_id"),
    ("mal", "mal_id"),
    ("anilist", "anilist_id"),
    ("anidb", "anidb_id"),
    ("tvdb", "tvdb_id"),
]

_ROOT_LOOKUP_CHAIN = [
    ("mal", "root_mal_id"),
    ("anilist", "root_anilist_id"),
]


class ItemMatcher:
    """Resolve normalized items to TMDB IDs usable by PublicMetaDB."""

    def __init__(self, pmdb: PublicMetaDBClient, anime_root_resolver=None, initial_cache: dict | None = None):
        self._pmdb = pmdb
        # Pre-populate with persisted resolutions from a previous sync run so
        # unchanged items resolve instantly without any external API calls.
        self._cache: dict[str, int | None] = dict(initial_cache) if initial_cache else {}
        # Optional callable(anilist_id: int | None, mal_id: int | None) -> dict | None
        # Returns {"root": media_dict, ...} from the AniList prequel chain.
        # Used as a last resort for anime sequels that fail all direct lookups.
        self._anime_root_resolver = anime_root_resolver

    @property
    def resolution_cache(self) -> dict[str, int]:
        """Return only successful resolutions for persistence (excludes failures)."""
        return {k: v for k, v in self._cache.items() if isinstance(v, int)}

    def resolve_tmdb_id(self, item: dict) -> int | None:
        """Return a TMDB ID for a normalized item, or None if unresolvable."""
        cache_key = self._cache_key(item)
        if cache_key in self._cache:
            return self._cache[cache_key]

        tmdb_id = self._try_resolve(item)
        self._cache[cache_key] = tmdb_id
        return tmdb_id

    def _try_resolve(self, item: dict) -> int | None:
        title = item.get("title", "Unknown")
        media_type = item["media_type"]
        is_anime = item.get("simkl_type") == "anime"

        tmdb_raw = item.get("tmdb_id")
        if tmdb_raw:
            try:
                tmdb_id = int(tmdb_raw)
                logger.debug("Resolved '%s' via direct TMDB ID: %d", title, tmdb_id)
                return tmdb_id
            except (ValueError, TypeError):
                pass

        ids = item.get("ids", {})

        # For anime with an AniList ID: walk the prequel chain BEFORE trying direct
        # MAL/AniList lookups. This ensures sequels resolve to the root-series TMDB
        # entry (with an English title) rather than a season-specific entry that PMDB
        # may have mapped to a Japanese-titled TMDB record.
        if is_anime and self._anime_root_resolver and item.get("anilist_id"):
            tmdb_id = self._try_anime_root_lookup(item, media_type)
            if tmdb_id:
                return tmdb_id

        for id_type, item_key in _LOOKUP_CHAIN:
            ext_id = item.get(item_key) or ids.get(id_type) or ids.get(item_key)
            if not ext_id:
                continue
            ext_id = str(ext_id)
            tmdb_id = self._pmdb.lookup_by_external_id(id_type, ext_id, media_type)
            if tmdb_id:
                logger.debug("Resolved '%s' via %s lookup (%s -> %d)", title, id_type, ext_id, tmdb_id)
                return tmdb_id

        for id_type, item_key in _ROOT_LOOKUP_CHAIN:
            ext_id = item.get(item_key) or ids.get(item_key) or ids.get(f"root_{id_type}")
            if not ext_id:
                continue
            ext_id = str(ext_id)
            tmdb_id = self._pmdb.lookup_by_external_id(id_type, ext_id, media_type)
            if tmdb_id:
                logger.info(
                    "Resolved '%s' via root-series %s lookup (%s -> %d, root='%s')",
                    title, id_type, ext_id, tmdb_id,
                    item.get("root_title") or "Unknown",
                )
                return tmdb_id

        # Anime without an AniList ID: try root resolver as last resort using MAL fallback.
        if is_anime and self._anime_root_resolver and not item.get("anilist_id"):
            tmdb_id = self._try_anime_root_lookup(item, media_type)
            if tmdb_id:
                return tmdb_id

        logger.warning(
            "Could not resolve TMDB ID for '%s' (year=%s, ids=%s)",
            title,
            item.get("year"),
            {k: v for k, v in ids.items() if v},
        )
        return None

    def _try_anime_root_lookup(self, item: dict, media_type: str) -> int | None:
        """Walk the AniList prequel chain and look up the root-series TMDB ID."""
        title = item.get("title", "Unknown")
        anilist_id: int | None = None
        mal_id: int | None = None
        try:
            if item.get("anilist_id"):
                anilist_id = int(item["anilist_id"])
            if item.get("mal_id"):
                mal_id = int(item["mal_id"])
        except (ValueError, TypeError):
            pass

        if not anilist_id and not mal_id:
            return None

        root_context = self._anime_root_resolver(anilist_id, mal_id)
        root = (root_context or {}).get("root") if isinstance(root_context, dict) else None
        if not isinstance(root, dict):
            return None

        # If root is the same item, it's already a root series — let direct lookup handle it.
        if root.get("id") and root.get("id") == anilist_id:
            return None

        for id_type, root_key in [("mal", "idMal"), ("anilist", "id")]:
            root_ext_id = root.get(root_key)
            if root_ext_id:
                tmdb_id = self._pmdb.lookup_by_external_id(id_type, str(root_ext_id), media_type)
                if tmdb_id:
                    logger.info(
                        "Resolved '%s' via root-series %s lookup (%s -> %d, root='%s')",
                        title, id_type, root_ext_id, tmdb_id,
                        self._media_title(root),
                    )
                    return tmdb_id
        return None

    @staticmethod
    def _media_title(media: dict) -> str:
        titles = media.get("title") or {}
        if isinstance(titles, dict):
            return titles.get("english") or titles.get("romaji") or str(media.get("id", "?"))
        return str(titles) or str(media.get("id", "?"))

    @staticmethod
    def _cache_key(item: dict) -> str:
        ids = item.get("ids", {})
        return (
            f"{ids.get('simkl', '')}:"
            f"{item.get('imdb_id', '')}:"
            f"{item.get('tmdb_id', '')}:"
            f"{ids.get('mal', '')}:"
            f"{ids.get('root_mal', '')}:"
            f"{ids.get('root_anilist', '')}:"
            f"{item.get('title', '')}:{item.get('year', '')}"
        )
