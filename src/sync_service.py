"""Core sync logic: SIMKL / AniList → PublicMetaDB one-way sync."""

import logging
from dataclasses import dataclass, field

from .config import AppConfig
from .matcher import ItemMatcher
from .publicmetadb_client import PublicMetaDBClient
from .simkl_client import SimklClient

logger = logging.getLogger(__name__)

# Human-readable labels per media type
_TYPE_LABELS = {
    "shows": "Series",
    "movies": "Movies",
    "anime": "Anime",
}

_STATUS_LABELS = {
    "watching": "Watching",
    "plantowatch": "Plan to Watch",
}


def _list_name(source: str, media_type: str, status: str) -> str:
    """Build a PMDB list name like 'SIMKL – Anime – Watching' or 'AniList – Anime – Watching'."""
    type_label = _TYPE_LABELS.get(media_type, media_type.title())
    status_label = _STATUS_LABELS.get(status, status.title())
    return f"{source} – {type_label} – {status_label}"


@dataclass
class SyncStats:
    """Counters for a single list sync."""
    list_name: str = ""
    items_fetched: int = 0
    items_resolved: int = 0
    items_added: int = 0
    items_removed: int = 0
    items_skipped_duplicate: int = 0
    items_skipped_unresolved: int = 0
    errors: list[str] = field(default_factory=list)


class SyncService:
    """Orchestrates the one-way sync from SIMKL to PublicMetaDB."""

    def __init__(self, config: AppConfig):
        self._config = config
        self._simkl = SimklClient(config.simkl)
        self._pmdb = PublicMetaDBClient(config.pmdb)
        self._matcher = ItemMatcher(self._pmdb)

    @property
    def simkl(self) -> SimklClient:
        return self._simkl

    def run(self) -> list[SyncStats]:
        """Execute a full sync cycle. Returns stats per list."""
        logger.info(
            "Starting sync (dry_run=%s, remove_missing=%s)",
            self._config.sync.dry_run, self._config.sync.remove_missing,
        )

        all_stats: list[SyncStats] = []

        # ── SIMKL sync ────────────────────────────────────────────
        all_stats.extend(self._sync_simkl())

        # ── AniList sync ──────────────────────────────────────────
        if self._config.anilist.enabled:
            all_stats.extend(self._sync_anilist())

        self._log_results(all_stats)
        return all_stats

    def _sync_simkl(self) -> list[SyncStats]:
        """Sync all configured SIMKL lists. Skips anime if AniList handles it."""
        media_types = self._config.sync.media_types
        if self._config.anilist.enabled:
            media_types = [t for t in media_types if t != "anime"]
            logger.info("AniList enabled — skipping SIMKL anime sync")
        stats: list[SyncStats] = []

        if not media_types:
            return stats

        watching_by_type = self._simkl.get_watching(media_types)
        ptw_by_type = self._simkl.get_plan_to_watch(media_types)

        for simkl_type in media_types:
            for status_key, grouped in [("watching", watching_by_type), ("plantowatch", ptw_by_type)]:
                items = grouped.get(simkl_type, [])
                name = _list_name("SIMKL", simkl_type, status_key)
                desc = f"Auto-synced '{_STATUS_LABELS[status_key]}' {_TYPE_LABELS.get(simkl_type, simkl_type)} from SIMKL"
                stats.append(self._sync_list(items, name, desc))

        return stats

    def _sync_anilist(self) -> list[SyncStats]:
        """Sync AniList anime Watching + Plan to Watch."""
        from .anilist_client import AniListClient

        client = AniListClient(self._config.anilist)
        stats: list[SyncStats] = []

        for status_key, fetcher in [("watching", client.get_watching), ("plantowatch", client.get_plan_to_watch)]:
            items = fetcher()
            name = _list_name("AniList", "anime", status_key)
            desc = f"Auto-synced '{_STATUS_LABELS[status_key]}' anime from AniList"
            stats.append(self._sync_list(items, name, desc))

        return stats

    def _sync_list(self, simkl_items: list[dict], list_name: str, list_description: str) -> SyncStats:
        """Sync a single SIMKL status+type list to a PublicMetaDB list."""
        stats = SyncStats(list_name=list_name, items_fetched=len(simkl_items))
        logger.info("Syncing '%s': %d SIMKL items", list_name, len(simkl_items))

        if not simkl_items:
            logger.info("  No items for '%s', skipping", list_name)
            return stats

        # Resolve TMDB IDs
        resolved: list[dict] = []
        for item in simkl_items:
            tmdb_id = self._matcher.resolve_tmdb_id(item)
            if tmdb_id is not None:
                resolved.append({**item, "resolved_tmdb_id": tmdb_id})
                stats.items_resolved += 1
            else:
                stats.items_skipped_unresolved += 1

        if self._config.sync.dry_run:
            return self._dry_run_report(resolved, list_name, stats)

        # Get or create the PMDB list (private)
        try:
            pmdb_list = self._pmdb.get_or_create_list(list_name, list_description)
        except Exception as e:
            stats.errors.append(f"Failed to get/create list: {e}")
            logger.error("Failed to get/create list '%s': %s", list_name, e)
            return stats

        list_id = pmdb_list["id"]

        # Fetch what's already in the PMDB list
        existing_items = self._pmdb.get_list_items(list_id)
        existing_map = self._build_existing_map(existing_items)

        # Track which keys should remain
        desired_keys: set[str] = set()

        for item in resolved:
            tmdb_id = item["resolved_tmdb_id"]
            media_type = item["media_type"]
            key = f"{tmdb_id}:{media_type}"
            desired_keys.add(key)

            if key in existing_map:
                stats.items_skipped_duplicate += 1
                continue

            try:
                self._pmdb.add_item_to_list(list_id, tmdb_id, media_type)
                stats.items_added += 1
            except Exception as e:
                msg = f"Failed to add '{item['title']}' (tmdb={tmdb_id}): {e}"
                stats.errors.append(msg)
                logger.error(msg)

        # Remove stale items if configured
        if self._config.sync.remove_missing:
            stats.items_removed = self._remove_stale(list_id, existing_items, desired_keys)

        return stats

    def _dry_run_report(self, resolved: list[dict], list_name: str, stats: SyncStats) -> SyncStats:
        logger.info("[DRY RUN] Would sync %d resolved items to '%s'", len(resolved), list_name)
        for item in resolved:
            logger.info(
                "  [DRY RUN] %s (year=%s, tmdb=%s, type=%s)",
                item["title"], item.get("year"), item["resolved_tmdb_id"], item["media_type"],
            )
        return stats

    def _remove_stale(self, list_id: str, existing_items: list[dict], desired_keys: set[str]) -> int:
        removed = 0
        for item in existing_items:
            key = f"{item.get('tmdb_id')}:{item.get('media_type')}"
            if key not in desired_keys:
                try:
                    self._pmdb.remove_item_from_list(list_id, item["id"])
                    removed += 1
                    logger.info("Removed stale item tmdb=%s from list %s", item.get("tmdb_id"), list_id)
                except Exception as e:
                    logger.error("Failed to remove item %s: %s", item.get("id"), e)
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
        for stats in all_stats:
            logger.info(
                "'%s': fetched=%d resolved=%d added=%d removed=%d "
                "dup=%d unresolved=%d errors=%d",
                stats.list_name,
                stats.items_fetched,
                stats.items_resolved,
                stats.items_added,
                stats.items_removed,
                stats.items_skipped_duplicate,
                stats.items_skipped_unresolved,
                len(stats.errors),
            )
            for err in stats.errors:
                logger.error("  Error: %s", err)
