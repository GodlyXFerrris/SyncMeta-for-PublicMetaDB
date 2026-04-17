"""Core sync logic for syncing configured sources into PublicMetaDB."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from .anilist_client import AniListClient
from .config import AniListConfig, AppConfig
from .matcher import ItemMatcher
from .mdblist_client import MdbListClient
from .publicmetadb_client import PublicMetaDBClient
from .simkl_client import SimklClient
from .trakt_client import TraktClient

logger = logging.getLogger(__name__)

_LIST_WRITE_WORKERS = 3
_LIST_RESOLVE_WORKERS = 3
_ACTIVITY_WRITE_WORKERS = 3
_MAPPING_WRITE_WORKERS = 3
_ANILIST_PREWARM_LIMIT = 200
_FUTURE_POLL_INTERVAL = 0.25


class SyncCancelled(Exception):
    """Raised when a running sync has been asked to stop."""

_TYPE_LABELS = {
    "shows": "Series",
    "movies": "Movies",
    "anime": "Anime",
}

_STATUS_LABELS = {
    "watching": "Watching",
    "plantowatch": "Plan to Watch",
    "completed": "Completed",
    "hold": "On Hold",
    "dropped": "Dropped",
    "watchlist": "Watchlist",
    "CURRENT": "Watching",
    "PLANNING": "Planning",
    "COMPLETED": "Completed",
    "PAUSED": "Paused",
    "DROPPED": "Dropped",
    "COMPLETED_ONA": "Completed ONA",
    "COMPLETED_OVA": "Completed OVA",
    "COMPLETED_MOVIE": "Completed Movie",
}


def _status_list_name(media_type: str, status: str) -> str:
    """Build a PMDB list name like 'Plan to Watch - Anime'."""
    type_label = _TYPE_LABELS.get(media_type, media_type.title())
    status_label = _STATUS_LABELS.get(status, status.replace("_", " ").title())
    return f"{status_label} - {type_label}"


def _display_status_name(media_type: str, status: str) -> str:
    type_label = _TYPE_LABELS.get(media_type, media_type.title())
    status_label = _STATUS_LABELS.get(status, status.replace("_", " ").title())
    return f"{status_label} - {type_label}"


@dataclass
class SyncStats:
    """Counters for a single list sync."""

    list_name: str = ""
    display_name: str = ""
    source_name: str = ""
    items_fetched: int = 0
    items_resolved: int = 0
    items_added: int = 0
    items_removed: int = 0
    items_skipped_duplicate: int = 0
    items_skipped_unresolved: int = 0
    errors: list[str] = field(default_factory=list)
    history_cursor: str = ""
    unresolved_items: list[dict] = field(default_factory=list)
    phase_timings: dict[str, float] = field(default_factory=dict)
    match_breakdown: dict[str, int] = field(default_factory=dict)
    unresolved_reason_counts: dict[str, int] = field(default_factory=dict)
    pmdb_metrics: dict[str, int] = field(default_factory=dict)


def _unresolved_item_summary(item: dict, list_name: str = "", unresolved_reason: str = "") -> dict:
    """Extract a compact, serialisable record for an item that could not be resolved."""
    ids = item.get("ids") or {}
    summary = {
        "title": item.get("title") or "Unknown",
        "year": item.get("year"),
        "media_type": item.get("media_type"),
        "simkl_type": item.get("simkl_type"),
        "tmdb_id": item.get("tmdb_id"),
        "imdb_id": item.get("imdb_id") or ids.get("imdb"),
        "mal_id": item.get("mal_id") or ids.get("mal"),
        "anilist_id": item.get("anilist_id") or ids.get("anilist"),
        "root_mal_id": item.get("root_mal_id") or ids.get("root_mal"),
        "root_anilist_id": item.get("root_anilist_id") or ids.get("root_anilist"),
        "anidb_id": item.get("anidb_id") or ids.get("anidb"),
        "tvdb_id": item.get("tvdb_id") or ids.get("tvdb"),
        "simkl_id": ids.get("simkl"),
        "list_name": list_name,
        "cache_key": ItemMatcher._cache_key(item),
    }
    if unresolved_reason:
        summary["unresolved_reason"] = unresolved_reason
    if item.get("simkl_type") == "anime":
        summary.update({
            "root_episode_offset": item.get("root_episode_offset") or 0,
            "has_root_ids": bool(item.get("root_anilist_id") or item.get("root_mal_id") or ids.get("root_anilist") or ids.get("root_mal")),
            "has_anime_ids": bool(item.get("anilist_id") or item.get("mal_id") or ids.get("anilist") or ids.get("mal")),
        })
    return summary


class SyncService:
    """Orchestrates one-way sync into PublicMetaDB."""

    def __init__(
        self,
        config: AppConfig,
        status_callback=None,
        progress_callback=None,
        managed_lists: list[dict] | None = None,
        cancel_requested_callback=None,
        sync_modes: dict | None = None,
        resolution_cache: dict | None = None,
        failed_resolution_cache: dict | None = None,
        manual_list_additions: dict | None = None,
    ):
        self._config = config
        self._simkl = SimklClient(config.simkl, cancel_requested_callback=cancel_requested_callback)
        self._trakt = TraktClient(config.trakt, cancel_requested_callback=cancel_requested_callback)
        self._mdblist = MdbListClient(config.mdblist, cancel_requested_callback=cancel_requested_callback)
        self._pmdb = PublicMetaDBClient(config.pmdb, cancel_requested_callback=cancel_requested_callback)
        self._anilist_root_client = AniListClient(
            config.anilist if config.anilist.enabled else AniListConfig(),
            cancel_requested_callback=cancel_requested_callback,
        )
        self._matcher = ItemMatcher(
            self._pmdb,
            anime_root_resolver=self._make_anime_root_resolver(),
            initial_cache=resolution_cache,
            initial_failed_cache=failed_resolution_cache,
        )
        self._status_callback = status_callback
        self._progress_callback = progress_callback
        self._managed_lists = self._normalize_managed_lists(managed_lists)
        self._cancel_requested_callback = cancel_requested_callback
        self._sync_modes = self._normalize_sync_modes(sync_modes, config)
        self._last_progress_publish = 0.0
        self._live_progress_rows: dict[str, dict] = {}
        self._mapping_contribution_lock = threading.Lock()
        self._contributed_mapping_keys: set[tuple[int, str, str, str]] = set()
        self._fribb_lookup_cache: dict[tuple[str, str], dict | None] = {}
        self._anime_seasons_cache: dict[int, list[dict]] = {}
        self._manual_list_additions: dict[str, list[dict]] = manual_list_additions or {}
        self._anime_history_remap_cache: dict[tuple, dict | None] = {}
        self._pmdb_cache_lock = threading.Lock()
        self._pmdb_run_list_index: dict[str, dict] | None = None
        self._pmdb_list_items_cache: dict[str, list[dict]] = {}

    def _make_anime_root_resolver(self):
        """Return a callable used by ItemMatcher to lazily walk AniList prequel chains."""
        client = self._anilist_root_client

        def resolver(anilist_id: int | None, mal_id: int | None) -> dict | None:
            if not anilist_id and mal_id:
                anilist_id = client.get_anilist_id_by_mal(mal_id)
            if not anilist_id:
                return None
            get_ctx = getattr(client, "_get_root_context", None)
            if callable(get_ctx):
                return get_ctx(anilist_id)
            return None

        return resolver

    @property
    def resolution_cache(self) -> dict[str, int]:
        return self._matcher.resolution_cache

    @property
    def failed_resolution_cache(self) -> dict[str, str]:
        return self._matcher.failed_resolution_cache

    @property
    def simkl(self) -> SimklClient:
        return self._simkl

    @property
    def managed_lists(self) -> list[dict]:
        return [dict(item) for item in sorted(self._managed_lists.values(), key=lambda item: item["list_name"])]

    def run(self) -> list[SyncStats]:
        """Execute a full sync cycle. Returns stats per synced list."""
        self._check_cancelled()
        self._set_status("Preparing sync")
        flags = []
        if self._config.sync.dry_run:
            flags.append("DRY RUN")
        if self._config.sync.remove_missing:
            flags.append("remove_missing")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        logger.info("═" * 60)
        logger.info("▶  SYNC STARTED%s", flag_str)
        logger.info("   modes: %s", ", ".join(k for k, v in self._sync_modes.items() if v) or "none")
        logger.info("═" * 60)

        all_stats: list[SyncStats] = []
        if self._sync_modes["lists"]:
            logger.info("── List Sync ──────────────────────────────────────────")
            self._prime_pmdb_list_index()
            anilist_enabled = (
                self._config.anilist.enabled
                and bool(self._config.anilist.selected_statuses)
            )
            if anilist_enabled:
                # Run SIMKL and AniList concurrently — they fetch from independent
                # APIs and write to distinct PMDB lists so there is no data race.
                # ItemMatcher already uses a threading.Lock for its shared cache.
                # NOTE: SyncCancelled must be detected via the future's exception
                # and re-raised here, not swallowed by the generic except clause.
                cancelled = False
                pool = ThreadPoolExecutor(max_workers=2)
                shutdown_wait = True
                try:
                    future_simkl = pool.submit(self._sync_simkl)
                    future_anilist = pool.submit(self._sync_anilist)
                    for future in self._iter_completed_futures([future_simkl, future_anilist]):
                        try:
                            all_stats.extend(future.result())
                        except SyncCancelled:
                            cancelled = True
                        except Exception as exc:
                            logger.error("Provider sync failed: %s", exc)
                except SyncCancelled:
                    shutdown_wait = False
                    raise
                finally:
                    pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)
                if cancelled:
                    raise SyncCancelled("Sync stopped by user")
            else:
                all_stats.extend(self._sync_simkl())
            self._publish_progress(all_stats, force=True)

            if self._config.trakt.enabled:
                all_stats.extend(self._sync_trakt())
                self._publish_progress(all_stats, force=True)

            if self._config.mdblist.enabled:
                all_stats.extend(self._sync_mdblist())
                self._publish_progress(all_stats, force=True)

        if self._sync_modes["lists"] and any([
            self._config.sync.simkl_sync_to_pmdb_watchlist,
            self._config.sync.trakt_sync_to_pmdb_watchlist,
            self._config.sync.anilist_sync_to_pmdb_watchlist,
        ]):
            logger.info("── PMDB Watchlist Sync ────────────────────────────────")
            wl_stats = self._sync_pmdb_watchlist()
            if wl_stats:
                all_stats.append(wl_stats)
                self._publish_progress(all_stats, force=True)

        if self._sync_modes["history"] or self._sync_modes["resume"]:
            logger.info("── Watch History / Resume ─────────────────────────────")
            activity_rows: list[SyncStats] = []
            if self._config.simkl.access_token:
                activity_rows.extend(self._sync_simkl_activity())
            if self._config.trakt.enabled:
                activity_rows.extend(self._sync_trakt_activity())
            all_stats.extend(self._merge_activity_stats(activity_rows))
            self._publish_progress(all_stats, force=True)

        if self._sync_modes["lists"] and not self._config.sync.dry_run and self._config.sync.delete_disabled_lists:
            desired_names = {stats.list_name for stats in all_stats if stats.list_name}
            self._delete_disabled_lists(desired_names)

        self._set_status("Finalizing sync results")
        self._log_results(all_stats)
        return all_stats

    def _sync_pmdb_watchlist(self) -> SyncStats | None:
        """Merge selected watchlist items from enabled providers into the PMDB native watchlist."""
        all_items: list[dict] = []

        if self._config.sync.simkl_sync_to_pmdb_watchlist and self._config.simkl.access_token:
            self._set_status("Fetching SIMKL plan-to-watch for PMDB watchlist")
            for simkl_type in self._config.sync.media_types:
                self._check_cancelled()
                grouped = self._simkl.get_status("plantowatch", [simkl_type])
                all_items.extend(grouped.get(simkl_type, []))

        if self._config.sync.trakt_sync_to_pmdb_watchlist and self._config.trakt.enabled:
            self._set_status("Fetching Trakt watchlist for PMDB watchlist")
            watchlist = self._trakt.get_watchlist() or []
            all_items.extend(watchlist)

        if self._config.sync.anilist_sync_to_pmdb_watchlist and self._config.anilist.enabled:
            self._set_status("Fetching AniList planning for PMDB watchlist")
            self._check_cancelled()
            items = self._anilist_root_client.get_status("PLANNING") or []
            all_items.extend(items)

        if not all_items:
            return None

        return self._sync_list(
            all_items,
            "Watchlist",
            "SyncMeta combined watchlist",
            display_name="PMDB Watchlist",
            source_name="Combined",
            is_public=False,
            list_type="watchlist",
        )

    def _sync_simkl(self) -> list[SyncStats]:
        """Sync all configured SIMKL lists."""
        media_types = list(self._config.sync.media_types)

        stats: list[SyncStats] = []
        if not media_types:
            return stats

        all_items_by_status: list[tuple[str, str, list[dict]]] = []
        simkl_fetch_started = time.perf_counter()
        for simkl_type in media_types:
            self._check_cancelled()
            statuses = self._config.simkl.selected_statuses.get(simkl_type, [])
            if not statuses:
                continue
            for status_key in statuses:
                self._check_cancelled()
                self._set_status(f"Fetching SIMKL {_STATUS_LABELS.get(status_key, status_key)} {simkl_type}")
                fetch_started = time.perf_counter()
                grouped = self._simkl.get_status(status_key, [simkl_type])
                items = grouped.get(simkl_type, [])
                fetch_elapsed = time.perf_counter() - fetch_started
                logger.info(
                    "Fetched SIMKL %s %s in %.2fs (%d items)",
                    _STATUS_LABELS.get(status_key, status_key),
                    simkl_type,
                    fetch_elapsed,
                    len(items),
                )
                all_items_by_status.append((simkl_type, status_key, items))
        logger.info(
            "Fetched SIMKL selections in %.2fs (%d buckets)",
            time.perf_counter() - simkl_fetch_started,
            len(all_items_by_status),
        )

        anime_prewarm_ids = sorted({
            int(item["anilist_id"])
            for simkl_type, _, items in all_items_by_status
            if simkl_type == "anime"
            for item in items
            if (
                item.get("anilist_id")
                and str(item.get("media_type", "")).strip().lower() == "tv"
                and not item.get("root_anilist_id")
            )
        })
        if anime_prewarm_ids:
            self._set_status("Pre-warming SIMKL anime metadata cache")
            self._prewarm_simkl_anime_ids(anime_prewarm_ids[:_ANILIST_PREWARM_LIMIT])

        for simkl_type, status_key, items in all_items_by_status:
            self._check_cancelled()
            name = _status_list_name(simkl_type, status_key)
            display_name = _display_status_name(simkl_type, status_key)
            description = (
                f"Auto-synced '{_STATUS_LABELS.get(status_key, status_key)}' "
                f"{_TYPE_LABELS.get(simkl_type, simkl_type)} from SIMKL"
            )

            self._publish_pending_list(name, display_name, "SIMKL")

            stats.append(
                self._sync_list(
                    items,
                    name,
                    description,
                    display_name=display_name,
                    source_name="SIMKL",
                    is_public=self._config.sync.simkl_visibility == "public",
                    selection={
                        "source": "simkl",
                        "media_type": simkl_type,
                        "status": status_key,
                    },
                )
            )

        return stats

    def _sync_trakt_activity(self) -> list[SyncStats]:
        stats: list[SyncStats] = []

        if self._config.sync.trakt_sync_watched_history:
            stats.append(self._sync_trakt_watched_history())

        if self._config.sync.trakt_sync_resume_progress:
            stats.append(self._sync_trakt_resume_progress())

        return stats

    def _sync_simkl_activity(self) -> list[SyncStats]:
        stats: list[SyncStats] = []

        if self._config.sync.simkl_sync_watched_history:
            stats.append(self._sync_simkl_watched_history())

        return stats

    def _sync_simkl_watched_history(self) -> SyncStats:
        stats = SyncStats(
            list_name="",
            display_name="SIMKL Watch History",
            source_name="SIMKL",
        )
        self._set_status("Fetching SIMKL watched history")
        full_sync = self._config.sync.full_history_sync
        cursor = None if full_sync else (self._config.sync.simkl_history_cursor or None)
        items = self._simkl.get_watched_history(since=cursor)

        try:
            existing_items = self._pmdb.get_watched_history()
        except Exception as exc:
            stats.errors.append(f"Failed to load PublicMetaDB watched history: {exc}")
            return stats

        if self._config.sync.simkl_history_anime_only:
            items = [item for item in items if str(item.get("simkl_type", "")).strip().lower() == "anime"]
        items = self._expand_simkl_aggregate_history(items)
        stats.history_cursor = "" if full_sync else self._latest_history_cursor(items, cursor or "")
        stats.items_fetched = len(items)

        # Fetch completed anime list up-front so we can use it as a fallback
        # for shows where SIMKL has no per-episode history records at all.
        self._set_status("Fetching SIMKL completed anime list")
        completed_anime = self._fetch_simkl_completed_anime()
        logger.info("  SIMKL completed anime: %d entries", len(completed_anime))

        existing_counts: dict[str, int] = {}
        for existing_item in existing_items:
            key = self._watched_identity_key(existing_item)
            if key:
                existing_counts[key] = existing_counts.get(key, 0) + 1

        # Track which completed anime already have per-episode records so the
        # season-level fallback pass can skip them (avoids double-counting).
        shows_with_episode_records: set[str] = set()

        # Count how many times each key appears in source (multi-watch support).
        source_seen: dict[str, int] = {}
        pending_items: list[dict] = []
        total = len(items)
        for idx, item in enumerate(items):
            self._check_cancelled()
            if idx % 50 == 0:
                self._set_status(f"Resolving SIMKL history ({idx}/{total})")
            item = self._resolve_activity_item(item)
            item = self._remap_simkl_anime_history_item(item)

            key = self._watched_identity_key(item)
            if not key:
                stats.items_skipped_unresolved += 1
                continue
            stats.items_resolved += 1

            # Record that this show has per-episode data so the season-level
            # fallback pass won't also mark it (which would double-count).
            if str(item.get("simkl_type", "")).strip().lower() == "anime":
                for id_key in ("anilist_id", "root_anilist_id", "mal_id", "root_mal_id"):
                    val = item.get(id_key)
                    if val:
                        shows_with_episode_records.add(f"{id_key}:{val}")

            source_seen[key] = source_seen.get(key, 0) + 1
            # Only add if PMDB has fewer plays than source for this key.
            if existing_counts.get(key, 0) >= source_seen[key]:
                stats.items_skipped_duplicate += 1
                continue
            if self._config.sync.dry_run:
                existing_counts[key] = existing_counts.get(key, 0) + 1
                stats.items_added += 1
                continue
            pending_items.append(item)

        self._write_watched_history_items(
            pending_items,
            existing_counts,
            stats,
            status_message="Writing SIMKL history to PublicMetaDB",
            total_source=total,
        )

        # Season-level fallback: only for completed anime that produced zero
        # per-episode records (e.g. user marked complete without tracking each
        # episode in SIMKL).  Shows that already have episode records are
        # skipped to prevent PMDB double-counting.
        self._sync_completed_anime_seasons(stats, existing_counts, completed_anime, shows_with_episode_records)
        return stats

    def _fetch_simkl_completed_anime(self) -> list[dict]:
        """Return all TV anime entries from the user's SIMKL completed list."""
        try:
            grouped = self._simkl.get_status("completed", ["anime"])
            return [
                item for item in grouped.get("anime", [])
                if item.get("media_type") != "movie"
            ]
        except Exception as exc:
            logger.warning("Could not fetch SIMKL completed anime: %s", exc)
            return []

    def _sync_completed_anime_seasons(
        self,
        stats: SyncStats,
        existing_counts: dict[str, int],
        completed_anime: list[dict],
        shows_with_episode_records: set[str],
    ) -> None:
        """Season-level fallback for completed anime with no per-episode history.

        Only fires for shows where SIMKL returned zero individual episode records.
        Shows that already have episode-level entries are skipped entirely to
        prevent PMDB from double-counting plays.
        """
        fallback_count = 0
        skipped_has_episodes = 0
        pending_items: list[dict] = []
        for item in completed_anime:
            self._check_cancelled()

            # Skip shows that already have per-episode records from the history
            # pass — adding a season mark on top would double-count in PMDB.
            item_has_records = any(
                f"{id_key}:{item.get(id_key)}" in shows_with_episode_records
                for id_key in ("anilist_id", "root_anilist_id", "mal_id", "root_mal_id")
                if item.get(id_key)
            )
            if item_has_records:
                skipped_has_episodes += 1
                continue

            resolved = self._resolve_activity_item(item)

            # Use Fribb + PMDB anime-seasons to find the right TMDB season,
            # same pipeline as episode remapping.
            fribb = self._lookup_fribb_entry(resolved)
            tmdb_id = int(resolved.get("tmdb_id") or 0)
            tmdb_season = 1

            if fribb is not None:
                remapped = self._remap_via_fribb(fribb, tmdb_id, 1)
                if remapped:
                    if remapped.get("tmdb_id"):
                        tmdb_id = int(remapped["tmdb_id"])
                    tmdb_season = remapped.get("season", 1)
            elif tmdb_id > 0:
                offset = int(resolved.get("root_episode_offset") or 0)
                if offset > 0:
                    try:
                        anime_seasons = self._get_cached_anime_seasons(tmdb_id)
                        if anime_seasons:
                            remapped = self._map_episode_via_anime_seasons(anime_seasons, offset, 1)
                            if remapped:
                                tmdb_season = remapped.get("season", 1)
                    except Exception:
                        pass

            if not tmdb_id:
                continue

            season_key = f"{tmdb_id}:tv:{tmdb_season}:"
            if existing_counts.get(season_key, 0) > 0:
                stats.items_skipped_duplicate += 1
                continue

            if self._config.sync.dry_run:
                existing_counts[season_key] = 1
                stats.items_added += 1
                continue
            pending_items.append({
                **item,
                "tmdb_id": tmdb_id,
                "media_type": "tv",
                "season": tmdb_season,
                "episode": None,
                "watched_at": item.get("last_watched_at") or item.get("watched_at"),
            })

        before_added = stats.items_added
        self._write_watched_history_items(
            pending_items,
            existing_counts,
            stats,
            status_message="Marking completed anime seasons in PublicMetaDB",
        )
        fallback_count += max(0, stats.items_added - before_added)

        if skipped_has_episodes:
            logger.info(
                "  Season fallback skipped %d show(s) that already had episode records",
                skipped_has_episodes,
            )
        if fallback_count:
            logger.info(
                "  Season fallback marked %d show(s) with no episode history",
                fallback_count,
            )

    def _expand_simkl_aggregate_history(self, items: list[dict]) -> list[dict]:
        expanded: list[dict] = []
        for item in items:
            aggregate_count = item.get("aggregate_watched_count")
            if aggregate_count:
                resolved = self._resolve_activity_item(item)
                aggregate_rows = self._expand_simkl_aggregate_anime_item(resolved)
                if not aggregate_rows:
                    aggregate_rows = self._simkl.expand_aggregate_history_item(resolved)
                if aggregate_rows:
                    expanded.extend(self._remap_simkl_anime_history_item(row) for row in aggregate_rows)
                else:
                    logger.info(
                        "Skipping aggregate SIMKL anime history for '%s' because it could not be safely mapped to concrete seasons/episodes",
                        resolved.get("title", "Unknown"),
                    )
                continue
            expanded.append(item)
        return self._dedupe_activity_history_items(expanded)

    def _expand_simkl_aggregate_anime_item(self, item: dict) -> list[dict]:
        """Expand aggregate anime history through the anime remap pipeline.

        Seasonal anime is often represented as a franchise root plus an episode
        offset. Expanding those items through raw TMDB season plans tends to
        collapse into Season 1. For smaller anime runs, synthesize Season 1
        episode rows first, then let the normal anime remapper place them into
        the correct root TMDB season/episode.
        """
        if str(item.get("simkl_type", "")).strip().lower() != "anime":
            return []
        if str(item.get("media_type", "")).strip().lower() != "tv":
            return []
        try:
            watched_total = int(item.get("aggregate_watched_count") or 0)
        except (TypeError, ValueError):
            return []
        if watched_total <= 0:
            return []

        has_root_offset = int(item.get("root_episode_offset") or 0) > 0
        has_anime_ids = any(item.get(key) for key in ("anilist_id", "root_anilist_id", "mal_id", "root_mal_id"))
        if not has_root_offset and not has_anime_ids:
            return []
        if watched_total > 200:
            return []

        watched_at = item.get("watched_at")
        return [
            {
                **item,
                "season": 1,
                "episode": episode_number,
                "watched_at": watched_at,
            }
            for episode_number in range(1, watched_total + 1)
        ]

    def _remap_simkl_anime_history_item(self, item: dict) -> dict:
        """Remap a SIMKL anime TV history item to the correct PMDB season+episode.

        Resolution chain (first success wins):
          1. Fribb anime-lists  →  PMDB anime-seasons  (TVDB season → exact TMDB range)
          2. Fribb anime-lists  →  direct TVDB season  (tvdb_season == tmdb_season heuristic)
          3. PMDB anime-seasons with absolute offset    (legacy path, offset-based)
          4. Single-season TMDB heuristic              (only for shows with 1 TMDB season)
        """
        if str(item.get("simkl_type", "")).strip().lower() != "anime":
            return item
        if str(item.get("media_type", "")).strip().lower() != "tv":
            return item

        try:
            episode = int(item.get("episode") or 0)
            tmdb_id = int(item.get("tmdb_id") or 0)
            offset = int(item.get("root_episode_offset") or 0)
        except (TypeError, ValueError):
            return item
        if episode <= 0:
            return item

        cache_key = (
            str(item.get("tmdb_id") or ""),
            str(item.get("anilist_id") or ""),
            str(item.get("root_anilist_id") or ""),
            str(item.get("mal_id") or ""),
            str(item.get("root_mal_id") or ""),
            offset,
            episode,
        )
        cached = self._anime_history_remap_cache.get(cache_key)
        if cached is not None:
            return {**item, **cached}

        def _build_tmdb_season_map(tid: int) -> dict[int, int]:
            """Build {tmdb_season: total_episode_count} from PMDB anime-seasons data.

            PMDB anime-seasons maps TVDB arcs to TMDB season+start. We aggregate
            episode_count per tmdb_season to get how many episodes each TMDB season
            actually has, without scraping TMDB's website.
            """
            try:
                seasons = self._get_cached_anime_seasons(tid)
            except Exception:
                return {}
            totals: dict[int, int] = {}
            for s in seasons:
                try:
                    ts = int(s["tmdb_season"])
                    ec = int(s["episode_count"])
                    tmdb_start = int(s["tmdb_episode_start"])
                    # max episode in this arc within the tmdb_season
                    max_ep = tmdb_start + ec - 1
                    totals[ts] = max(totals.get(ts, 0), max_ep)
                except (TypeError, ValueError, KeyError):
                    continue
            return totals

        # Build season episode-count maps from PMDB data (reliable API, no scraping).
        season_plan_map: dict[int, int] = _build_tmdb_season_map(tmdb_id) if tmdb_id > 0 else {}
        # Also keep TMDB scraper plan as secondary fallback for validation only.
        try:
            _tmdb_plan = self._simkl._get_tmdb_season_plan_cached(tmdb_id) if tmdb_id > 0 else []
            for _sn, _cnt in _tmdb_plan:
                if _sn > 0 and _sn not in season_plan_map:
                    season_plan_map[_sn] = _cnt
        except Exception:
            pass

        def _validate_and_fix(remapped: dict) -> dict | None:
            """Validate remapped season/episode against known episode counts.

            - Redistribute across known seasons when cumulative counts are reliable.
            - Treat 0-episode placeholder seasons as invalid targets unless a
              redistribution lands on a real season.
            - Refuse unsafe remaps for sequel/root-offset items when PMDB season
              topology is incomplete or inconsistent.
            - Only allow season-1 overflow when the mapping evidence says this is
              effectively a single-season target.
            Returns a (possibly corrected) remapped dict, or None if unresolvable.
            """
            target_season = int(remapped.get("season") or 1)
            target_episode = int(remapped.get("episode") or 1)
            target_tmdb_id = int(remapped.get("tmdb_id") or tmdb_id)
            plan = season_plan_map if target_tmdb_id == tmdb_id else _build_tmdb_season_map(target_tmdb_id)
            season_ep_count = plan.get(target_season)
            if season_ep_count is None:
                return remapped  # No data — accept as-is

            absolute = offset + episode
            known_positive_seasons = sorted(sn for sn, cnt in plan.items() if int(cnt or 0) > 0)
            has_placeholder_seasons = any(int(cnt or 0) == 0 for cnt in plan.values())
            has_multi_season_evidence = (
                offset > 0
                or bool(item.get("root_anilist_id") or item.get("root_mal_id"))
                or len(known_positive_seasons) > 1
                or has_placeholder_seasons
            )
            only_season_one_known = known_positive_seasons == [1]

            if season_ep_count == 0 or target_episode > season_ep_count:
                redistributed = self._map_absolute_via_season_plan(plan, absolute)
                if redistributed:
                    out = {**remapped, **redistributed}
                    if target_tmdb_id != tmdb_id:
                        out["tmdb_id"] = target_tmdb_id
                    return out
                if season_ep_count == 0:
                    return None
                if only_season_one_known and not has_multi_season_evidence:
                    return {**remapped, "season": 1, "episode": absolute}
                return None
            return remapped

        # ── Path 1 & 2: Fribb anime-lists ──────────────────────────────────────
        fribb = self._lookup_fribb_entry(item)
        if fribb is not None:
            remapped = self._remap_via_fribb(fribb, tmdb_id, episode)
            if remapped:
                remapped = _validate_and_fix(remapped) or remapped
                self._anime_history_remap_cache[cache_key] = dict(remapped)
                return {**item, **remapped}

        # Remaining paths only make sense when there is a non-zero offset
        # (offset == 0 means the item IS the root season; no remapping needed).
        if offset <= 0 or tmdb_id <= 0:
            return item

        # ── Path 3: PMDB anime-seasons with absolute episode offset ─────────────
        try:
            anime_seasons = self._get_cached_anime_seasons(tmdb_id)
            if anime_seasons:
                remapped = self._map_episode_via_anime_seasons(anime_seasons, offset, episode)
                if remapped:
                    remapped = _validate_and_fix(remapped) or remapped
                    self._anime_history_remap_cache[cache_key] = dict(remapped)
                    return {**item, **remapped}
        except Exception:
            pass

        # ── Path 4: PMDB season map cumulative distribution ────────────────────
        # Handles both single-season and multi-season shows using episode counts
        # from PMDB anime-seasons (reliable API, no scraping required).
        try:
            if season_plan_map:
                absolute = offset + episode
                redistributed = self._map_absolute_via_season_plan(season_plan_map, absolute)
                if redistributed:
                    self._anime_history_remap_cache[cache_key] = dict(redistributed)
                    return {**item, **redistributed}
                known_positive_seasons = sorted(sn for sn, cnt in season_plan_map.items() if int(cnt or 0) > 0)
                has_multi_season_evidence = (
                    offset > 0
                    or bool(item.get("root_anilist_id") or item.get("root_mal_id"))
                    or len(known_positive_seasons) > 1
                )
                if known_positive_seasons == [1] and not has_multi_season_evidence:
                    remapped = {"season": 1, "episode": absolute}
                    self._anime_history_remap_cache[cache_key] = dict(remapped)
                    return {**item, **remapped}
        except Exception:
            pass

        self._anime_history_remap_cache[cache_key] = {}
        return item

    @staticmethod
    def _map_absolute_via_season_plan(season_plan_map: dict[int, int], absolute: int) -> dict | None:
        """Distribute an absolute episode number across TMDB seasons using cumulative counts.

        season_plan_map: {season_number: episode_count, ...}
        Returns {"season": N, "episode": E} or None if the episode is out of range.
        """
        if not season_plan_map or absolute <= 0:
            return None
        cumulative = 0
        for season_number in sorted(season_plan_map):
            ep_count = season_plan_map[season_number]
            if ep_count <= 0:
                continue
            if cumulative < absolute <= cumulative + ep_count:
                return {"season": season_number, "episode": absolute - cumulative}
            cumulative += ep_count
        return None

    def _lookup_fribb_entry(self, item: dict) -> dict | None:
        """Return the Fribb anime-lists entry for this SIMKL item, or None."""
        from . import fribb_client
        ids = item.get("ids") or {}

        for id_key in ("anilist_id", "root_anilist_id"):
            raw = item.get(id_key) or ids.get("anilist") or ids.get("root_anilist")
            if raw:
                try:
                    cache_key = ("anilist", str(int(raw)))
                    if cache_key in self._fribb_lookup_cache:
                        return self._fribb_lookup_cache[cache_key]
                    entry = fribb_client.lookup_by_anilist(int(raw))
                    if entry:
                        self._fribb_lookup_cache[cache_key] = entry
                        return entry
                    self._fribb_lookup_cache[cache_key] = None
                except (TypeError, ValueError):
                    pass

        for id_key in ("mal_id", "root_mal_id"):
            raw = item.get(id_key) or ids.get("mal") or ids.get("root_mal")
            if raw:
                try:
                    cache_key = ("mal", str(int(raw)))
                    if cache_key in self._fribb_lookup_cache:
                        return self._fribb_lookup_cache[cache_key]
                    entry = fribb_client.lookup_by_mal(int(raw))
                    if entry:
                        self._fribb_lookup_cache[cache_key] = entry
                        return entry
                    self._fribb_lookup_cache[cache_key] = None
                except (TypeError, ValueError):
                    pass

        return None

    def _remap_via_fribb(self, fribb: dict, item_tmdb_id: int, episode: int) -> dict | None:
        """Use Fribb's TVDB season info + PMDB anime-seasons for accurate remapping.

        Fribb gives us which TVDB season this AniList/MAL entry belongs to and the
        episode offset within that season.  PMDB anime-seasons maps TVDB season
        (stored as season_number) to a TMDB season + episode range.  When PMDB
        data is unavailable we fall back to assuming tvdb_season == tmdb_season,
        which is correct for the vast majority of modern seasonal anime.
        """
        tvdb_season_raw = fribb.get("thetvdb_season")
        if tvdb_season_raw is None:
            return None
        try:
            tvdb_season = int(tvdb_season_raw)
            tvdb_epoffset = int(fribb.get("thetvdb_epoffset") or 0)
        except (TypeError, ValueError):
            return None

        if tvdb_season <= 0:
            return None  # Season 0 = specials; leave untouched

        # Episode number within the TVDB season
        tvdb_episode = tvdb_epoffset + episode

        # Prefer the Fribb-provided TMDB ID when the item doesn't have one
        # (Fribb's themoviedb is the TMDB ID for the franchise root).
        tmdb_id = item_tmdb_id
        fribb_tmdb = fribb.get("themoviedb")
        if not tmdb_id and fribb_tmdb:
            try:
                tmdb_id = int(fribb_tmdb)
            except (TypeError, ValueError):
                pass

        # Try PMDB anime-seasons: season_number in PMDB == TVDB season number.
        if tmdb_id > 0:
            try:
                anime_seasons = self._get_cached_anime_seasons(tmdb_id)
                if anime_seasons:
                    remapped = self._map_episode_via_tvdb_season(anime_seasons, tvdb_season, tvdb_episode)
                    if remapped:
                        out = {"season": remapped["season"], "episode": remapped["episode"]}
                        if tmdb_id != item_tmdb_id:
                            out["tmdb_id"] = tmdb_id
                        return out
            except Exception:
                pass

        # Fallback: tvdb_season == tmdb_season holds for most shows where each
        # cour / arc gets its own TMDB season (AoT, DanDaDan, etc.).
        out: dict = {"season": tvdb_season, "episode": tvdb_episode}
        if tmdb_id > 0 and tmdb_id != item_tmdb_id:
            out["tmdb_id"] = tmdb_id
        return out

    def _get_cached_anime_seasons(self, tmdb_id: int) -> list[dict]:
        tmdb_id = int(tmdb_id)
        if tmdb_id <= 0:
            return []
        if tmdb_id not in self._anime_seasons_cache:
            self._anime_seasons_cache[tmdb_id] = list(self._pmdb.get_anime_seasons(tmdb_id))
        return list(self._anime_seasons_cache[tmdb_id])

    @staticmethod
    def _map_episode_via_tvdb_season(
        seasons: list[dict], tvdb_season: int, tvdb_episode: int
    ) -> dict | None:
        """Map a TVDB season+episode to a TMDB season+episode using PMDB anime-seasons.

        PMDB stores the community mapping as: season_number (= TVDB season) →
        tmdb_season + tmdb_episode_start.  We find the matching entry and compute
        the TMDB episode from the start offset.
        """
        for s in seasons:
            try:
                if int(s["season_number"]) != tvdb_season:
                    continue
                tmdb_season = int(s["tmdb_season"])
                tmdb_start = int(s["tmdb_episode_start"])
            except (TypeError, ValueError, KeyError):
                continue
            return {"season": tmdb_season, "episode": tmdb_start + (tvdb_episode - 1)}
        return None

    @staticmethod
    def _map_episode_via_anime_seasons(seasons: list[dict], offset: int, episode: int) -> dict | None:
        """Legacy path: map absolute episode (offset + episode) via PMDB anime-seasons.

        Used when Fribb data is unavailable.  Accumulates episode_count across
        sorted season entries to locate which arc the absolute episode falls in.
        """
        absolute = offset + episode

        valid = [
            s for s in seasons
            if s.get("season_number") is not None
            and s.get("episode_count")
            and s.get("tmdb_season") is not None
            and s.get("tmdb_episode_start") is not None
        ]
        if not valid:
            return None

        valid.sort(key=lambda s: int(s.get("season_number") or 0))

        cumulative = 0
        for s in valid:
            try:
                ep_count = int(s["episode_count"])
                tmdb_season = int(s["tmdb_season"])
                tmdb_start = int(s["tmdb_episode_start"])
            except (TypeError, ValueError):
                return None
            if ep_count <= 0:
                return None

            season_abs_start = cumulative + 1
            season_abs_end = cumulative + ep_count

            if season_abs_start <= absolute <= season_abs_end:
                return {
                    "season": tmdb_season,
                    "episode": tmdb_start + (absolute - season_abs_start),
                }

            cumulative += ep_count

        return None

    def _sync_trakt_watched_history(self) -> SyncStats:
        stats = SyncStats(
            list_name="",
            display_name="Trakt Watch History",
            source_name="Trakt",
        )
        self._set_status("Fetching Trakt watched history")
        full_sync = self._config.sync.full_history_sync
        cursor = None if full_sync else (self._config.sync.trakt_history_cursor or None)
        items = self._trakt.get_watched_history(since=cursor)

        try:
            existing_items = self._pmdb.get_watched_history()
        except Exception as exc:
            stats.errors.append(f"Failed to load PublicMetaDB watched history: {exc}")
            return stats

        stats.items_fetched = len(items)
        stats.history_cursor = "" if full_sync else self._latest_history_cursor(items, cursor or "")

        existing_counts: dict[str, int] = {}
        for existing_item in existing_items:
            key = self._watched_identity_key(existing_item)
            if key:
                existing_counts[key] = existing_counts.get(key, 0) + 1

        source_seen: dict[str, int] = {}
        pending_items: list[dict] = []
        total = len(items)
        for idx, item in enumerate(items):
            self._check_cancelled()
            if idx % 50 == 0:
                self._set_status(f"Resolving Trakt history ({idx}/{total})")
            item = self._resolve_activity_item(item)
            key = self._watched_identity_key(item)
            if not key:
                stats.items_skipped_unresolved += 1
                continue
            stats.items_resolved += 1
            source_seen[key] = source_seen.get(key, 0) + 1
            if existing_counts.get(key, 0) >= source_seen[key]:
                stats.items_skipped_duplicate += 1
                continue
            if self._config.sync.dry_run:
                existing_counts[key] = existing_counts.get(key, 0) + 1
                stats.items_added += 1
                continue
            pending_items.append(item)
        self._write_watched_history_items(
            pending_items,
            existing_counts,
            stats,
            status_message="Writing Trakt history to PublicMetaDB",
            total_source=total,
        )
        return stats

    def _sync_trakt_resume_progress(self) -> SyncStats:
        stats = SyncStats(
            list_name="",
            display_name="Trakt Resume Progress",
            source_name="Trakt",
        )
        self._set_status("Fetching Trakt playback progress")
        items = self._trakt.get_playback_progress()
        stats.items_fetched = len(items)

        normalized_items: list[dict] = []
        for item in items:
            self._check_cancelled()
            item = self._resolve_activity_item(item)
            key = self._resume_key(item)
            if not key:
                stats.items_skipped_unresolved += 1
                continue
            normalized_items.append(item)
            stats.items_resolved += 1

        if self._config.sync.dry_run:
            stats.items_added = len(normalized_items)
            return stats

        if not normalized_items:
            return stats

        try:
            existing_resume_points = self._pmdb.get_resume_points()
        except Exception as exc:
            stats.errors.append(f"Failed to load PublicMetaDB resume points: {exc}")
            return stats

        existing_resume_by_key: dict[str, dict] = {}
        for item in existing_resume_points:
            key = self._resume_key(item)
            if key:
                existing_resume_by_key[key] = item

        payloads: list[dict] = []
        for item in normalized_items:
            self._check_cancelled()
            key = self._resume_key(item)
            payload = {
                "tmdb_id": int(item["tmdb_id"]),
                "media_type": item["media_type"],
                "position_ms": int(item["position_ms"]),
                "runtime_ms": int(item["runtime_ms"]),
            }
            if item.get("season") is not None:
                payload["season"] = int(item["season"])
            if item.get("episode") is not None:
                payload["episode"] = int(item["episode"])
            existing = existing_resume_by_key.get(key)
            if existing and self._resume_matches(existing, payload):
                stats.items_skipped_duplicate += 1
                continue
            payloads.append(payload)

        if not payloads:
            return stats

        for chunk in self._chunked(payloads, 50):
            self._check_cancelled()
            try:
                self._set_status("Writing Trakt resume progress to PublicMetaDB")
                response = self._pmdb.save_resume_points_batch(chunk) or {}
                for result in response.get("results", []):
                    action = str(result.get("action", "")).strip().lower()
                    if action == "saved":
                        stats.items_added += 1
                    elif action == "completed":
                        stats.items_removed += 1
                stats.items_skipped_duplicate += max(0, len(chunk) - sum(
                    1 for result in response.get("results", [])
                    if str(result.get("action", "")).strip().lower() in {"saved", "completed"}
                ))
            except SyncCancelled:
                raise
            except Exception as exc:
                stats.errors.append(f"Failed to sync resume batch: {exc}")
                continue
        return stats

    def _sync_anilist(self) -> list[SyncStats]:
        """Sync configured AniList anime statuses."""
        if "anime" not in self._config.sync.media_types:
            return []

        from .anilist_client import AniListClient

        client = AniListClient(
            self._config.anilist,
            cancel_requested_callback=self._cancel_requested_callback,
        )
        stats: list[SyncStats] = []

        # Collect all items from every status first, then pre-warm the shared
        # AniList prequel-chain cache concurrently before the list sync begins.
        # This ensures SIMKL anime (which runs concurrently) gets cache hits for
        # any AniList IDs it shares, instead of each thread walking the same chains.
        all_items_by_status: list[tuple[str, list[dict]]] = []
        fetch_started = time.perf_counter()
        fetched_by_status = client.get_statuses(list(self._config.anilist.selected_statuses))
        logger.info(
            "Fetched AniList selections in %.2fs (%d statuses)",
            time.perf_counter() - fetch_started,
            len(self._config.anilist.selected_statuses),
        )
        for status_key in self._config.anilist.selected_statuses:
            self._check_cancelled()
            self._set_status(f"Fetching AniList {_STATUS_LABELS.get(status_key, status_key)} anime")
            items = fetched_by_status.get(status_key, [])
            logger.info(
                "Loaded AniList %s (%d items)",
                _STATUS_LABELS.get(status_key, status_key),
                len(items),
            )
            all_items_by_status.append((status_key, items))

        # Pre-warm the shared prequel-chain cache for all unique AniList IDs.
        # Uses up to 4 threads; the shared lock in _get_root_context prevents
        # duplicate chain walks for the same ID.
        all_anilist_ids = list({
            int(item["anilist_id"])
            for _, items in all_items_by_status
            for item in items
            if item.get("anilist_id")
        })
        simkl_anime_enabled = bool(
            self._config.simkl.selected_statuses.get("anime")
            and "anime" in self._config.sync.media_types
        )
        if all_anilist_ids and simkl_anime_enabled:
            prewarm_ids = all_anilist_ids[:_ANILIST_PREWARM_LIMIT]
            self._set_status("Pre-warming anime metadata cache")
            get_ctx = getattr(client, "_get_root_context", None)
            if callable(get_ctx):
                pool = ThreadPoolExecutor(max_workers=4)
                shutdown_wait = True
                try:
                    futures = {pool.submit(get_ctx, aid): aid for aid in prewarm_ids}
                    for future in self._iter_completed_futures(futures):
                        try:
                            future.result()
                        except Exception as exc:
                            logger.debug("Cache warm failed for anilist_id=%s: %s", futures[future], exc)
                except SyncCancelled:
                    shutdown_wait = False
                    raise
                finally:
                    pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)
            logger.info(
                "Pre-warmed anime chain cache for %d/%d AniList IDs",
                len(prewarm_ids),
                len(all_anilist_ids),
            )
        elif all_anilist_ids:
            logger.info(
                "Skipped AniList pre-warm for %d IDs because SIMKL anime sync is not enabled",
                len(all_anilist_ids),
            )

        for status_key, items in all_items_by_status:
            self._check_cancelled()
            name = _status_list_name("anime", status_key)
            display_name = _display_status_name("anime", status_key)
            self._publish_pending_list(name, display_name, "AniList")
            description = f"Auto-synced '{_STATUS_LABELS.get(status_key, status_key)}' anime from AniList"
            stats.append(
                self._sync_list(
                    items,
                    name,
                    description,
                    display_name=display_name,
                    source_name="AniList",
                    is_public=self._config.sync.anilist_visibility == "public",
                    selection={
                        "source": "anilist",
                        "status": status_key,
                    },
                )
            )

        return stats

    def _sync_trakt(self) -> list[SyncStats]:
        """Sync configured Trakt watchlist and list sources."""
        stats: list[SyncStats] = []

        should_sync_watchlist_movies = self._config.trakt.sync_watchlist_movies and "movies" in self._config.sync.media_types
        should_sync_watchlist_shows = self._config.trakt.sync_watchlist_shows and "shows" in self._config.sync.media_types
        if should_sync_watchlist_movies or should_sync_watchlist_shows:
            self._check_cancelled()
            if should_sync_watchlist_movies:
                self._publish_pending_list(_status_list_name("movies", "watchlist"), _display_status_name("movies", "watchlist"), "Trakt")
            if should_sync_watchlist_shows:
                self._publish_pending_list(_status_list_name("shows", "watchlist"), _display_status_name("shows", "watchlist"), "Trakt")
            self._set_status("Fetching Trakt watchlist")
            watchlist_items = self._trakt.get_watchlist()
            grouped = {
                "shows": [item for item in watchlist_items if item["media_type"] == "tv"],
                "movies": [item for item in watchlist_items if item["media_type"] == "movie"],
            }
            if should_sync_watchlist_movies:
                items = grouped.get("movies", [])
                stats.append(
                    self._sync_list(
                        items,
                        _status_list_name("movies", "watchlist"),
                        "Auto-synced Trakt movie watchlist",
                        display_name=_display_status_name("movies", "watchlist"),
                        source_name="Trakt",
                        is_public=self._config.sync.trakt_personal_visibility == "public",
                        selection={
                            "source": "trakt",
                            "kind": "watchlist",
                            "media_type": "movies",
                        },
                    )
                )
            if should_sync_watchlist_shows:
                items = grouped.get("shows", [])
                stats.append(
                    self._sync_list(
                        items,
                        _status_list_name("shows", "watchlist"),
                        "Auto-synced Trakt show watchlist",
                        display_name=_display_status_name("shows", "watchlist"),
                        source_name="Trakt",
                        is_public=self._config.sync.trakt_personal_visibility == "public",
                        selection={
                            "source": "trakt",
                            "kind": "watchlist",
                            "media_type": "shows",
                        },
                    )
                )

        selected_lists = self._dedupe_trakt_lists(self._config.trakt.selected_lists)
        selected_default_lists = [item for item in selected_lists if item.get("source") == "default"]
        selected_liked_lists = [item for item in selected_lists if item.get("source") == "liked"]
        selected_personal_lists = [item for item in selected_lists if item.get("source") == "personal"]
        selected_public_lists = [item for item in selected_lists if item.get("source") == "discover"]

        fetched_selected_trakt = self._fetch_selected_trakt_lists(
            selected_default_lists,
            selected_liked_lists,
            selected_personal_lists,
            selected_public_lists,
        )

        if self._config.trakt.sync_liked_lists:
            self._check_cancelled()
            self._set_status("Fetching Trakt liked lists")
            for liked_list in self._trakt.get_liked_lists():
                self._check_cancelled()
                self._publish_pending_list(liked_list["name"], liked_list["name"], f"Trakt by {liked_list['user']}")
                items = self._filter_trakt_items(liked_list["items"])
                name = liked_list["name"]
                description = f"Auto-synced liked Trakt list '{liked_list['name']}'"
                stats.append(
                    self._sync_list(
                        items,
                        name,
                        description,
                        display_name=liked_list["name"],
                        source_name=f"Trakt by {liked_list['user']}",
                        is_public=self._config.sync.trakt_public_visibility == "public",
                        selection={
                            "source": "trakt",
                            "kind": "liked-auto",
                            "list_source": "liked",
                            "user": liked_list.get("user", ""),
                            "slug": liked_list.get("slug", ""),
                            "name": liked_list.get("name", ""),
                        },
                    )
                )

        for trakt_list, items in fetched_selected_trakt:
            self._check_cancelled()
            source_kind = str(trakt_list.get("source", "")).strip()
            is_default = source_kind == "default"
            source_name = "Trakt" if is_default else f"Trakt by {trakt_list['user']}"
            description = (
                f"Auto-synced Trakt default catalog '{trakt_list['name']}'"
                if is_default
                else (
                    f"Auto-synced your Trakt list '{trakt_list['name']}'"
                    if source_kind == "personal"
                    else f"Auto-synced Trakt list '{trakt_list['name']}' by {trakt_list['user']}"
                )
            )
            is_public = (
                self._config.sync.trakt_personal_visibility == "public"
                if source_kind in {"default", "personal"}
                else self._config.sync.trakt_public_visibility == "public"
            )
            selection = {
                "source": "trakt",
                "kind": "default" if is_default else "selected-list",
                "list_source": source_kind,
                "user": trakt_list.get("user", ""),
                "slug": trakt_list.get("slug", ""),
                "name": trakt_list.get("name", ""),
            }
            if is_default:
                selection["catalog_key"] = trakt_list.get("catalog_key", "")
            stats.append(
                self._sync_list(
                    items,
                    trakt_list["name"],
                    description,
                    display_name=trakt_list["name"],
                    source_name=source_name,
                    is_public=is_public,
                    selection=selection,
                )
            )

        return stats

    def _sync_mdblist(self) -> list[SyncStats]:
        """Sync selected MDBList lists."""
        stats: list[SyncStats] = []

        for mdblist, items in self._fetch_selected_mdblist_lists(self._dedupe_mdblist_lists(self._config.mdblist.selected_lists)):
            self._check_cancelled()
            name = mdblist["name"]
            description = f"Auto-synced MDBList '{mdblist['name']}'"
            stats.append(
                self._sync_list(
                    items,
                    name,
                    description,
                    display_name=mdblist["name"],
                    source_name="MDBList",
                    is_public=self._config.sync.mdblist_visibility == "public",
                    selection={
                        "source": "mdblist",
                        "id": mdblist.get("id"),
                        "mediatype": mdblist.get("mediatype", ""),
                        "name": mdblist.get("name", ""),
                    },
                )
            )

        return stats

    def _filter_trakt_items(self, items: list[dict]) -> list[dict]:
        filtered = []
        for item in items:
            if item["media_type"] == "movie" and "movies" not in self._config.sync.media_types:
                continue
            if item["media_type"] == "tv" and "shows" not in self._config.sync.media_types:
                continue
            filtered.append(item)
        return filtered

    def _fetch_selected_trakt_lists(
        self,
        selected_default_lists: list[dict],
        selected_liked_lists: list[dict],
        selected_personal_lists: list[dict],
        selected_public_lists: list[dict],
    ) -> list[tuple[dict, list[dict]]]:
        def trakt_job_key(trakt_list: dict) -> tuple[str, str, str, str, str]:
            return (
                str(trakt_list.get("source", "")),
                str(trakt_list.get("user", "")),
                str(trakt_list.get("slug", "")),
                str(trakt_list.get("catalog_key", "")),
                str(trakt_list.get("name", "")),
            )

        jobs: list[tuple[dict, str]] = []
        for trakt_list in selected_default_lists:
            self._publish_pending_list(trakt_list["name"], trakt_list["name"], "Trakt")
            jobs.append((trakt_list, "default"))
        for trakt_list in selected_liked_lists + selected_personal_lists + selected_public_lists:
            self._publish_pending_list(trakt_list["name"], trakt_list["name"], f"Trakt by {trakt_list['user']}")
            jobs.append((trakt_list, "selected"))
        if not jobs:
            return []

        results_by_key: dict[tuple[str, str, str, str, str], list[dict]] = {}
        pool = ThreadPoolExecutor(max_workers=min(4, len(jobs)))
        shutdown_wait = True
        try:
            futures = {}
            for trakt_list, mode in jobs:
                key = trakt_job_key(trakt_list)
                if mode == "default":
                    self._set_status(f"Fetching Trakt catalog {trakt_list['name']}")
                    futures[pool.submit(
                        self._trakt.get_default_catalog,
                        trakt_list.get("catalog_key") or trakt_list.get("slug", ""),
                    )] = (key, trakt_list)
                else:
                    self._set_status(f"Fetching Trakt list {trakt_list['name']}")
                    futures[pool.submit(
                        self._trakt.get_list_items,
                        trakt_list["user"],
                        trakt_list["slug"],
                    )] = (key, trakt_list)
            for future in self._iter_completed_futures(futures):
                key, trakt_list = futures[future]
                try:
                    results_by_key[key] = self._filter_trakt_items(future.result() or [])
                except Exception as exc:
                    logger.error("Failed to fetch Trakt source '%s': %s", trakt_list.get("name", ""), exc)
                    results_by_key[key] = []
        except SyncCancelled:
            shutdown_wait = False
            raise
        finally:
            pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)

        ordered_results: list[tuple[dict, list[dict]]] = []
        for trakt_list, _ in jobs:
            key = trakt_job_key(trakt_list)
            ordered_results.append((trakt_list, results_by_key.get(key, [])))
        return ordered_results

    def _filter_mdblist_items(self, items: list[dict]) -> list[dict]:
        filtered = []
        for item in items:
            if item["media_type"] == "movie" and "movies" not in self._config.sync.media_types:
                continue
            if item["media_type"] == "tv" and "shows" not in self._config.sync.media_types:
                continue
            filtered.append(item)
        return filtered

    def _fetch_selected_mdblist_lists(self, selected_lists: list[dict]) -> list[tuple[dict, list[dict]]]:
        if not selected_lists:
            return []
        for mdblist in selected_lists:
            self._publish_pending_list(mdblist["name"], mdblist["name"], "MDBList")

        results_by_key: dict[tuple[int, str], list[dict]] = {}
        pool = ThreadPoolExecutor(max_workers=min(4, len(selected_lists)))
        shutdown_wait = True
        try:
            futures = {
                pool.submit(self._mdblist.get_list_items, mdblist["id"]): mdblist
                for mdblist in selected_lists
            }
            for future in self._iter_completed_futures(futures):
                mdblist = futures[future]
                key = (int(mdblist["id"]), str(mdblist.get("mediatype", "")))
                try:
                    results_by_key[key] = self._filter_mdblist_items(future.result() or [])
                except Exception as exc:
                    logger.error("Failed to fetch MDBList '%s': %s", mdblist.get("name", ""), exc)
                    results_by_key[key] = []
        except SyncCancelled:
            shutdown_wait = False
            raise
        finally:
            pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)

        return [
            (mdblist, results_by_key.get((int(mdblist["id"]), str(mdblist.get("mediatype", ""))), []))
            for mdblist in selected_lists
        ]

    @staticmethod
    def _dedupe_trakt_lists(items: list[dict]) -> list[dict]:
        unique: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in items or []:
            user = str(item.get("user", "")).strip()
            slug = str(item.get("slug", "")).strip()
            name = str(item.get("name", "")).strip()
            if not user or not slug or not name:
                continue
            key = (user.lower(), slug.lower())
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    @staticmethod
    def _dedupe_mdblist_lists(items: list[dict]) -> list[dict]:
        unique: list[dict] = []
        seen: set[tuple[int, str]] = set()
        for item in items or []:
            try:
                list_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            mediatype = str(item.get("mediatype", "")).strip().lower()
            name = str(item.get("name", "")).strip()
            if mediatype not in {"movie", "show"} or not name:
                continue
            key = (list_id, mediatype)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique

    def _sync_list(
        self,
        source_items: list[dict],
        list_name: str,
        list_description: str,
        display_name: str | None = None,
        source_name: str | None = None,
        is_public: bool = False,
        selection: dict | None = None,
        list_type: str = "custom",
    ) -> SyncStats:
        """Sync a single source list to a PublicMetaDB list."""
        actual_list_name = self._resolve_managed_list_name(list_name, source_name or "", selection)
        stats = SyncStats(
            list_name=actual_list_name,
            display_name=display_name or list_name,
            source_name=source_name or "",
            items_fetched=len(source_items),
        )
        self._remember_progress_row(stats)
        self._set_status(f"Processing {actual_list_name}")
        logger.info("  ┌ '%s'  (%d source items)", actual_list_name, len(source_items))

        if not source_items:
            logger.debug("  │ No items, skipping '%s'", actual_list_name)
            self._publish_progress([stats], force=True)
            return stats

        self._set_status(f"Resolving IDs for {actual_list_name}")
        resolve_started = time.perf_counter()
        stats_snapshot = getattr(self._matcher, "stats_snapshot", None)
        matcher_stats_before = (
            stats_snapshot() if callable(stats_snapshot) else {"lookups": 0, "cache_hits": 0, "failed_cache_hits": 0}
        )
        pmdb_stats_snapshot = getattr(self._pmdb, "stats_snapshot", None)
        pmdb_stats_before = pmdb_stats_snapshot() if callable(pmdb_stats_snapshot) else {}
        resolved: list[dict] = []
        pending_mapping_contributions: list[tuple[int, str, str, str]] = []
        resolve_pool = ThreadPoolExecutor(max_workers=min(_LIST_RESOLVE_WORKERS, len(source_items)))
        resolve_futures = {
            resolve_pool.submit(self._matcher.resolve_match, item): item
            for item in source_items
        }
        try:
            for future in self._iter_completed_futures(resolve_futures):
                self._check_cancelled()
                item = resolve_futures[future]
                try:
                    match_result = future.result()
                except SyncCancelled:
                    raise
                except Exception:
                    stats.items_skipped_unresolved += 1
                    continue
                tmdb_id = match_result.tmdb_id
                if tmdb_id is not None:
                    resolved.append({**item, "resolved_tmdb_id": tmdb_id})
                    stats.items_resolved += 1
                    stats.match_breakdown[match_result.resolution_kind] = (
                        int(stats.match_breakdown.get(match_result.resolution_kind, 0)) + 1
                    )
                    if (
                        not self._config.sync.dry_run
                        and (item.get("simkl_type") == "anime" or not item.get("tmdb_id"))
                        and match_result.resolution_kind != "external_mapping"
                    ):
                        pending_mapping_contributions.extend(self._collect_id_mapping_contributions(item, tmdb_id))
                else:
                    stats.items_skipped_unresolved += 1
                    unresolved_reason = match_result.unresolved_reason or "not_found"
                    stats.unresolved_reason_counts[unresolved_reason] = (
                        int(stats.unresolved_reason_counts.get(unresolved_reason, 0)) + 1
                    )
                    stats.unresolved_items.append(
                        _unresolved_item_summary(item, list_name=stats.list_name, unresolved_reason=unresolved_reason)
                    )
                self._publish_progress([stats])
        except SyncCancelled:
            resolve_pool.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            resolve_pool.shutdown(wait=False)
        resolve_elapsed = time.perf_counter() - resolve_started
        matcher_stats_after = (
            stats_snapshot() if callable(stats_snapshot) else {"lookups": 0, "cache_hits": 0, "failed_cache_hits": 0}
        )
        cache_hits = max(0, matcher_stats_after["cache_hits"] - matcher_stats_before["cache_hits"])
        failed_cache_hits = max(
            0,
            matcher_stats_after["failed_cache_hits"] - matcher_stats_before["failed_cache_hits"],
        )
        lookup_count = max(0, matcher_stats_after["lookups"] - matcher_stats_before["lookups"])
        unresolved_rate = (
            (stats.items_skipped_unresolved / stats.items_fetched) if stats.items_fetched else 0.0
        )
        cache_hit_rate = ((cache_hits + failed_cache_hits) / lookup_count) if lookup_count else 0.0
        stats.phase_timings["resolve_seconds"] = round(resolve_elapsed, 4)

        if self._config.sync.dry_run:
            return self._dry_run_report(resolved, actual_list_name, stats)

        try:
            self._set_status(f"Preparing PublicMetaDB list {actual_list_name}")
            pmdb_list = self._get_or_create_pmdb_list_cached(actual_list_name, list_description, is_public=is_public, list_type=list_type)
            self._register_managed_list(
                actual_list_name,
                str(pmdb_list.get("id", "")),
                display_name or list_name,
                source_name or "",
                selection,
            )
        except SyncCancelled:
            raise
        except Exception as exc:
            stats.errors.append(f"Failed to get/create list: {exc}")
            logger.error("Failed to get/create list '%s': %s", actual_list_name, exc)
            self._publish_progress([stats], force=True)
            return stats

        list_id = pmdb_list["id"]

        self._set_status(f"Loading existing items from {actual_list_name}")
        pmdb_read_started = time.perf_counter()
        existing_items = self._get_cached_list_items(list_id)
        existing_map = self._build_existing_map(existing_items)
        pmdb_read_elapsed = time.perf_counter() - pmdb_read_started
        stats.phase_timings["pmdb_read_seconds"] = round(pmdb_read_elapsed, 4)
        desired_keys: set[str] = set()
        pending_adds: list[tuple[dict, int, str, str]] = []
        for item in resolved:
            self._check_cancelled()
            tmdb_id = item["resolved_tmdb_id"]
            media_type = item["media_type"]
            key = f"{tmdb_id}:{media_type}"
            if key in desired_keys:
                stats.items_skipped_duplicate += 1
                self._publish_progress([stats])
                continue
            desired_keys.add(key)

            if key in existing_map:
                stats.items_skipped_duplicate += 1
                self._publish_progress([stats])
                continue
            pending_adds.append((item, tmdb_id, media_type, key))

        # Inject manually-resolved items so remove_missing never evicts them.
        for manual_entry in self._manual_list_additions.get(actual_list_name, []):
            m_tmdb_id = manual_entry.get("tmdb_id")
            m_media_type = manual_entry.get("media_type") or "movie"
            if not m_tmdb_id:
                continue
            m_key = f"{m_tmdb_id}:{m_media_type}"
            desired_keys.add(m_key)
            if m_key not in existing_map:
                synthetic = {"title": f"manual:{m_tmdb_id}", "media_type": m_media_type}
                pending_adds.append((synthetic, m_tmdb_id, m_media_type, m_key))

        pmdb_write_started = time.perf_counter()
        if pending_adds:
            self._set_status(f"Adding items to {actual_list_name}")
            pool = ThreadPoolExecutor(max_workers=min(_LIST_WRITE_WORKERS, len(pending_adds)))
            shutdown_wait = True
            try:
                futures = {
                    pool.submit(self._pmdb.add_item_to_list, list_id, tmdb_id, media_type): (item, tmdb_id, media_type, key)
                    for item, tmdb_id, media_type, key in pending_adds
                }
                for future in self._iter_completed_futures(futures):
                    item, tmdb_id, media_type, key = futures[future]
                    try:
                        result = future.result()
                        if result is not None:
                            cached_item = {
                                "tmdb_id": tmdb_id,
                                "media_type": media_type,
                            }
                            if isinstance(result, dict):
                                cached_item.update(result)
                            existing_map[key] = cached_item
                            self._record_cached_list_item_add(list_id, cached_item)
                            stats.items_added += 1
                        else:
                            stats.errors.append(
                                f"Failed to add '{item['title']}' (tmdb={tmdb_id}): API returned no result"
                            )
                    except SyncCancelled:
                        raise
                    except Exception as exc:
                        stats.errors.append(f"Failed to add '{item['title']}' (tmdb={tmdb_id}): {exc}")
                    self._publish_progress([stats], force=True)
            except SyncCancelled:
                shutdown_wait = False
                raise
            finally:
                pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)

        if self._config.sync.remove_missing:
            self._set_status(f"Removing stale items from {actual_list_name}")
            stats.items_removed = self._remove_stale(list_id, existing_items, desired_keys)
            self._publish_progress([stats], force=True)

        if pending_mapping_contributions:
            self._set_status(f"Contributing PMDB mappings for {actual_list_name}")
            self._flush_id_mapping_contributions(pending_mapping_contributions)

        pmdb_write_elapsed = time.perf_counter() - pmdb_write_started
        stats.phase_timings["pmdb_write_seconds"] = round(pmdb_write_elapsed, 4)
        pmdb_stats_after = pmdb_stats_snapshot() if callable(pmdb_stats_snapshot) else {}
        if pmdb_stats_before or pmdb_stats_after:
            stats.pmdb_metrics = self._delta_counter_snapshot(pmdb_stats_after, pmdb_stats_before)

        logger.info(
            "  └ '%s'  resolved=%d  added=%d  dup=%d  unresolved=%d  removed=%d%s  [resolve=%.2fs pmdb_read=%.2fs pmdb_write=%.2fs cache_hit=%.0f%% unresolved=%.0f%%]",
            actual_list_name,
            stats.items_resolved,
            stats.items_added,
            stats.items_skipped_duplicate,
            stats.items_skipped_unresolved,
            stats.items_removed,
            f"  ⚠ {len(stats.errors)} error(s)" if stats.errors else "",
            resolve_elapsed,
            pmdb_read_elapsed,
            pmdb_write_elapsed,
            cache_hit_rate * 100.0,
            unresolved_rate * 100.0,
        )
        return stats

    def _prewarm_simkl_anime_cache(self) -> None:
        """Pre-warm the shared AniList prequel-chain cache for SIMKL anime items.

        Fetches all SIMKL anime IDs from the configured statuses, filters to
        those not already cached, then concurrently walks their prequel chains.
        This ensures the shared cache is hot before the per-status list sync
        loop runs, so each item resolves instantly via cache hits.
        """
        from .anilist_client import _SHARED_ROOT_CONTEXT_CACHE

        anime_statuses = self._config.simkl.selected_statuses.get("anime", [])
        if not anime_statuses:
            return

        # Collect all unique AniList IDs from every SIMKL anime status.
        anilist_ids: set[int] = set()
        try:
            for status_key in anime_statuses:
                grouped = self._simkl.get_status(status_key, ["anime"])
                for item in grouped.get("anime", []):
                    aid = item.get("anilist_id")
                    if aid:
                        try:
                            anilist_ids.add(int(aid))
                        except (TypeError, ValueError):
                            pass
        except Exception as exc:
            logger.debug("SIMKL anime pre-warm fetch failed: %s", exc)
            return

        self._prewarm_simkl_anime_ids([aid for aid in anilist_ids if aid not in _SHARED_ROOT_CONTEXT_CACHE])

    def _prewarm_simkl_anime_ids(self, uncached: list[int]) -> None:
        if not uncached:
            logger.info("SIMKL anime chain cache: all requested IDs already cached")
            return

        logger.info(
            "Pre-warming SIMKL anime chain cache: %d candidate IDs",
            len(uncached),
        )
        get_ctx = getattr(self._anilist_root_client, "_get_root_context", None)
        if not callable(get_ctx):
            return

        pool = ThreadPoolExecutor(max_workers=4)
        shutdown_wait = True
        try:
            futures = {pool.submit(get_ctx, aid): aid for aid in uncached}
            for future in self._iter_completed_futures(futures):
                try:
                    future.result()
                except Exception as exc:
                    logger.debug("Chain pre-warm failed for anilist_id=%s: %s", futures[future], exc)
        except SyncCancelled:
            shutdown_wait = False
            raise
        finally:
            pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)

    def _collect_id_mapping_contributions(self, item: dict, tmdb_id: int) -> list[tuple[int, str, str, str]]:
        """Return unique PMDB mapping contributions to send after resolving."""
        media_type = item.get("media_type", "")
        ids = item.get("ids") or {}
        seen: set[tuple[str, str]] = set()
        contributions: list[tuple[int, str, str, str]] = []
        for id_type, item_key in [
            ("mal", "mal_id"),
            ("anilist", "anilist_id"),
            ("mal", "root_mal_id"),
            ("anilist", "root_anilist_id"),
            ("imdb", "imdb_id"),
            ("tvdb", "tvdb_id"),
            ("anidb", "anidb_id"),
            ("trakt", "trakt_id"),
        ]:
            id_value = (
                item.get(item_key)
                or ids.get(id_type)
                or ids.get(item_key)
                or ids.get(f"root_{id_type}")
            )
            if not id_value:
                continue
            key = (id_type, str(id_value))
            if key in seen:
                continue
            seen.add(key)
            contribution_key = (int(tmdb_id), str(media_type), id_type, str(id_value))
            with self._mapping_contribution_lock:
                if contribution_key in self._contributed_mapping_keys:
                    continue
                self._contributed_mapping_keys.add(contribution_key)
            contributions.append(contribution_key)
        return contributions

    def _flush_id_mapping_contributions(self, contributions: list[tuple[int, str, str, str]]) -> None:
        if not contributions:
            return
        pool = ThreadPoolExecutor(max_workers=min(_MAPPING_WRITE_WORKERS, len(contributions)))
        shutdown_wait = True
        try:
            futures = {
                pool.submit(self._pmdb.create_id_mapping, tmdb_id, media_type, id_type, id_value): (
                    tmdb_id,
                    media_type,
                    id_type,
                    id_value,
                )
                for tmdb_id, media_type, id_type, id_value in contributions
            }
            for future in self._iter_completed_futures(futures):
                tmdb_id, media_type, id_type, id_value = futures[future]
                try:
                    future.result()
                    logger.debug(
                        "Contributed %s mapping: %s=%s -> tmdb_id=%d",
                        media_type,
                        id_type,
                        id_value,
                        tmdb_id,
                    )
                except SyncCancelled:
                    raise
                except Exception as exc:
                    logger.debug("Failed to contribute ID mapping: %s", exc)
        except SyncCancelled:
            shutdown_wait = False
            raise
        finally:
            pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)

    def _contribute_id_mapping(self, item: dict, tmdb_id: int) -> None:
        """Compatibility wrapper for one-off activity/resume backfills."""
        self._flush_id_mapping_contributions(self._collect_id_mapping_contributions(item, tmdb_id))

    def _should_backfill_pmdb_mapping(self, item: dict) -> bool:
        return (
            not self._config.sync.dry_run
            and (item.get("simkl_type") == "anime" or not item.get("tmdb_id"))
        )

    def _dry_run_report(self, resolved: list[dict], list_name: str, stats: SyncStats) -> SyncStats:
        logger.info(
            "  └ [DRY RUN] '%s'  would add=%d  unresolved=%d",
            list_name,
            len(resolved),
            stats.items_skipped_unresolved,
        )
        for item in resolved:
            logger.debug(
                "    [DRY RUN] %s  year=%s  tmdb=%s  type=%s",
                item["title"],
                item.get("year"),
                item["resolved_tmdb_id"],
                item["media_type"],
            )
        return stats

    def _write_watched_history_items(
        self,
        items: list[dict],
        existing_counts: dict[str, int],
        stats: SyncStats,
        status_message: str,
        total_source: int = 0,
    ) -> None:
        if not items:
            return

        deduped_items: list[dict] = []
        pending_seen: set[str] = set()
        for item in items:
            key = self._watched_identity_key(item)
            if key and key in pending_seen:
                stats.items_skipped_duplicate += 1
                continue
            if key:
                pending_seen.add(key)
            deduped_items.append(item)

        items = deduped_items
        if not items:
            return

        total_to_write = len(items)
        written = 0
        self._set_status(f"{status_message} (0/{total_to_write} new)")
        pool = ThreadPoolExecutor(max_workers=min(_ACTIVITY_WRITE_WORKERS, len(items)))
        shutdown_wait = True
        try:
            futures = {
                pool.submit(
                    self._pmdb.mark_watched,
                    tmdb_id=int(item["tmdb_id"]),
                    media_type=item["media_type"],
                    season=item.get("season"),
                    episode=item.get("episode"),
                    watched_at=item.get("watched_at"),
                    dedupe=True,
                ): item
                for item in items
            }
            for future in self._iter_completed_futures(futures):
                item = futures[future]
                key = self._watched_identity_key(item)
                try:
                    future.result()
                    if key:
                        existing_counts[key] = 1
                    stats.items_added += 1
                    written += 1
                    if written % 10 == 0 or written == total_to_write:
                        self._set_status(f"{status_message} ({written}/{total_to_write} new)")
                        self._publish_progress([stats])
                except SyncCancelled:
                    raise
                except Exception as exc:
                    stats.errors.append(
                        f"Failed to import watched item '{item.get('title', 'Unknown')}': {exc}"
                    )
        except SyncCancelled:
            shutdown_wait = False
            raise
        finally:
            pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)

    def _remove_stale(self, list_id: str, existing_items: list[dict], desired_keys: set[str]) -> int:
        stale_items: list[dict] = []
        for item in existing_items:
            self._check_cancelled()
            key = f"{item.get('tmdb_id')}:{item.get('media_type')}"
            if key not in desired_keys:
                stale_items.append(item)

        removed = 0
        if stale_items:
            pool = ThreadPoolExecutor(max_workers=min(_LIST_WRITE_WORKERS, len(stale_items)))
            shutdown_wait = True
            try:
                futures = {
                    pool.submit(self._pmdb.remove_item_from_list, list_id, item["id"]): item
                    for item in stale_items
                }
                for future in self._iter_completed_futures(futures):
                    item = futures[future]
                    try:
                        future.result()
                        removed += 1
                        self._record_cached_list_item_remove(list_id, item.get("id", ""))
                        logger.debug("Removed stale item tmdb=%s from list %s", item.get("tmdb_id"), list_id)
                    except SyncCancelled:
                        raise
                    except Exception as exc:
                        logger.error("Failed to remove item %s: %s", item.get("id"), exc)
            except SyncCancelled:
                shutdown_wait = False
                raise
            finally:
                pool.shutdown(wait=shutdown_wait, cancel_futures=not shutdown_wait)
        if removed:
            logger.info("  │ Removed %d stale item(s) from list %s", removed, list_id)
        return removed

    @staticmethod
    def _build_existing_map(items: list[dict]) -> dict[str, dict]:
        result = {}
        for item in items:
            tmdb_id = item.get("tmdb_id")
            media_type = item.get("media_type")
            if tmdb_id and media_type:
                result[f"{tmdb_id}:{media_type}"] = item
        return result

    @staticmethod
    def _log_results(all_stats: list[SyncStats]) -> None:
        total_added = sum(s.items_added for s in all_stats)
        total_removed = sum(s.items_removed for s in all_stats)
        total_errors = sum(len(s.errors) for s in all_stats)

        logger.info("── Sync Summary ───────────────────────────────────────────")
        for stats in all_stats:
            if not stats.list_name and not stats.items_fetched and not stats.items_added and not stats.errors:
                continue
            parts = [f"added={stats.items_added}"]
            if stats.items_removed:
                parts.append(f"removed={stats.items_removed}")
            if stats.items_skipped_duplicate:
                parts.append(f"dup={stats.items_skipped_duplicate}")
            if stats.items_skipped_unresolved:
                parts.append(f"unresolved={stats.items_skipped_unresolved}")
            if stats.errors:
                parts.append(f"⚠ errors={len(stats.errors)}")
            logger.info("  %-45s %s", f"'{stats.display_name or stats.list_name}'", "  ".join(parts))
            for err in stats.errors:
                logger.error("    ✗ %s", err)

        logger.info("── Total: added=%d  removed=%d  errors=%d ─────────────────",
                    total_added, total_removed, total_errors)
        logger.info("▶  SYNC COMPLETE")
        logger.info("═" * 60)

    def _set_status(self, status: str) -> None:
        self._check_cancelled()
        if self._status_callback:
            try:
                self._status_callback(status)
            except Exception:
                logger.debug("Status callback failed", exc_info=True)

    def _publish_progress(self, rows: list[SyncStats], force: bool = False) -> None:
        if not self._progress_callback:
            return
        now = time.monotonic()
        if not force and now - self._last_progress_publish < 5.0:
            return
        try:
            for row in rows:
                self._remember_progress_row(row)
            payload = list(self._live_progress_rows.values())
            self._progress_callback(payload)
            self._last_progress_publish = now
        except Exception:
            logger.debug("Progress callback failed", exc_info=True)

    def _remember_progress_row(self, row: SyncStats) -> None:
        key = f"{row.display_name or row.list_name or ''}|{row.source_name or ''}"
        self._live_progress_rows[key] = {
            "list_name": row.list_name,
            "display_name": row.display_name,
            "source_name": row.source_name,
            "items_fetched": row.items_fetched,
            "items_resolved": row.items_resolved,
            "items_added": row.items_added,
            "items_removed": row.items_removed,
            "items_skipped_duplicate": row.items_skipped_duplicate,
            "items_skipped_unresolved": row.items_skipped_unresolved,
            "error_count": len(row.errors),
            "phase_timings": dict(row.phase_timings),
            "match_breakdown": dict(row.match_breakdown),
            "unresolved_reason_counts": dict(row.unresolved_reason_counts),
            "pmdb_metrics": dict(row.pmdb_metrics),
        }

    def _publish_pending_list(self, list_name: str, display_name: str, source_name: str) -> None:
        self._publish_progress([
            SyncStats(
                list_name=list_name,
                display_name=display_name,
                source_name=source_name,
                items_fetched=0,
            )
        ], force=True)

    def _prime_pmdb_list_index(self) -> None:
        refresh = getattr(self._pmdb, "refresh_lists_index", None)
        if not callable(refresh):
            return
        try:
            index = refresh() or {}
        except Exception as exc:
            logger.debug("Could not prefetch PMDB list index: %s", exc)
            return
        with self._pmdb_cache_lock:
            self._pmdb_run_list_index = dict(index)

    def _get_or_create_pmdb_list_cached(self, name: str, description: str, is_public: bool = False, list_type: str = "custom") -> dict:
        lookup_name = str(name or "").strip()
        with self._pmdb_cache_lock:
            # For watchlist type, search by type key in cache
            if list_type == "watchlist":
                existing = None
                if self._pmdb_run_list_index is not None:
                    for item in self._pmdb_run_list_index.values():
                        if str(item.get("type", "")).lower() == "watchlist":
                            existing = item
                            break
            else:
                existing = None if self._pmdb_run_list_index is None else self._pmdb_run_list_index.get(lookup_name)
        if existing:
            return dict(existing)
        pmdb_list = self._pmdb.get_or_create_list(name, description, is_public=is_public, list_type=list_type)
        with self._pmdb_cache_lock:
            if self._pmdb_run_list_index is None:
                self._pmdb_run_list_index = {}
            cache_key = str(pmdb_list.get("name", lookup_name)).strip() or lookup_name
            self._pmdb_run_list_index[cache_key] = dict(pmdb_list)
        return pmdb_list

    def _get_cached_list_items(self, list_id: str) -> list[dict]:
        with self._pmdb_cache_lock:
            cached = self._pmdb_list_items_cache.get(str(list_id))
        if cached is not None:
            return [dict(item) for item in cached]
        items = list(self._pmdb.get_list_items(list_id))
        with self._pmdb_cache_lock:
            self._pmdb_list_items_cache[str(list_id)] = [dict(item) for item in items]
        return [dict(item) for item in items]

    def _record_cached_list_item_add(self, list_id: str, item: dict) -> None:
        with self._pmdb_cache_lock:
            cached = self._pmdb_list_items_cache.get(str(list_id))
            if cached is None:
                return
            cached.append(dict(item))

    def _record_cached_list_item_remove(self, list_id: str, item_id: str) -> None:
        with self._pmdb_cache_lock:
            cached = self._pmdb_list_items_cache.get(str(list_id))
            if cached is None:
                return
            self._pmdb_list_items_cache[str(list_id)] = [
                dict(item) for item in cached
                if str(item.get("id", "")) != str(item_id)
            ]

    @staticmethod
    def _delta_counter_snapshot(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
        return {
            key: max(0, int(after.get(key, 0)) - int(before.get(key, 0)))
            for key in sorted(set(before) | set(after))
        }

    def _iter_completed_futures(self, futures) -> object:
        pending = set(futures.keys() if isinstance(futures, dict) else futures)
        while pending:
            self._check_cancelled()
            done, pending = wait(
                pending,
                timeout=_FUTURE_POLL_INTERVAL,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                yield future
        self._check_cancelled()

    def _check_cancelled(self) -> None:
        if not self._cancel_requested_callback:
            return
        try:
            if self._cancel_requested_callback():
                raise SyncCancelled("Sync stopped by user")
        except SyncCancelled:
            raise
        except Exception:
            logger.debug("Cancel callback failed", exc_info=True)

    @staticmethod
    def _watched_key(item: dict) -> str:
        tmdb_id = item.get("tmdb_id")
        media_type = item.get("media_type")
        if not tmdb_id or not media_type:
            return ""
        season = item.get("season")
        episode = item.get("episode")
        watched_at = item.get("watched_at")
        return f"{tmdb_id}:{media_type}:{season if season is not None else ''}:{episode if episode is not None else ''}:{watched_at if watched_at is not None else 'null'}"

    @staticmethod
    def _watched_identity_key(item: dict) -> str:
        tmdb_id = item.get("tmdb_id")
        media_type = item.get("media_type")
        if not tmdb_id or not media_type:
            return ""
        season = item.get("season")
        episode = item.get("episode")
        return f"{tmdb_id}:{media_type}:{season if season is not None else ''}:{episode if episode is not None else ''}"

    @staticmethod
    def _resume_key(item: dict) -> str:
        tmdb_id = item.get("tmdb_id")
        media_type = item.get("media_type")
        if not tmdb_id or not media_type:
            return ""
        season = item.get("season")
        episode = item.get("episode")
        return f"{tmdb_id}:{media_type}:{season if season is not None else ''}:{episode if episode is not None else ''}"

    @staticmethod
    def _resume_matches(existing: dict, payload: dict) -> bool:
        return (
            int(existing.get("position_ms", 0) or 0) == int(payload.get("position_ms", 0) or 0)
            and int(existing.get("runtime_ms", 0) or 0) == int(payload.get("runtime_ms", 0) or 0)
        )

    def _resolve_activity_item(self, item: dict) -> dict:
        if self._should_force_anime_re_resolve(item):
            match_result = self._matcher.resolve_match(item)
            tmdb_id = match_result.tmdb_id
            if tmdb_id is not None:
                if self._should_backfill_pmdb_mapping(item):
                    self._contribute_id_mapping(item, tmdb_id)
                return {
                    **item,
                    "tmdb_id": tmdb_id,
                }
        if item.get("tmdb_id"):
            if self._should_backfill_pmdb_mapping(item):
                try:
                    self._contribute_id_mapping(item, int(item["tmdb_id"]))
                except (TypeError, ValueError):
                    pass
            return item
        match_result = self._matcher.resolve_match(item)
        tmdb_id = match_result.tmdb_id
        if tmdb_id is None:
            return item
        if self._should_backfill_pmdb_mapping(item):
            self._contribute_id_mapping(item, tmdb_id)
        return {
            **item,
            "tmdb_id": tmdb_id,
        }

    @staticmethod
    def _should_force_anime_re_resolve(item: dict) -> bool:
        return (
            str(item.get("simkl_type", "")).strip().lower() == "anime"
            and str(item.get("media_type", "")).strip().lower() == "tv"
            and any(item.get(key) for key in ("anilist_id", "root_anilist_id", "mal_id", "root_mal_id"))
        )

    def _dedupe_activity_history_items(self, items: list[dict]) -> list[dict]:
        deduped: list[dict] = []
        seen: set[str] = set()
        for item in items:
            key = self._watched_identity_key(item)
            if not key:
                key = (
                    f"{item.get('title', '')}:"
                    f"{item.get('watched_at', '')}:"
                    f"{item.get('aggregate_watched_count', '')}:"
                    f"{item.get('simkl_type', '')}"
                )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    @staticmethod
    def _chunked(items: list[dict], size: int) -> list[list[dict]]:
        return [items[index:index + size] for index in range(0, len(items), size)]

    @staticmethod
    def _merge_activity_stats(rows: list[SyncStats]) -> list[SyncStats]:
        grouped: dict[str, SyncStats] = {}

        for row in rows:
            if "Watch History" in row.display_name:
                key = "watch_history"
                display_name = "Watch History"
            elif "Resume Progress" in row.display_name:
                key = "resume_progress"
                display_name = "Resume Progress"
            else:
                continue

            aggregate = grouped.get(key)
            if not aggregate:
                aggregate = SyncStats(list_name="", display_name=display_name, source_name="")
                grouped[key] = aggregate

            aggregate.items_fetched += row.items_fetched
            aggregate.items_resolved += row.items_resolved
            aggregate.items_added += row.items_added
            aggregate.items_removed += row.items_removed
            aggregate.items_skipped_duplicate += row.items_skipped_duplicate
            aggregate.items_skipped_unresolved += row.items_skipped_unresolved
            aggregate.errors.extend(list(row.errors))
            if row.history_cursor:
                aggregate.history_cursor = row.history_cursor

            existing_sources = {part.strip() for part in aggregate.source_name.split("+") if part.strip()}
            if row.source_name:
                existing_sources.add(row.source_name)
            aggregate.source_name = " + ".join(sorted(existing_sources))

        return [grouped[key] for key in ("watch_history", "resume_progress") if key in grouped]

    @staticmethod
    def _latest_history_cursor(items: list[dict], existing_cursor: str = "") -> str:
        latest = str(existing_cursor or "").strip()
        for item in items or []:
            watched_at = str(item.get("watched_at", "") or "").strip()
            if watched_at and (not latest or watched_at > latest):
                latest = watched_at
        return latest

    @staticmethod
    def _normalize_sync_modes(sync_modes: dict | None, config: AppConfig | None = None) -> dict[str, bool]:
        if sync_modes is None:
            raw = {
                "lists": True,
                "history": bool((config.sync.simkl_sync_watched_history or config.sync.trakt_sync_watched_history)) if config else False,
                "resume": bool(config.sync.trakt_sync_resume_progress) if config else False,
            }
        else:
            raw = sync_modes
        return {
            "lists": bool(raw.get("lists", True)),
            "history": bool(raw.get("history", False)),
            "resume": bool(raw.get("resume", False)),
        }

    @staticmethod
    def _normalize_managed_lists(items: list[dict] | None) -> dict[str, dict]:
        normalized: dict[str, dict] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            list_name = str(item.get("list_name", "")).strip()
            if not list_name:
                continue
            normalized[list_name] = {
                "list_name": list_name,
                "list_id": str(item.get("list_id", "")).strip(),
                "display_name": str(item.get("display_name", "")).strip(),
                "source_name": str(item.get("source_name", "")).strip(),
                "selection": dict(item.get("selection", {})) if isinstance(item.get("selection"), dict) else {},
            }
        return normalized

    @staticmethod
    def _selection_identity(selection: dict | None) -> str:
        if not isinstance(selection, dict):
            return ""
        source = str(selection.get("source", "")).strip().lower()
        if source == "simkl":
            return f"simkl:{selection.get('media_type', '')}:{selection.get('status', '')}"
        if source == "anilist":
            return f"anilist:{selection.get('status', '')}"
        if source == "mdblist":
            return f"mdblist:{selection.get('id', '')}:{selection.get('mediatype', '')}"
        if source == "trakt":
            kind = str(selection.get("kind", "")).strip().lower()
            if kind == "watchlist":
                return f"trakt:watchlist:{selection.get('media_type', '')}"
            if kind == "default":
                return f"trakt:default:{selection.get('catalog_key', '')}"
            return f"trakt:list:{selection.get('user', '')}:{selection.get('slug', '')}"
        return ""

    @staticmethod
    def _selection_suffix(source_name: str, selection: dict | None) -> str:
        if not isinstance(selection, dict):
            return source_name or "SyncMeta"
        source = str(selection.get("source", "")).strip().lower()
        if source == "trakt":
            kind = str(selection.get("kind", "")).strip().lower()
            if kind == "watchlist":
                media_type = str(selection.get("media_type", "")).strip().lower()
                label = _TYPE_LABELS.get(media_type, media_type.title() or "Watchlist")
                return f"Trakt {label}"
            if kind == "default":
                return "Trakt"
            user = str(selection.get("user", "")).strip()
            return f"Trakt {user}" if user else "Trakt"
        if source == "mdblist":
            return "MDBList"
        if source == "anilist":
            return "AniList"
        if source == "simkl":
            return "SIMKL"
        return source_name or "SyncMeta"

    def _resolve_managed_list_name(self, base_name: str, source_name: str, selection: dict | None) -> str:
        identity = self._selection_identity(selection)
        if identity:
            for item in self._managed_lists.values():
                if self._selection_identity(item.get("selection")) == identity:
                    return str(item.get("list_name", "")).strip() or base_name

        candidate = base_name
        suffix = self._selection_suffix(source_name, selection)
        attempt = 0

        while True:
            conflict = False
            existing_managed = self._managed_lists.get(candidate)
            if existing_managed and self._selection_identity(existing_managed.get("selection")) != identity:
                conflict = True
            elif attempt == 0:
                try:
                    existing_pmdb = self._pmdb.find_list_by_name(candidate)
                except Exception:
                    existing_pmdb = None
                if existing_pmdb:
                    existing_id = str(existing_pmdb.get("id", "")).strip()
                    managed_match = any(str(item.get("list_id", "")).strip() == existing_id for item in self._managed_lists.values())
                    if not managed_match:
                        conflict = True

            if not conflict:
                return candidate

            attempt += 1
            candidate = f"{base_name} ({suffix})" if attempt == 1 else f"{base_name} ({suffix} {attempt})"

    def _register_managed_list(
        self,
        list_name: str,
        list_id: str,
        display_name: str,
        source_name: str,
        selection: dict | None = None,
    ) -> None:
        self._managed_lists[list_name] = {
            "list_name": list_name,
            "list_id": list_id,
            "display_name": display_name,
            "source_name": source_name,
            "selection": dict(selection or {}),
        }

    def _delete_disabled_lists(self, desired_names: set[str]) -> None:
        stale_names = [list_name for list_name in self._managed_lists if list_name not in desired_names]
        for list_name in stale_names:
            self._check_cancelled()
            entry = self._managed_lists.get(list_name, {})
            list_id = str(entry.get("list_id", "")).strip()
            try:
                self._set_status(f"Deleting disabled PublicMetaDB list {list_name}")
                if not list_id:
                    existing = self._pmdb.find_list_by_name(list_name)
                    if not existing:
                        logger.info("Managed list '%s' no longer exists in PublicMetaDB", list_name)
                        self._managed_lists.pop(list_name, None)
                        continue
                    list_id = str(existing.get("id", "")).strip()
                self._pmdb.delete_list(list_id)
                logger.info("Deleted disabled PublicMetaDB list '%s' (id=%s)", list_name, list_id)
                self._managed_lists.pop(list_name, None)
            except SyncCancelled:
                raise
            except Exception as exc:
                logger.error("Failed to delete disabled PublicMetaDB list '%s': %s", list_name, exc)
