"""Configuration management for SyncMeta."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


SIMKL_DEFAULT_SELECTED_STATUSES = {
    "shows": ["watching", "plantowatch"],
    "movies": ["plantowatch"],
    "anime": ["watching", "plantowatch"],
}

ANILIST_DEFAULT_SELECTED_STATUSES = ["CURRENT", "PLANNING"]


@dataclass
class SimklConfig:
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    selected_statuses: dict[str, list[str]] = field(
        default_factory=lambda: {key: list(values) for key, values in SIMKL_DEFAULT_SELECTED_STATUSES.items()}
    )
    base_url: str = "https://api.simkl.com"


@dataclass
class AniListConfig:
    username: str = ""
    access_token: str = ""  # Optional, only needed for private lists
    enabled: bool = False
    selected_statuses: list[str] = field(default_factory=lambda: list(ANILIST_DEFAULT_SELECTED_STATUSES))


@dataclass
class TraktConfig:
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    username: str = ""
    enabled: bool = False
    sync_watchlist: bool = True
    sync_liked_lists: bool = True
    selected_lists: list[dict] = field(default_factory=list)
    base_url: str = "https://api.trakt.tv"


@dataclass
class MdbListConfig:
    api_key: str = ""
    enabled: bool = False
    selected_lists: list[dict] = field(default_factory=list)
    base_url: str = "https://api.mdblist.com"


@dataclass
class PublicMetaDBConfig:
    api_key: str = ""
    base_url: str = "https://publicmetadb.com"


@dataclass
class SyncConfig:
    remove_missing: bool = False
    dry_run: bool = False
    interval_minutes: int = 0
    media_types: list[str] = field(default_factory=lambda: ["shows", "movies", "anime"])


@dataclass
class AppConfig:
    simkl: SimklConfig = field(default_factory=SimklConfig)
    anilist: AniListConfig = field(default_factory=AniListConfig)
    trakt: TraktConfig = field(default_factory=TraktConfig)
    mdblist: MdbListConfig = field(default_factory=MdbListConfig)
    pmdb: PublicMetaDBConfig = field(default_factory=PublicMetaDBConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    state_file: str = "sync_state.json"


def load_config(config_path: str | None = None) -> AppConfig:
    """Load configuration from environment variables, optionally overlaid with a JSON config file."""
    cfg = AppConfig()

    cfg.simkl.client_id = os.getenv("SIMKL_CLIENT_ID", "")
    cfg.simkl.client_secret = os.getenv("SIMKL_CLIENT_SECRET", "")
    cfg.simkl.access_token = os.getenv("SIMKL_ACCESS_TOKEN", "")

    cfg.anilist.username = os.getenv("ANILIST_USERNAME", "")
    cfg.anilist.access_token = os.getenv("ANILIST_ACCESS_TOKEN", "")

    cfg.trakt.client_id = os.getenv("TRAKT_CLIENT_ID", "")
    cfg.trakt.client_secret = os.getenv("TRAKT_CLIENT_SECRET", "")
    cfg.trakt.access_token = os.getenv("TRAKT_ACCESS_TOKEN", "")
    cfg.trakt.refresh_token = os.getenv("TRAKT_REFRESH_TOKEN", "")
    cfg.trakt.username = os.getenv("TRAKT_USERNAME", "")

    cfg.mdblist.api_key = os.getenv("MDBLIST_API_KEY", "")

    trakt_enabled_env = os.getenv("TRAKT_ENABLED", "")
    if trakt_enabled_env:
        cfg.trakt.enabled = trakt_enabled_env.lower() == "true"
    else:
        cfg.trakt.enabled = bool(cfg.trakt.client_id and cfg.trakt.access_token)
    cfg.trakt.sync_watchlist = os.getenv("TRAKT_SYNC_WATCHLIST", "true").lower() == "true"
    cfg.trakt.sync_liked_lists = os.getenv("TRAKT_SYNC_LIKED_LISTS", "true").lower() == "true"

    anilist_enabled_env = os.getenv("ANILIST_ENABLED", "")
    if anilist_enabled_env:
        cfg.anilist.enabled = anilist_enabled_env.lower() == "true"
    else:
        cfg.anilist.enabled = bool(cfg.anilist.username)

    mdblist_enabled_env = os.getenv("MDBLIST_ENABLED", "")
    if mdblist_enabled_env:
        cfg.mdblist.enabled = mdblist_enabled_env.lower() == "true"
    else:
        cfg.mdblist.enabled = bool(cfg.mdblist.api_key)

    cfg.pmdb.api_key = os.getenv("PMDB_API_KEY", "")
    cfg.sync.remove_missing = os.getenv("SYNC_REMOVE_MISSING", "false").lower() == "true"
    cfg.sync.dry_run = os.getenv("SYNC_DRY_RUN", "false").lower() == "true"

    interval = os.getenv("SYNC_INTERVAL_MINUTES", "0")
    cfg.sync.interval_minutes = int(interval) if interval.isdigit() else 0

    media_types_env = os.getenv("SYNC_MEDIA_TYPES", "")
    if media_types_env:
        cfg.sync.media_types = [t.strip() for t in media_types_env.split(",") if t.strip()]

    if config_path:
        path = Path(config_path)
        if path.exists():
            with open(path, encoding="utf-8") as file_handle:
                data = json.load(file_handle)
            _apply_config_file(cfg, data)

    return cfg


def _apply_config_file(cfg: AppConfig, data: dict) -> None:
    """Apply config file values only where env vars are not set."""
    simkl = data.get("simkl", {})
    if not cfg.simkl.client_id:
        cfg.simkl.client_id = simkl.get("client_id", "")
    if not cfg.simkl.client_secret:
        cfg.simkl.client_secret = simkl.get("client_secret", "")
    if not cfg.simkl.access_token:
        cfg.simkl.access_token = simkl.get("access_token", "")
    cfg.simkl.selected_statuses = simkl.get("selected_statuses", cfg.simkl.selected_statuses)

    anilist = data.get("anilist", {})
    if not cfg.anilist.username:
        cfg.anilist.username = anilist.get("username", "")
    if not cfg.anilist.access_token:
        cfg.anilist.access_token = anilist.get("access_token", "")
    cfg.anilist.selected_statuses = anilist.get("selected_statuses", cfg.anilist.selected_statuses)
    if not os.getenv("ANILIST_ENABLED"):
        if "enabled" in anilist:
            cfg.anilist.enabled = anilist["enabled"]
        else:
            cfg.anilist.enabled = bool(cfg.anilist.username)

    trakt = data.get("trakt", {})
    if not cfg.trakt.client_id:
        cfg.trakt.client_id = trakt.get("client_id", "")
    if not cfg.trakt.client_secret:
        cfg.trakt.client_secret = trakt.get("client_secret", "")
    if not cfg.trakt.access_token:
        cfg.trakt.access_token = trakt.get("access_token", "")
    if not cfg.trakt.refresh_token:
        cfg.trakt.refresh_token = trakt.get("refresh_token", "")
    if not cfg.trakt.username:
        cfg.trakt.username = trakt.get("username", "")
    if not os.getenv("TRAKT_ENABLED"):
        cfg.trakt.enabled = trakt.get("enabled", bool(cfg.trakt.client_id and cfg.trakt.access_token))
    if not os.getenv("TRAKT_SYNC_WATCHLIST"):
        cfg.trakt.sync_watchlist = trakt.get("sync_watchlist", True)
    if not os.getenv("TRAKT_SYNC_LIKED_LISTS"):
        cfg.trakt.sync_liked_lists = trakt.get("sync_liked_lists", True)
    cfg.trakt.selected_lists = trakt.get("selected_lists", [])

    mdblist = data.get("mdblist", {})
    if not cfg.mdblist.api_key:
        cfg.mdblist.api_key = mdblist.get("api_key", "")
    cfg.mdblist.selected_lists = mdblist.get("selected_lists", [])
    if not os.getenv("MDBLIST_ENABLED"):
        cfg.mdblist.enabled = mdblist.get("enabled", bool(cfg.mdblist.api_key and cfg.mdblist.selected_lists))

    pmdb = data.get("pmdb", {})
    if not cfg.pmdb.api_key:
        cfg.pmdb.api_key = pmdb.get("api_key", "")

    sync = data.get("sync", {})
    if "remove_missing" in sync and not os.getenv("SYNC_REMOVE_MISSING"):
        cfg.sync.remove_missing = sync["remove_missing"]
    if "dry_run" in sync and not os.getenv("SYNC_DRY_RUN"):
        cfg.sync.dry_run = sync["dry_run"]
    if "interval_minutes" in sync and not os.getenv("SYNC_INTERVAL_MINUTES"):
        cfg.sync.interval_minutes = sync["interval_minutes"]
    if "media_types" in sync and not os.getenv("SYNC_MEDIA_TYPES"):
        cfg.sync.media_types = sync["media_types"]
    if "state_file" in data:
        cfg.state_file = data["state_file"]


def validate_config(cfg: AppConfig, sources: list[str] | None = None) -> list[str]:
    """Return a list of configuration errors (empty if valid)."""
    errors = []
    check_simkl = sources is None or "simkl" in sources
    check_anilist = sources is None or "anilist" in sources
    check_trakt = sources is None or "trakt" in sources
    check_mdblist = sources is None or "mdblist" in sources

    if check_simkl:
        if not cfg.simkl.client_id:
            errors.append("SIMKL_CLIENT_ID is required")
        if not cfg.simkl.access_token:
            errors.append("SIMKL_ACCESS_TOKEN is required (run `python main.py auth` to authenticate)")
        if not any(cfg.simkl.selected_statuses.get(media_type) for media_type in ["shows", "movies", "anime"]):
            errors.append("Select at least one SIMKL status to sync")

    if check_anilist and cfg.anilist.enabled:
        if not cfg.anilist.username:
            errors.append("ANILIST_USERNAME is required when AniList is enabled")
        if not cfg.anilist.selected_statuses:
            errors.append("Select at least one AniList status to sync")

    if check_trakt and cfg.trakt.enabled:
        if not cfg.trakt.client_id:
            errors.append("TRAKT_CLIENT_ID is required when Trakt is enabled")
        if not cfg.trakt.client_secret:
            errors.append("TRAKT_CLIENT_SECRET is required when Trakt is enabled")
        if not cfg.trakt.access_token:
            errors.append("TRAKT_ACCESS_TOKEN is required when Trakt is enabled")
        if (
            not cfg.trakt.sync_watchlist
            and not cfg.trakt.sync_liked_lists
            and not cfg.trakt.selected_lists
        ):
            errors.append("Enable at least one Trakt source: watchlist, liked lists, or selected public lists")

    if check_mdblist and cfg.mdblist.enabled:
        if not cfg.mdblist.api_key:
            errors.append("MDBLIST_API_KEY is required when MDBList is enabled")
        if not cfg.mdblist.selected_lists:
            errors.append("Select at least one MDBList list to sync")

    if not cfg.pmdb.api_key:
        errors.append("PMDB_API_KEY is required")
    return errors
