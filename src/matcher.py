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

    def __init__(self, pmdb: PublicMetaDBClient):
        self._pmdb = pmdb
        self._cache: dict[str, int | None] = {}

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

        tmdb_raw = item.get("tmdb_id")
        if tmdb_raw:
            try:
                tmdb_id = int(tmdb_raw)
                logger.debug("Resolved '%s' via direct TMDB ID: %d", title, tmdb_id)
                return tmdb_id
            except (ValueError, TypeError):
                pass

        ids = item.get("ids", {})
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
                    title,
                    id_type,
                    ext_id,
                    tmdb_id,
                    item.get("root_title") or "Unknown",
                )
                return tmdb_id

        logger.warning(
            "Could not resolve TMDB ID for '%s' (year=%s, ids=%s)",
            title,
            item.get("year"),
            {k: v for k, v in ids.items() if v},
        )
        return None

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
