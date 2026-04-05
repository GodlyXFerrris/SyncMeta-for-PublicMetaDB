"""Core sync logic for syncing configured sources into PublicMetaDB."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import AppConfig
from .matcher import ItemMatcher
from .mdblist_client import MdbListClient
from .publicmetadb_client import PublicMetaDBClient
from .simkl_client import SimklClient
from .trakt_client import TraktClient

logger = logging.getLogger(__name__)

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
}


def _list_name(source: str, media_type: str, status: str) -> str:
    """Build a PMDB list name like 'SIMKL - Anime - Watching'."""
    type_label = _TYPE_LABELS.get(media_type, media_type.title())
    status_label = _STATUS_LABELS.get(status, status.replace("_", " ").title())
    return f"{source} - {type_label} - {status_label}"


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


class SyncService:
    """Orchestrates one-way sync into PublicMetaDB."""

    def __init__(self, config: AppConfig, status_callback=None):
        self._config = config
        self._simkl = SimklClient(config.simkl)
        self._trakt = TraktClient(config.trakt)
        self._mdblist = MdbListClient(config.mdblist)
        self._pmdb = PublicMetaDBClient(config.pmdb)
        self._matcher = ItemMatcher(self._pmdb)
        self._status_callback = status_callback

    @property
    def simkl(self) -> SimklClient:
        return self._simkl

    def run(self) -> list[SyncStats]:
        """Execute a full sync cycle. Returns stats per synced list."""
        self._set_status("Preparing sync")
        logger.info(
            "Starting sync (dry_run=%s, remove_missing=%s)",
            self._config.sync.dry_run,
            self._config.sync.remove_missing,
        )

        all_stats: list[SyncStats] = []
        all_stats.extend(self._sync_simkl())

        if self._config.anilist.enabled and self._config.anilist.selected_statuses:
            all_stats.extend(self._sync_anilist())

        if self._config.trakt.enabled:
            all_stats.extend(self._sync_trakt())

        if self._config.mdblist.enabled:
            all_stats.extend(self._sync_mdblist())

        self._set_status("Finalizing sync results")
        self._log_results(all_stats)
        return all_stats

    def _sync_simkl(self) -> list[SyncStats]:
        """Sync all configured SIMKL lists."""
        media_types = list(self._config.sync.media_types)
        if self._config.anilist.enabled and self._config.anilist.selected_statuses:
            media_types = [media_type for media_type in media_types if media_type != "anime"]
            logger.info("AniList enabled, skipping SIMKL anime sync")

        stats: list[SyncStats] = []
        if not media_types:
            return stats

        for simkl_type in media_types:
            for status_key in self._config.simkl.selected_statuses.get(simkl_type, []):
                self._set_status(f"Fetching SIMKL {_STATUS_LABELS.get(status_key, status_key)} {simkl_type}")
                grouped = self._simkl.get_status(status_key, [simkl_type])
                items = grouped.get(simkl_type, [])
                name = _list_name("SIMKL", simkl_type, status_key)
                display_name = _display_status_name(simkl_type, status_key)
                description = (
                    f"Auto-synced '{_STATUS_LABELS.get(status_key, status_key)}' "
                    f"{_TYPE_LABELS.get(simkl_type, simkl_type)} from SIMKL"
                )
                stats.append(self._sync_list(items, name, description, display_name=display_name, source_name="SIMKL"))

        return stats

    def _sync_anilist(self) -> list[SyncStats]:
        """Sync configured AniList anime statuses."""
        if "anime" not in self._config.sync.media_types:
            return []

        from .anilist_client import AniListClient

        client = AniListClient(self._config.anilist)
        stats: list[SyncStats] = []

        for status_key in self._config.anilist.selected_statuses:
            self._set_status(f"Fetching AniList {_STATUS_LABELS.get(status_key, status_key)} anime")
            items = client.get_status(status_key)
            name = _list_name("AniList", "anime", status_key)
            display_name = _display_status_name("anime", status_key)
            description = f"Auto-synced '{_STATUS_LABELS.get(status_key, status_key)}' anime from AniList"
            stats.append(self._sync_list(items, name, description, display_name=display_name, source_name="AniList"))

        return stats

    def _sync_trakt(self) -> list[SyncStats]:
        """Sync configured Trakt watchlist and list sources."""
        stats: list[SyncStats] = []

        if self._config.trakt.sync_watchlist:
            self._set_status("Fetching Trakt watchlist")
            watchlist_items = self._trakt.get_watchlist()
            grouped = {
                "shows": [item for item in watchlist_items if item["media_type"] == "tv"],
                "movies": [item for item in watchlist_items if item["media_type"] == "movie"],
            }
            for media_type in self._config.sync.media_types:
                if media_type not in {"shows", "movies"}:
                    continue
                items = grouped.get(media_type, [])
                name = _list_name("Trakt", media_type, "watchlist")
                display_name = _display_status_name(media_type, "watchlist")
                description = f"Auto-synced Trakt watchlist {_TYPE_LABELS.get(media_type, media_type)}"
                stats.append(self._sync_list(items, name, description, display_name=display_name, source_name="Trakt"))

        selected_lists = self._dedupe_trakt_lists(self._config.trakt.selected_lists)
        selected_default_lists = [item for item in selected_lists if item.get("source") == "default"]
        selected_liked_lists = [item for item in selected_lists if item.get("source") == "liked"]
        selected_public_lists = [item for item in selected_lists if item.get("source") != "liked"]
        selected_public_lists = [item for item in selected_public_lists if item.get("source") != "default"]

        for trakt_list in selected_default_lists:
            self._set_status(f"Fetching Trakt catalog {trakt_list['name']}")
            items = self._filter_trakt_items(
                self._trakt.get_default_catalog(trakt_list.get("catalog_key") or trakt_list.get("slug", ""))
            )
            name = f"Trakt Default - {trakt_list['name']}"
            description = f"Auto-synced Trakt default catalog '{trakt_list['name']}'"
            stats.append(self._sync_list(items, name, description, display_name=trakt_list["name"], source_name="Trakt"))

        if self._config.trakt.sync_liked_lists:
            self._set_status("Fetching Trakt liked lists")
            for liked_list in self._trakt.get_liked_lists():
                items = self._filter_trakt_items(liked_list["items"])
                name = f"Trakt List - {liked_list['user']} - {liked_list['name']}"
                description = f"Auto-synced liked Trakt list '{liked_list['name']}'"
                stats.append(self._sync_list(items, name, description, display_name=liked_list["name"], source_name=f"Trakt by {liked_list['user']}"))
        else:
            for trakt_list in selected_liked_lists:
                self._set_status(f"Fetching Trakt list {trakt_list['name']}")
                items = self._filter_trakt_items(
                    self._trakt.get_list_items(trakt_list["user"], trakt_list["slug"])
                )
                name = f"Trakt List - {trakt_list['user']} - {trakt_list['name']}"
                description = f"Auto-synced Trakt list '{trakt_list['name']}' by {trakt_list['user']}"
                stats.append(self._sync_list(items, name, description, display_name=trakt_list["name"], source_name=f"Trakt by {trakt_list['user']}"))

        for trakt_list in selected_public_lists:
            self._set_status(f"Fetching Trakt list {trakt_list['name']}")
            items = self._filter_trakt_items(
                self._trakt.get_list_items(trakt_list["user"], trakt_list["slug"])
            )
            name = f"Trakt List - {trakt_list['user']} - {trakt_list['name']}"
            description = f"Auto-synced Trakt list '{trakt_list['name']}' by {trakt_list['user']}"
            stats.append(self._sync_list(items, name, description, display_name=trakt_list["name"], source_name=f"Trakt by {trakt_list['user']}"))

        return stats

    def _sync_mdblist(self) -> list[SyncStats]:
        """Sync selected MDBList lists."""
        stats: list[SyncStats] = []

        for mdblist in self._dedupe_mdblist_lists(self._config.mdblist.selected_lists):
            self._set_status(f"Fetching MDBList {mdblist['name']}")
            items = self._filter_mdblist_items(self._mdblist.get_list_items(mdblist["id"]))
            media_type = "movies" if mdblist["mediatype"] == "movie" else "shows"
            name = f"MDBList - {_TYPE_LABELS.get(media_type, media_type.title())} - {mdblist['name']}"
            description = f"Auto-synced MDBList '{mdblist['name']}'"
            stats.append(self._sync_list(items, name, description, display_name=mdblist["name"], source_name="MDBList"))

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
    ) -> SyncStats:
        """Sync a single source list to a PublicMetaDB list."""
        stats = SyncStats(
            list_name=list_name,
            display_name=display_name or list_name,
            source_name=source_name or "",
            items_fetched=len(source_items),
        )
        self._set_status(f"Processing {list_name}")
        logger.info("Syncing '%s': %d source items", list_name, len(source_items))

        if not source_items:
            logger.info("  No items for '%s', skipping", list_name)
            return stats

        self._set_status(f"Resolving IDs for {list_name}")
        resolved: list[dict] = []
        for item in source_items:
            tmdb_id = self._matcher.resolve_tmdb_id(item)
            if tmdb_id is not None:
                resolved.append({**item, "resolved_tmdb_id": tmdb_id})
                stats.items_resolved += 1
            else:
                stats.items_skipped_unresolved += 1

        if self._config.sync.dry_run:
            return self._dry_run_report(resolved, list_name, stats)

        try:
            self._set_status(f"Preparing PublicMetaDB list {list_name}")
            pmdb_list = self._pmdb.get_or_create_list(list_name, list_description)
        except Exception as exc:
            stats.errors.append(f"Failed to get/create list: {exc}")
            logger.error("Failed to get/create list '%s': %s", list_name, exc)
            return stats

        list_id = pmdb_list["id"]

        self._set_status(f"Loading existing items from {list_name}")
        existing_items = self._pmdb.get_list_items(list_id)
        existing_map = self._build_existing_map(existing_items)
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
                self._set_status(f"Adding items to {list_name}")
                self._pmdb.add_item_to_list(list_id, tmdb_id, media_type)
                stats.items_added += 1
            except Exception as exc:
                message = f"Failed to add '{item['title']}' (tmdb={tmdb_id}): {exc}"
                stats.errors.append(message)
                logger.error(message)

        if self._config.sync.remove_missing:
            self._set_status(f"Removing stale items from {list_name}")
            stats.items_removed = self._remove_stale(list_id, existing_items, desired_keys)

        return stats

    def _dry_run_report(self, resolved: list[dict], list_name: str, stats: SyncStats) -> SyncStats:
        logger.info("[DRY RUN] Would sync %d resolved items to '%s'", len(resolved), list_name)
        for item in resolved:
            logger.info(
                "  [DRY RUN] %s (year=%s, tmdb=%s, type=%s)",
                item["title"],
                item.get("year"),
                item["resolved_tmdb_id"],
                item["media_type"],
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
                except Exception as exc:
                    logger.error("Failed to remove item %s: %s", item.get("id"), exc)
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
                "'%s': fetched=%d resolved=%d added=%d removed=%d dup=%d unresolved=%d errors=%d",
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

    def _set_status(self, status: str) -> None:
        if self._status_callback:
            try:
                self._status_callback(status)
            except Exception:
                logger.debug("Status callback failed", exc_info=True)
