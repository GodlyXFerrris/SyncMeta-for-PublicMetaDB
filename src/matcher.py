"""Matching logic to resolve SIMKL items to PublicMetaDB TMDB IDs."""

import logging
import threading
import time
from dataclasses import dataclass

import requests

from .publicmetadb_client import PublicMetaDBClient

logger = logging.getLogger(__name__)

# External ID types to try, in order, when TMDB is missing.
_DEFAULT_LOOKUP_CHAIN = [
    ("imdb", "imdb_id"),
    ("mal", "mal_id"),
    ("anilist", "anilist_id"),
    ("anidb", "anidb_id"),
    ("tvdb", "tvdb_id"),
]

# Anime usually has stronger AniList/MAL signals than IMDb. Prefer those first.
_ANIME_LOOKUP_CHAIN = [
    ("anilist", "anilist_id"),
    ("mal", "mal_id"),
    ("anidb", "anidb_id"),
    ("imdb", "imdb_id"),
    ("tvdb", "tvdb_id"),
]

_ROOT_LOOKUP_CHAIN = [
    ("mal", "root_mal_id"),
    ("anilist", "root_anilist_id"),
]

# Failed resolutions are remembered for this long before being retried.
_FAILED_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days


@dataclass(frozen=True)
class MatchResult:
    tmdb_id: int | None
    resolution_kind: str
    unresolved_reason: str | None = None
    match_confidence: str = "verified"
    anime_mapping_source: str | None = None
    candidate_tmdb_id: int | None = None


class ItemMatcher:
    """Resolve normalized items to TMDB IDs usable by PublicMetaDB."""

    def __init__(
        self,
        pmdb: PublicMetaDBClient,
        anime_root_resolver=None,
        initial_cache: dict | None = None,
        initial_failed_cache: dict[str, str] | None = None,
    ):
        self._pmdb = pmdb
        # Pre-populate with persisted resolutions from a previous sync run so
        # unchanged items resolve instantly without any external API calls.
        self._cache: dict[str, int | None] = dict(initial_cache) if initial_cache else {}
        # Lock protecting _cache and _failed_cache so concurrent provider syncs
        # (e.g. SIMKL shows + AniList anime) don't race on cache writes.
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._stats = {
            "lookups": 0,
            "cache_hits": 0,
            "failed_cache_hits": 0,
            "direct_hits": 0,
            "mapping_hits": 0,
            "root_hits": 0,
            "lookup_unavailable": 0,
            "missing_id_failures": 0,
            "not_found_failures": 0,
        }
        # Optional callable(anilist_id: int | None, mal_id: int | None) -> dict | None
        # Returns {"root": media_dict, ...} from the AniList prequel chain.
        # Used as a last resort for anime sequels that fail all direct lookups.
        self._anime_root_resolver = anime_root_resolver
        # Failed resolutions keyed by cache_key → unix timestamp of failure.
        # Items in here are skipped until the TTL expires, avoiding redundant
        # AniList chain-walks and PMDB lookups for permanently unresolvable entries.
        self._failed_cache: dict[str, float] = self._load_failed_cache(initial_failed_cache)
        self._failed_reason_cache: dict[str, str] = {}

    @staticmethod
    def _load_failed_cache(raw: dict[str, str] | None) -> dict[str, float]:
        """Convert persisted ISO-timestamp failed cache to float unix timestamps."""
        if not raw:
            return {}
        result: dict[str, float] = {}
        now = time.time()
        cutoff = now - _FAILED_CACHE_TTL_SECONDS
        for key, iso in raw.items():
            try:
                import datetime
                ts = datetime.datetime.fromisoformat(iso).timestamp()
                if ts > cutoff:
                    result[key] = ts
            except Exception:
                pass
        return result

    @property
    def resolution_cache(self) -> dict[str, int]:
        """Return only successful resolutions for persistence (excludes failures)."""
        return {k: v for k, v in self._cache.items() if isinstance(v, int)}

    @property
    def failed_resolution_cache(self) -> dict[str, str]:
        """Return recent failed resolutions as {cache_key: iso_timestamp} for persistence."""
        import datetime
        now = time.time()
        cutoff = now - _FAILED_CACHE_TTL_SECONDS
        return {
            key: datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()
            for key, ts in self._failed_cache.items()
            if ts > cutoff
        }

    def stats_snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def resolve_tmdb_id(self, item: dict) -> int | None:
        return self.resolve_match(item).tmdb_id

    def resolve_match(self, item: dict) -> MatchResult:
        """Return a detailed match result for a normalized item."""
        cache_key = self._cache_key(item)

        # Fast path: check caches without the lock (reads are safe in CPython).
        with self._lock:
            self._stats["lookups"] += 1
        if cache_key in self._cache:
            cached_tmdb_id = self._cache[cache_key]
            with self._lock:
                self._stats["cache_hits"] += 1
            if cached_tmdb_id is not None:
                return MatchResult(tmdb_id=cached_tmdb_id, resolution_kind="cache")
            return MatchResult(
                tmdb_id=None,
                resolution_kind="unresolved",
                unresolved_reason=self._failed_reason_cache.get(cache_key, "cached_miss"),
            )
        failed_at = self._failed_cache.get(cache_key)
        if failed_at is not None and (time.time() - failed_at) < _FAILED_CACHE_TTL_SECONDS:
            with self._lock:
                self._stats["failed_cache_hits"] += 1
            return MatchResult(
                tmdb_id=None,
                resolution_kind="unresolved",
                unresolved_reason=self._failed_reason_cache.get(cache_key, "cached_miss"),
            )

        # Coordinate cache misses per key so only duplicate lookups block each
        # other; distinct items can still resolve concurrently.
        with self._lock:
            if cache_key in self._cache:
                cached_tmdb_id = self._cache[cache_key]
                if cached_tmdb_id is not None:
                    return MatchResult(tmdb_id=cached_tmdb_id, resolution_kind="cache")
                return MatchResult(
                    tmdb_id=None,
                    resolution_kind="unresolved",
                    unresolved_reason=self._failed_reason_cache.get(cache_key, "cached_miss"),
                )
            failed_at = self._failed_cache.get(cache_key)
            if failed_at is not None and (time.time() - failed_at) < _FAILED_CACHE_TTL_SECONDS:
                return MatchResult(
                    tmdb_id=None,
                    resolution_kind="unresolved",
                    unresolved_reason=self._failed_reason_cache.get(cache_key, "cached_miss"),
                )
            in_flight = self._inflight.get(cache_key)
            if in_flight is None:
                in_flight = threading.Event()
                self._inflight[cache_key] = in_flight
                is_owner = True
            else:
                is_owner = False

        if not is_owner:
            in_flight.wait()
            tmdb_id = self._cache.get(cache_key)
            if tmdb_id is not None:
                return MatchResult(tmdb_id=tmdb_id, resolution_kind="cache")
            return MatchResult(
                tmdb_id=None,
                resolution_kind="unresolved",
                unresolved_reason=self._failed_reason_cache.get(cache_key, "cached_miss"),
            )

        try:
            result = self._try_resolve(item)
        except Exception:
            with self._lock:
                self._inflight.pop(cache_key, None)
                in_flight.set()
            raise

        with self._lock:
            self._cache[cache_key] = result.tmdb_id
            if result.tmdb_id is None:
                self._failed_cache[cache_key] = time.time()
                self._failed_reason_cache[cache_key] = result.unresolved_reason or "not_found"
            else:
                self._failed_cache.pop(cache_key, None)
                self._failed_reason_cache.pop(cache_key, None)
            self._inflight.pop(cache_key, None)
            in_flight.set()
            return result

    def _record_match_stat(self, result: MatchResult) -> None:
        if result.tmdb_id is not None:
            if result.resolution_kind == "direct_tmdb":
                self._stats["direct_hits"] += 1
            elif result.resolution_kind in {"external_mapping", "fribb_exact"}:
                self._stats["mapping_hits"] += 1
            elif result.resolution_kind == "root_series":
                self._stats["root_hits"] += 1
            return
        if result.unresolved_reason == "lookup_unavailable":
            self._stats["lookup_unavailable"] += 1
        elif result.unresolved_reason == "missing_ids":
            self._stats["missing_id_failures"] += 1
        else:
            self._stats["not_found_failures"] += 1

    def _lookup_external_mapping(self, id_type: str, ext_id: str, media_type: str) -> tuple[int | None, str]:
        try:
            detailed_lookup = getattr(self._pmdb, "lookup_by_external_id_detailed", None)
            if callable(detailed_lookup):
                detail = detailed_lookup(id_type, ext_id, media_type) or {}
                return detail.get("tmdb_id"), str(detail.get("status") or "miss")
            tmdb_id = self._pmdb.lookup_by_external_id(id_type, ext_id, media_type)
            return tmdb_id, "hit" if tmdb_id else "miss"
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code in {401, 403}:
                logger.warning(
                    "PMDB mapping lookup failed for %s=%s (%s) with %s; treating mapping lookup as unavailable",
                    id_type,
                    ext_id,
                    media_type,
                    status_code,
                )
                return None, "lookup_unavailable"
            raise

    def _try_resolve(self, item: dict) -> MatchResult:
        title = item.get("title", "Unknown")
        media_type = item["media_type"]
        is_anime = item.get("simkl_type") == "anime"
        ids = item.get("ids", {})
        anime_resolve_mode = self._anime_resolve_mode(item)
        allow_root_series = bool(item.get("allow_root_series")) or anime_resolve_mode in {"history_identity", "resume_identity"}

        if item.get("prefer_root_series") or allow_root_series:
            root_result = self._resolve_root_series(item, ids, media_type)
            if root_result.tmdb_id:
                with self._lock:
                    self._record_match_stat(root_result)
                return root_result

        if is_anime:
            # For list_identity: try PMDB external mapping FIRST — it has per-show
            # accurate TMDB IDs (e.g. Shippuden→1553, Boruto→65334). Fribb often
            # stores the franchise root TMDB for sequels, causing false duplicates.
            if anime_resolve_mode == "list_identity":
                for id_type, item_key in self._lookup_chain_for_item(item):
                    ext_id = item.get(item_key) or ids.get(id_type) or ids.get(item_key)
                    if not ext_id:
                        continue
                    ext_id = str(ext_id)
                    tmdb_id, status = self._lookup_external_mapping(id_type, ext_id, media_type)
                    if tmdb_id:
                        logger.debug("Resolved anime '%s' via PMDB %s mapping (%s -> %d)", title, id_type, ext_id, tmdb_id)
                        result = MatchResult(
                            tmdb_id=tmdb_id,
                            resolution_kind="external_mapping",
                            match_confidence="verified",
                            anime_mapping_source=id_type,
                        )
                        with self._lock:
                            self._record_match_stat(result)
                        return result

            # Fribb exact lookup — fallback for list_identity, primary for other modes.
            fribb_result = self._try_exact_anime_fribb_lookup(item)
            if fribb_result.tmdb_id:
                with self._lock:
                    self._record_match_stat(fribb_result)
                return fribb_result
            if anime_resolve_mode == "list_identity":
                unresolved_reason = fribb_result.unresolved_reason or "missing_anime_mapping"
                if unresolved_reason == "not_found":
                    unresolved_reason = "missing_anime_mapping"
                return MatchResult(
                    tmdb_id=None,
                    resolution_kind="unresolved",
                    unresolved_reason=unresolved_reason,
                    match_confidence=fribb_result.match_confidence or "unresolved",
                    anime_mapping_source=fribb_result.anime_mapping_source or "fribb_exact",
                    candidate_tmdb_id=fribb_result.candidate_tmdb_id,
                )

        # Do NOT collapse anime list identity to the franchise root up front.
        # Root-chain lookups are useful for history remapping, but for title/list
        # sync they can incorrectly turn Boruto into Naruto or UBW into Fate/Zero.
        # Prefer direct external mappings and verified direct TMDB first.

        if not is_anime:
            tmdb_raw = item.get("tmdb_id")
            if tmdb_raw:
                try:
                    tmdb_id = int(tmdb_raw)
                    logger.debug("Resolved '%s' via direct TMDB ID: %d", title, tmdb_id)
                    result = MatchResult(
                        tmdb_id=tmdb_id,
                        resolution_kind="direct_tmdb",
                        match_confidence="verified",
                        anime_mapping_source="direct_tmdb" if is_anime else None,
                    )
                    with self._lock:
                        self._record_match_stat(result)
                    return result
                except (ValueError, TypeError):
                    pass

        had_lookup_candidate = False
        lookup_unavailable = False

        for id_type, item_key in self._lookup_chain_for_item(item):
            ext_id = item.get(item_key) or ids.get(id_type) or ids.get(item_key)
            if not ext_id:
                continue
            had_lookup_candidate = True
            ext_id = str(ext_id)
            tmdb_id, status = self._lookup_external_mapping(id_type, ext_id, media_type)
            if tmdb_id:
                logger.debug("Resolved '%s' via %s lookup (%s -> %d)", title, id_type, ext_id, tmdb_id)
                result = MatchResult(
                    tmdb_id=tmdb_id,
                    resolution_kind="external_mapping",
                    match_confidence="verified",
                    anime_mapping_source=id_type if is_anime else None,
                )
                with self._lock:
                    self._record_match_stat(result)
                return result
            if status == "lookup_unavailable":
                lookup_unavailable = True

        if (not is_anime and not item.get("prefer_root_series")) or (is_anime and allow_root_series):
            root_result = self._resolve_root_series(item, ids, media_type)
            if root_result.tmdb_id:
                with self._lock:
                    self._record_match_stat(root_result)
                return root_result
            if root_result.unresolved_reason == "lookup_unavailable":
                lookup_unavailable = True

        tmdb_raw = item.get("tmdb_id")
        if tmdb_raw:
            if not is_anime or self._can_accept_anime_direct_tmdb(item):
                try:
                    tmdb_id = int(tmdb_raw)
                    logger.debug("Resolved '%s' via direct TMDB ID: %d", title, tmdb_id)
                    result = MatchResult(
                        tmdb_id=tmdb_id,
                        resolution_kind="direct_tmdb",
                        match_confidence="verified",
                        anime_mapping_source="direct_tmdb" if is_anime else None,
                    )
                    with self._lock:
                        self._record_match_stat(result)
                    return result
                except (ValueError, TypeError):
                    pass
            elif had_lookup_candidate:
                logger.warning(
                    "Skipping direct TMDB fallback for SIMKL anime '%s' because its AniList/MAL identity could not be verified",
                    title,
                )

        if not had_lookup_candidate and not item.get("tmdb_id"):
            result = MatchResult(
                tmdb_id=None,
                resolution_kind="unresolved",
                unresolved_reason="missing_ids",
                match_confidence="unresolved",
            )
            with self._lock:
                self._record_match_stat(result)
            return result

        logger.warning(
            "Could not resolve TMDB ID for '%s' (year=%s, ids=%s)",
            title,
            item.get("year"),
            {k: v for k, v in ids.items() if v},
        )
        result = MatchResult(
            tmdb_id=None,
            resolution_kind="unresolved",
            unresolved_reason="lookup_unavailable" if lookup_unavailable else "not_found",
            match_confidence="unresolved",
        )
        with self._lock:
            self._record_match_stat(result)
        return result

    def _resolve_root_series(self, item: dict, ids: dict, media_type: str) -> MatchResult:
        title = item.get("title", "Unknown")
        lookup_unavailable = False
        for id_type, item_key in _ROOT_LOOKUP_CHAIN:
            ext_id = item.get(item_key) or ids.get(item_key) or ids.get(f"root_{id_type}")
            if not ext_id:
                continue
            ext_id = str(ext_id)
            tmdb_id, status = self._lookup_external_mapping(id_type, ext_id, media_type)
            if tmdb_id:
                logger.info(
                    "Resolved '%s' via root-series %s lookup (%s -> %d, root='%s')",
                    title, id_type, ext_id, tmdb_id,
                    item.get("root_title") or "Unknown",
                )
                return MatchResult(
                    tmdb_id=tmdb_id,
                    resolution_kind="root_series",
                    match_confidence="verified",
                    anime_mapping_source="root_chain",
                )
            if status == "lookup_unavailable":
                lookup_unavailable = True
        return MatchResult(
            tmdb_id=None,
            resolution_kind="unresolved",
            unresolved_reason="lookup_unavailable" if lookup_unavailable else "not_found",
            match_confidence="unresolved",
            anime_mapping_source="root_chain",
        )

    def _try_exact_anime_fribb_lookup(self, item: dict) -> MatchResult:
        """Use exact AniList/MAL anime-lists mappings before broader fallback logic."""
        from . import fribb_client

        ids = item.get("ids", {})
        entry = None

        lookup_order = (
            ("anilist", item.get("anilist_id") or ids.get("anilist"), fribb_client.lookup_by_anilist),
            ("mal", item.get("mal_id") or ids.get("mal"), fribb_client.lookup_by_mal),
            ("anidb", item.get("anidb_id") or ids.get("anidb"), fribb_client.lookup_by_anidb),
            ("simkl", ids.get("simkl"), fribb_client.lookup_by_simkl),
            ("imdb", item.get("imdb_id") or ids.get("imdb"), fribb_client.lookup_by_imdb),
        )
        exact_source = None
        exact_value = None
        for source_name, raw_value, lookup_fn in lookup_order:
            if not raw_value:
                continue
            try:
                lookup_value = int(raw_value) if source_name != "imdb" else str(raw_value)
                entry = lookup_fn(lookup_value)
            except (TypeError, ValueError):
                entry = None
            if entry is not None:
                exact_source = source_name
                exact_value = raw_value
                break

        if not isinstance(entry, dict):
            return MatchResult(
                tmdb_id=None,
                resolution_kind="unresolved",
                unresolved_reason="not_found",
                match_confidence="unresolved",
                anime_mapping_source="fribb_exact",
            )

        tmdb_raw = entry.get("themoviedb")
        try:
            tmdb_id = int(tmdb_raw) if tmdb_raw else None
        except (TypeError, ValueError):
            tmdb_id = None
        if not tmdb_id:
            return MatchResult(
                tmdb_id=None,
                resolution_kind="unresolved",
                unresolved_reason="not_found",
                match_confidence="unresolved",
                anime_mapping_source="fribb_exact",
            )

        fribb_type = str(entry.get("type") or "").strip().lower()
        expected_media_type = "movie" if fribb_type == "movie" else "tv"
        if str(item.get("media_type") or "").strip().lower() != expected_media_type:
            return MatchResult(
                tmdb_id=None,
                resolution_kind="unresolved",
                unresolved_reason="not_found",
                match_confidence="ambiguous",
                anime_mapping_source="fribb_exact",
                candidate_tmdb_id=tmdb_id,
            )

        logger.debug(
            "Resolved anime '%s' via exact Fribb mapping (%s -> %d)",
            item.get("title", "Unknown"),
            exact_value or item.get("anilist_id") or item.get("mal_id") or ids.get("mal"),
            tmdb_id,
        )
        return MatchResult(
            tmdb_id=tmdb_id,
            resolution_kind="fribb_exact",
            match_confidence="exact",
            anime_mapping_source=f"fribb_exact:{exact_source}" if exact_source else "fribb_exact",
        )

    def _can_accept_anime_direct_tmdb(self, item: dict) -> bool:
        """Allow raw TMDB only after the anime identity is verified."""
        if item.get("simkl_type") != "anime":
            return True
        identity_present = bool(
            item.get("anilist_id")
            or item.get("mal_id")
            or item.get("anidb_id")
            or (item.get("ids") or {}).get("anilist")
            or (item.get("ids") or {}).get("mal")
            or (item.get("ids") or {}).get("anidb")
        )
        try:
            tmdb_raw = item.get("tmdb_id")
            if tmdb_raw:
                from . import fribb_client
                exact_match = self._try_exact_anime_fribb_lookup(item)
                if exact_match.tmdb_id is not None:
                    return int(tmdb_raw) == int(exact_match.tmdb_id)
                anilist_id = item.get("anilist_id") or (item.get("ids") or {}).get("anilist")
                mal_id = item.get("mal_id") or (item.get("ids") or {}).get("mal")
                anidb_id = item.get("anidb_id") or (item.get("ids") or {}).get("anidb")
                simkl_id = (item.get("ids") or {}).get("simkl")
                imdb_id = item.get("imdb_id") or (item.get("ids") or {}).get("imdb")
                exact_entry = None
                if anilist_id:
                    exact_entry = fribb_client.lookup_by_anilist(int(anilist_id))
                if exact_entry is None and mal_id:
                    exact_entry = fribb_client.lookup_by_mal(int(mal_id))
                if exact_entry is None and anidb_id:
                    exact_entry = fribb_client.lookup_by_anidb(int(anidb_id))
                if exact_entry is None and simkl_id:
                    exact_entry = fribb_client.lookup_by_simkl(int(simkl_id))
                if exact_entry is None and imdb_id:
                    exact_entry = fribb_client.lookup_by_imdb(str(imdb_id))
                if exact_entry is not None:
                    fribb_type = str(exact_entry.get("type") or "").strip().lower()
                    expected_media_type = "movie" if fribb_type == "movie" else "tv"
                    item_media_type = str(item.get("media_type") or "").strip().lower()
                    if item_media_type == expected_media_type:
                        return fribb_client.validate_tmdb(exact_entry, int(tmdb_raw))
        except Exception:
            logger.debug("Anime direct TMDB verification failed; trying softer fallback", exc_info=True)
        if not self._anime_root_resolver:
            return identity_present

        anilist_id: int | None = None
        mal_id: int | None = None
        try:
            if item.get("anilist_id"):
                anilist_id = int(item["anilist_id"])
            if item.get("mal_id"):
                mal_id = int(item["mal_id"])
        except (TypeError, ValueError):
            return identity_present and str(item.get("media_type") or "").strip().lower() == "movie"

        if not anilist_id and not mal_id:
            return identity_present and str(item.get("media_type") or "").strip().lower() == "movie"

        try:
            root_context = self._anime_root_resolver(anilist_id, mal_id)
        except Exception:
            logger.debug("Anime identity verification failed", exc_info=True)
            return identity_present and str(item.get("media_type") or "").strip().lower() == "movie"
        root = (root_context or {}).get("root") if isinstance(root_context, dict) else None
        return isinstance(root, dict) and bool(root.get("id"))

    @staticmethod
    def _lookup_chain_for_item(item: dict) -> list[tuple[str, str]]:
        if item.get("simkl_type") == "anime":
            return _ANIME_LOOKUP_CHAIN
        return _DEFAULT_LOOKUP_CHAIN

    @staticmethod
    def _anime_resolve_mode(item: dict) -> str:
        mode = str(item.get("anime_resolve_mode") or "").strip().lower()
        if mode in {"list_identity", "history_identity", "resume_identity"}:
            return mode
        return "generic"

    def _try_anime_root_lookup(self, item: dict, media_type: str) -> MatchResult:
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
            return MatchResult(tmdb_id=None, resolution_kind="unresolved", unresolved_reason="missing_ids")

        root_context = self._anime_root_resolver(anilist_id, mal_id)
        root = (root_context or {}).get("root") if isinstance(root_context, dict) else None
        if not isinstance(root, dict):
            return MatchResult(tmdb_id=None, resolution_kind="unresolved", unresolved_reason="not_found")

        # If root is the same item, it's already a root series — let direct lookup handle it.
        if root.get("id") and root.get("id") == anilist_id:
            return MatchResult(tmdb_id=None, resolution_kind="unresolved", unresolved_reason="not_found")

        for id_type, root_key in [("mal", "idMal"), ("anilist", "id")]:
            root_ext_id = root.get(root_key)
            if root_ext_id:
                tmdb_id, status = self._lookup_external_mapping(id_type, str(root_ext_id), media_type)
                if tmdb_id:
                    logger.info(
                        "Resolved '%s' via root-series %s lookup (%s -> %d, root='%s')",
                        title, id_type, root_ext_id, tmdb_id,
                        self._media_title(root),
                    )
                    return MatchResult(tmdb_id=tmdb_id, resolution_kind="root_series")
                if status == "lookup_unavailable":
                    return MatchResult(tmdb_id=None, resolution_kind="unresolved", unresolved_reason="lookup_unavailable")
        return MatchResult(tmdb_id=None, resolution_kind="unresolved", unresolved_reason="not_found")

    @staticmethod
    def _media_title(media: dict) -> str:
        titles = media.get("title") or {}
        if isinstance(titles, dict):
            return titles.get("english") or titles.get("romaji") or str(media.get("id", "?"))
        return str(titles) or str(media.get("id", "?"))

    @staticmethod
    def _cache_key(item: dict) -> str:
        ids = item.get("ids", {})
        anilist_id = item.get("anilist_id") or ids.get("anilist") or ""
        resolver_mode = str(item.get("anime_resolve_mode") or "")
        return (
            f"{item.get('media_type', '')}:"
            f"{resolver_mode}:"
            f"{ids.get('simkl', '')}:"
            f"{item.get('imdb_id', '')}:"
            f"{item.get('tmdb_id', '')}:"
            f"{ids.get('mal', '')}:"
            f"{anilist_id}:"
            f"{ids.get('root_mal', '')}:"
            f"{ids.get('root_anilist', '')}:"
            f"{item.get('title', '')}:{item.get('year', '')}"
        )
