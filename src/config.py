"""Configuration management for SIMKL-to-PublicMetaDB sync."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SimklConfig:
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    base_url: str = "https://api.simkl.com"


@dataclass
class AniListConfig:
    username: str = ""
    access_token: str = ""  # Optional — only needed for private lists
    enabled: bool = False


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
    pmdb: PublicMetaDBConfig = field(default_factory=PublicMetaDBConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    state_file: str = "sync_state.json"


def load_config(config_path: str | None = None) -> AppConfig:
    """Load configuration from environment variables, optionally overlaid with a JSON config file."""
    cfg = AppConfig()

    # Environment variables (always take precedence)
    cfg.simkl.client_id = os.getenv("SIMKL_CLIENT_ID", "")
    cfg.simkl.client_secret = os.getenv("SIMKL_CLIENT_SECRET", "")
    cfg.simkl.access_token = os.getenv("SIMKL_ACCESS_TOKEN", "")
    cfg.anilist.username = os.getenv("ANILIST_USERNAME", "")
    cfg.anilist.access_token = os.getenv("ANILIST_ACCESS_TOKEN", "")
    # Auto-enable when username is set; explicit flag can override
    anilist_enabled_env = os.getenv("ANILIST_ENABLED", "")
    if anilist_enabled_env:
        cfg.anilist.enabled = anilist_enabled_env.lower() == "true"
    else:
        cfg.anilist.enabled = bool(cfg.anilist.username)
    cfg.pmdb.api_key = os.getenv("PMDB_API_KEY", "")
    cfg.sync.remove_missing = os.getenv("SYNC_REMOVE_MISSING", "false").lower() == "true"
    cfg.sync.dry_run = os.getenv("SYNC_DRY_RUN", "false").lower() == "true"

    interval = os.getenv("SYNC_INTERVAL_MINUTES", "0")
    cfg.sync.interval_minutes = int(interval) if interval.isdigit() else 0

    media_types_env = os.getenv("SYNC_MEDIA_TYPES", "")
    if media_types_env:
        cfg.sync.media_types = [t.strip() for t in media_types_env.split(",") if t.strip()]

    # Config file overlay
    if config_path:
        path = Path(config_path)
        if path.exists():
            with open(path) as f:
                data = json.load(f)
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

    anilist = data.get("anilist", {})
    if not cfg.anilist.username:
        cfg.anilist.username = anilist.get("username", "")
    if not cfg.anilist.access_token:
        cfg.anilist.access_token = anilist.get("access_token", "")
    if not os.getenv("ANILIST_ENABLED"):
        if "enabled" in anilist:
            cfg.anilist.enabled = anilist["enabled"]
        else:
            cfg.anilist.enabled = bool(cfg.anilist.username)

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
    """Return a list of configuration errors (empty if valid).

    Args:
        sources: Which sources to validate. None means all enabled.
                 Values: "simkl", "anilist".
    """
    errors = []
    check_simkl = sources is None or "simkl" in sources
    check_anilist = sources is None or "anilist" in sources

    if check_simkl:
        if not cfg.simkl.client_id:
            errors.append("SIMKL_CLIENT_ID is required")
        if not cfg.simkl.access_token:
            errors.append("SIMKL_ACCESS_TOKEN is required (run `python main.py auth` to authenticate)")

    if check_anilist and cfg.anilist.enabled:
        if not cfg.anilist.username:
            errors.append("ANILIST_USERNAME is required when AniList is enabled")

    if not cfg.pmdb.api_key:
        errors.append("PMDB_API_KEY is required")
    return errors
