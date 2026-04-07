"""Core sync logic for syncing configured sources into PublicMetaDB."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .anilist_client import AniListClient
from .config import AniListConfig, AppConfig
from .matcher import ItemMatcher
from .mdblist_client import MdbListClient
from .publicmetadb_client import PublicMetaDBClient
from .simkl_client import SimklClient
from .trakt_client import TraktClient

logger = logging.getLogger(__name__)


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
    ):
        self._config = config
        self._simkl = SimklClient(config.simkl, cancel_requested_callback=cancel_requested_callback)
        self._trakt = TraktClient(config.trakt, cancel_requested_callback=cancel_requested_callback)
        self._mdblist = MdbListClient(config.mdblist)
        self._pmdb = PublicMetaDBClient(config.pmdb)
        self._anilist_root_client = AniListClient(
            config.anilist if config.anilist.enabled else AniListConfig()
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
                with ThreadPoolExecutor(max_workers=2) as pool:
                    future_simkl = pool.submit(self._sync_simkl)
                    future_anilist = pool.submit(self._sync_anilist)
                    for future in as_completed([future_simkl, future_anilist]):
                        try:
                            all_stats.extend(future.result())
                        except SyncCancelled:
                            cancelled = True
                        except Exception as exc:
                            logger.error("Provider sync failed: %s", exc)
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

    def _sync_simkl(self) -> list[SyncStats]:
        """Sync all configured SIMKL lists."""
        media_types = list(self._config.sync.media_types)

        stats: list[SyncStats] = []
        if not media_types:
            return stats

        for simkl_type in media_types:
            self._check_cancelled()
            statuses = self._config.simkl.selected_statuses.get(simkl_type, [])
            if not statuses:
                continue

            for status_key in statuses:
                self._check_cancelled()

                self._set_status(f"Fetching SIMKL {_STATUS_LABELS.get(status_key, status_key)} {simkl_type}")
                grouped = self._simkl.get_status(status_key, [simkl_type])
                items = grouped.get(simkl_type, [])

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
        cursor = self._config.sync.simkl_history_cursor or None
        items = self._simkl.get_watched_history(since=cursor)

        try:
            existing_items = self._pmdb.get_watched_history()
        except Exception as exc:
            stats.errors.append(f"Failed to load PublicMetaDB watched history: {exc}")
            return stats

        if self._config.sync.simkl_history_anime_only:
            items = [item for item in items if str(item.get("simkl_type", "")).strip().lower() == "anime"]
        items = self._expand_simkl_aggregate_history(items)
        stats.history_cursor = self._latest_history_cursor(items, cursor or "")
        stats.items_fetched = len(items)

        existing_counts: dict[str, int] = {}
        for existing_item in existing_items:
            key = self._watched_identity_key(existing_item)
            if key:
                existing_counts[key] = existing_counts.get(key, 0) + 1

        for item in items:
            self._check_cancelled()
            item = self._resolve_activity_item(item)
            item = self._remap_simkl_anime_history_item(item)
            key = self._watched_identity_key(item)
            if not key:
                stats.items_skipped_unresolved += 1
                continue
            stats.items_resolved += 1
            if existing_counts.get(key, 0) > 0:
                stats.items_skipped_duplicate += 1
                continue
            if self._config.sync.dry_run:
                existing_counts[key] = 1
                stats.items_added += 1
                continue
            try:
                self._set_status("Writing SIMKL watched history to PublicMetaDB")
                self._pmdb.mark_watched(
                    tmdb_id=int(item["tmdb_id"]),
                    media_type=item["media_type"],
                    season=item.get("season"),
                    episode=item.get("episode"),
                    watched_at=item.get("watched_at"),
                    dedupe=True,
                )
                existing_counts[key] = 1
                stats.items_added += 1
            except SyncCancelled:
                raise
            except Exception as exc:
                stats.errors.append(f"Failed to import watched item '{item.get('title', 'Unknown')}': {exc}")

        # Second pass: for completed TV anime, mark the entire TMDB season watched.
        # Individual episode history may be incomplete (e.g. only first cour tracked),
        # so if the user marked a show "completed" on SIMKL we trust that and stamp
        # the whole season rather than relying solely on per-episode records.
        self._sync_completed_anime_seasons(stats, existing_counts)
        return stats

    def _sync_completed_anime_seasons(self, stats: SyncStats, existing_counts: dict[str, int]) -> None:
        """Mark entire TMDB seasons watched for anime the user completed on SIMKL."""
        try:
            grouped = self._simkl.get_status("completed", ["anime"])
            items = grouped.get("anime", [])
        except Exception as exc:
            logger.warning("Could not fetch SIMKL completed anime for season-level sync: %s", exc)
            return

        for item in items:
            self._check_cancelled()
            if item.get("media_type") == "movie":
                continue  # movies are handled as individual watched entries

            resolved = self._resolve_activity_item(item)
            tmdb_id = resolved.get("tmdb_id")
            if not tmdb_id:
                continue

            # Determine the correct TMDB season using anime-seasons mapping when
            # the item is a sequel (offset > 0 means it's not the root season).
            offset = int(resolved.get("root_episode_offset") or 0)
            tmdb_season = 1
            if offset > 0:
                try:
                    anime_seasons = self._pmdb.get_anime_seasons(int(tmdb_id))
                    if anime_seasons:
                        remapped = self._map_episode_via_anime_seasons(anime_seasons, offset, 1)
                        if remapped:
                            tmdb_season = remapped.get("season", 1)
                except Exception:
                    pass

            season_key = f"{tmdb_id}:tv:{tmdb_season}:"
            if existing_counts.get(season_key, 0) > 0:
                stats.items_skipped_duplicate += 1
                continue

            if self._config.sync.dry_run:
                existing_counts[season_key] = 1
                stats.items_added += 1
                continue

            try:
                self._set_status("Marking completed anime seasons in PublicMetaDB")
                self._pmdb.mark_watched(
                    tmdb_id=int(tmdb_id),
                    media_type="tv",
                    season=tmdb_season,
                    watched_at=item.get("last_watched_at") or item.get("watched_at"),
                    dedupe=True,
                )
                existing_counts[season_key] = 1
                stats.items_added += 1
            except SyncCancelled:
                raise
            except Exception as exc:
                stats.errors.append(
                    f"Failed to mark completed season for '{item.get('title', 'Unknown')}': {exc}"
                )

    def _expand_simkl_aggregate_history(self, items: list[dict]) -> list[dict]:
        expanded: list[dict] = []
        for item in items:
            aggregate_count = item.get("aggregate_watched_count")
            if aggregate_count:
                resolved = self._resolve_activity_item(item)
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

        # ── Path 1 & 2: Fribb anime-lists ──────────────────────────────────────
        fribb = self._lookup_fribb_entry(item)
        if fribb is not None:
            remapped = self._remap_via_fribb(fribb, tmdb_id, episode)
            if remapped:
                return {**item, **remapped}

        # Remaining paths only make sense when there is a non-zero offset
        # (offset == 0 means the item IS the root season; no remapping needed).
        if offset <= 0 or tmdb_id <= 0:
            return item

        # ── Path 3: PMDB anime-seasons with absolute episode offset ─────────────
        try:
            anime_seasons = self._pmdb.get_anime_seasons(tmdb_id)
            if anime_seasons:
                remapped = self._map_episode_via_anime_seasons(anime_seasons, offset, episode)
                if remapped:
                    return {**item, **remapped}
        except Exception:
            pass

        # ── Path 4: Single-season TMDB heuristic ───────────────────────────────
        try:
            season_plan = self._simkl._get_tmdb_season_plan_cached(tmdb_id)
            positive_seasons = [(sn, cnt) for sn, cnt in season_plan if sn > 0 and cnt > 0]
            if len(positive_seasons) == 1 and positive_seasons[0][0] == 1:
                return {**item, "season": 1, "episode": offset + episode}
        except Exception:
            pass

        return item

    def _lookup_fribb_entry(self, item: dict) -> dict | None:
        """Return the Fribb anime-lists entry for this SIMKL item, or None."""
        from . import fribb_client
        ids = item.get("ids") or {}

        for id_key in ("anilist_id", "root_anilist_id"):
            raw = item.get(id_key) or ids.get("anilist") or ids.get("root_anilist")
            if raw:
                try:
                    entry = fribb_client.lookup_by_anilist(int(raw))
                    if entry:
                        return entry
                except (TypeError, ValueError):
                    pass

        for id_key in ("mal_id", "root_mal_id"):
            raw = item.get(id_key) or ids.get("mal") or ids.get("root_mal")
            if raw:
                try:
                    entry = fribb_client.lookup_by_mal(int(raw))
                    if entry:
                        return entry
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
                anime_seasons = self._pmdb.get_anime_seasons(tmdb_id)
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
        cursor = self._config.sync.trakt_history_cursor or None
        items = self._trakt.get_watched_history(since=cursor)

        try:
            existing_items = self._pmdb.get_watched_history()
        except Exception as exc:
            stats.errors.append(f"Failed to load PublicMetaDB watched history: {exc}")
            return stats

        stats.items_fetched = len(items)
        stats.history_cursor = self._latest_history_cursor(items, cursor or "")

        existing_counts: dict[str, int] = {}
        for existing_item in existing_items:
            key = self._watched_identity_key(existing_item)
            if key:
                existing_counts[key] = existing_counts.get(key, 0) + 1

        for item in items:
            self._check_cancelled()
            item = self._resolve_activity_item(item)
            key = self._watched_identity_key(item)
            if not key:
                stats.items_skipped_unresolved += 1
                continue
            stats.items_resolved += 1
            if existing_counts.get(key, 0) > 0:
                stats.items_skipped_duplicate += 1
                continue
            if self._config.sync.dry_run:
                existing_counts[key] = 1
                stats.items_added += 1
                continue
            try:
                self._set_status("Writing Trakt watched history to PublicMetaDB")
                self._pmdb.mark_watched(
                    tmdb_id=int(item["tmdb_id"]),
                    media_type=item["media_type"],
                    season=item.get("season"),
                    episode=item.get("episode"),
                    watched_at=item.get("watched_at"),
                    dedupe=True,
                )
                existing_counts[key] = 1
                stats.items_added += 1
            except SyncCancelled:
                raise
            except Exception as exc:
                stats.errors.append(f"Failed to import watched item '{item.get('title', 'Unknown')}': {exc}")
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

        client = AniListClient(self._config.anilist)
        stats: list[SyncStats] = []

        # Collect all items from every status first, then pre-warm the shared
        # AniList prequel-chain cache concurrently before the list sync begins.
        # This ensures SIMKL anime (which runs concurrently) gets cache hits for
        # any AniList IDs it shares, instead of each thread walking the same chains.
        all_items_by_status: list[tuple[str, list[dict]]] = []
        for status_key in self._config.anilist.selected_statuses:
            self._check_cancelled()
            self._set_status(f"Fetching AniList {_STATUS_LABELS.get(status_key, status_key)} anime")
            items = client.get_status(status_key)
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
        if all_anilist_ids:
            self._set_status("Pre-warming anime metadata cache")
            get_ctx = getattr(client, "_get_root_context", None)
            if callable(get_ctx):
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futures = {pool.submit(get_ctx, aid): aid for aid in all_anilist_ids}
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as exc:
                            logger.debug("Cache warm failed for anilist_id=%s: %s", futures[future], exc)
            logger.info("Pre-warmed anime chain cache for %d AniList IDs", len(all_anilist_ids))

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

        for trakt_list in selected_default_lists:
            self._check_cancelled()
            self._publish_pending_list(trakt_list["name"], trakt_list["name"], "Trakt")
            self._set_status(f"Fetching Trakt catalog {trakt_list['name']}")
            items = self._filter_trakt_items(
                self._trakt.get_default_catalog(trakt_list.get("catalog_key") or trakt_list.get("slug", ""))
            )
            name = trakt_list["name"]
            description = f"Auto-synced Trakt default catalog '{trakt_list['name']}'"
            stats.append(
                self._sync_list(
                    items,
                    name,
                    description,
                    display_name=trakt_list["name"],
                    source_name="Trakt",
                    is_public=self._config.sync.trakt_personal_visibility == "public",
                    selection={
                        "source": "trakt",
                        "kind": "default",
                        "catalog_key": trakt_list.get("catalog_key", ""),
                        "name": trakt_list.get("name", ""),
                    },
                )
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

        for trakt_list in selected_liked_lists:
            self._check_cancelled()
            self._publish_pending_list(trakt_list["name"], trakt_list["name"], f"Trakt by {trakt_list['user']}")
            self._set_status(f"Fetching Trakt list {trakt_list['name']}")
            items = self._filter_trakt_items(
                self._trakt.get_list_items(trakt_list["user"], trakt_list["slug"])
            )
            name = trakt_list["name"]
            description = f"Auto-synced Trakt list '{trakt_list['name']}' by {trakt_list['user']}"
            stats.append(
                self._sync_list(
                    items,
                    name,
                    description,
                    display_name=trakt_list["name"],
                    source_name=f"Trakt by {trakt_list['user']}",
                    is_public=self._config.sync.trakt_public_visibility == "public",
                    selection={
                        "source": "trakt",
                        "kind": "selected-list",
                        "list_source": trakt_list.get("source", ""),
                        "user": trakt_list.get("user", ""),
                        "slug": trakt_list.get("slug", ""),
                        "name": trakt_list.get("name", ""),
                    },
                )
            )

        for trakt_list in selected_personal_lists:
            self._check_cancelled()
            self._publish_pending_list(trakt_list["name"], trakt_list["name"], f"Trakt by {trakt_list['user']}")
            self._set_status(f"Fetching Trakt list {trakt_list['name']}")
            items = self._filter_trakt_items(
                self._trakt.get_list_items(trakt_list["user"], trakt_list["slug"])
            )
            name = trakt_list["name"]
            description = f"Auto-synced your Trakt list '{trakt_list['name']}'"
            stats.append(
                self._sync_list(
                    items,
                    name,
                    description,
                    display_name=trakt_list["name"],
                    source_name=f"Trakt by {trakt_list['user']}",
                    is_public=self._config.sync.trakt_personal_visibility == "public",
                    selection={
                        "source": "trakt",
                        "kind": "selected-list",
                        "list_source": trakt_list.get("source", ""),
                        "user": trakt_list.get("user", ""),
                        "slug": trakt_list.get("slug", ""),
                        "name": trakt_list.get("name", ""),
                    },
                )
            )

        for trakt_list in selected_public_lists:
            self._check_cancelled()
            self._publish_pending_list(trakt_list["name"], trakt_list["name"], f"Trakt by {trakt_list['user']}")
            self._set_status(f"Fetching Trakt list {trakt_list['name']}")
            items = self._filter_trakt_items(
                self._trakt.get_list_items(trakt_list["user"], trakt_list["slug"])
            )
            name = trakt_list["name"]
            description = f"Auto-synced Trakt list '{trakt_list['name']}' by {trakt_list['user']}"
            stats.append(
                self._sync_list(
                    items,
                    name,
                    description,
                    display_name=trakt_list["name"],
                    source_name=f"Trakt by {trakt_list['user']}",
                    is_public=self._config.sync.trakt_public_visibility == "public",
                    selection={
                        "source": "trakt",
                        "kind": "selected-list",
                        "list_source": trakt_list.get("source", ""),
                        "user": trakt_list.get("user", ""),
                        "slug": trakt_list.get("slug", ""),
                        "name": trakt_list.get("name", ""),
                    },
                )
            )

        return stats

    def _sync_mdblist(self) -> list[SyncStats]:
        """Sync selected MDBList lists."""
        stats: list[SyncStats] = []

        for mdblist in self._dedupe_mdblist_lists(self._config.mdblist.selected_lists):
            self._check_cancelled()
            self._publish_pending_list(mdblist["name"], mdblist["name"], "MDBList")
            self._set_status(f"Fetching MDBList {mdblist['name']}")
            items = self._filter_mdblist_items(self._mdblist.get_list_items(mdblist["id"]))
            media_type = "movies" if mdblist["mediatype"] == "movie" else "shows"
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

    def _filter_mdblist_items(self, items: list[dict]) -> list[dict]:
        filtered = []
        for item in items:
            if item["media_type"] == "movie" and "movies" not in self._config.sync.media_types:
                continue
            if item["media_type"] == "tv" and "shows" not in self._config.sync.media_types:
                continue
            filtered.append(item)
        return filtered

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
        resolved: list[dict] = []
        for item in source_items:
            self._check_cancelled()
            tmdb_id = self._matcher.resolve_tmdb_id(item)
            if tmdb_id is not None:
                resolved.append({**item, "resolved_tmdb_id": tmdb_id})
                stats.items_resolved += 1
                # If SIMKL/AniList gave us a non-TMDB ID that we resolved via
                # the external-ID lookup chain, contribute that mapping back to
                # PMDB so the community benefits from the resolution.
                if not item.get("tmdb_id") and not self._config.sync.dry_run:
                    self._contribute_id_mapping(item, tmdb_id)
            else:
                stats.items_skipped_unresolved += 1
            self._publish_progress([stats])

        if self._config.sync.dry_run:
            return self._dry_run_report(resolved, actual_list_name, stats)

        try:
            self._set_status(f"Preparing PublicMetaDB list {actual_list_name}")
            pmdb_list = self._pmdb.get_or_create_list(actual_list_name, list_description, is_public=is_public)
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
        existing_items = self._pmdb.get_list_items(list_id)
        existing_map = self._build_existing_map(existing_items)
        desired_keys: set[str] = set()

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

            try:
                self._set_status(f"Adding items to {actual_list_name}")
                result = self._pmdb.add_item_to_list(list_id, tmdb_id, media_type)
                if result is not None:
                    existing_map[key] = {
                        "tmdb_id": tmdb_id,
                        "media_type": media_type,
                    }
                    stats.items_added += 1
                else:
                    message = f"Failed to add '{item['title']}' (tmdb={tmdb_id}): API returned no result"
                    stats.errors.append(message)
                self._publish_progress([stats])
            except SyncCancelled:
                raise
            except Exception as exc:
                stats.errors.append(f"Failed to add '{item['title']}' (tmdb={tmdb_id}): {exc}")
                self._publish_progress([stats], force=True)

        if self._config.sync.remove_missing:
            self._set_status(f"Removing stale items from {actual_list_name}")
            stats.items_removed = self._remove_stale(list_id, existing_items, desired_keys)
            self._publish_progress([stats], force=True)

        logger.info(
            "  └ '%s'  resolved=%d  added=%d  dup=%d  unresolved=%d  removed=%d%s",
            actual_list_name,
            stats.items_resolved,
            stats.items_added,
            stats.items_skipped_duplicate,
            stats.items_skipped_unresolved,
            stats.items_removed,
            f"  ⚠ {len(stats.errors)} error(s)" if stats.errors else "",
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

        # Only walk chains not already in the shared cache.
        uncached = [aid for aid in anilist_ids if aid not in _SHARED_ROOT_CONTEXT_CACHE]
        if not uncached:
            logger.info("SIMKL anime chain cache: all %d IDs already cached", len(anilist_ids))
            return

        logger.info(
            "Pre-warming SIMKL anime chain cache: %d uncached / %d total IDs",
            len(uncached), len(anilist_ids),
        )
        get_ctx = getattr(self._anilist_root_client, "_get_root_context", None)
        if not callable(get_ctx):
            return

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(get_ctx, aid): aid for aid in uncached}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    logger.debug("Chain pre-warm failed for anilist_id=%s: %s", futures[future], exc)

    def _contribute_id_mapping(self, item: dict, tmdb_id: int) -> None:
        """Silently contribute a resolved external ID mapping back to PMDB.

        When we resolve a TMDB ID via MAL/AniList/IMDB/TVDB (because the item
        had no TMDB ID), we submit that mapping to PMDB so the community can
        benefit from it on future lookups.  Failures are logged at DEBUG level
        and never propagate to the caller.
        """
        media_type = item.get("media_type", "")
        # Pick the best available external ID to contribute (prefer the most specific).
        for id_type, item_key in [
            ("mal", "mal_id"),
            ("anilist", "anilist_id"),
            ("imdb", "imdb_id"),
            ("tvdb", "tvdb_id"),
            ("anidb", "anidb_id"),
        ]:
            id_value = item.get(item_key) or (item.get("ids") or {}).get(id_type)
            if id_value:
                try:
                    self._pmdb.create_id_mapping(tmdb_id, media_type, id_type, str(id_value))
                    logger.debug(
                        "Contributed %s mapping: %s=%s → tmdb_id=%d",
                        media_type, id_type, id_value, tmdb_id,
                    )
                except Exception as exc:
                    logger.debug("Failed to contribute ID mapping: %s", exc)
                return  # Only contribute one mapping per item

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

    def _remove_stale(self, list_id: str, existing_items: list[dict], desired_keys: set[str]) -> int:
        removed = 0
        for item in existing_items:
            self._check_cancelled()
            key = f"{item.get('tmdb_id')}:{item.get('media_type')}"
            if key not in desired_keys:
                try:
                    self._pmdb.remove_item_from_list(list_id, item["id"])
                    removed += 1
                    logger.debug("Removed stale item tmdb=%s from list %s", item.get("tmdb_id"), list_id)
                except SyncCancelled:
                    raise
                except Exception as exc:
                    logger.error("Failed to remove item %s: %s", item.get("id"), exc)
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
        if item.get("tmdb_id"):
            return item
        tmdb_id = self._matcher.resolve_tmdb_id(item)
        if tmdb_id is None:
            return item
        return {
            **item,
            "tmdb_id": tmdb_id,
        }

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
