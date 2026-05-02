"""Persistent profile storage for the web dashboard."""

from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from werkzeug.security import check_password_hash, generate_password_hash

from .config import ANILIST_DEFAULT_SELECTED_STATUSES, SIMKL_DEFAULT_SELECTED_STATUSES

ALLOWED_MEDIA_TYPES = {"shows", "movies", "anime"}
DEFAULT_MEDIA_TYPES = ["shows", "movies", "anime"]
DEFAULT_SYNC_INTERVAL_SECONDS = 43200  # 12 hours
MIN_SYNC_INTERVAL_SECONDS = 600
DEFAULT_RESUME_SYNC_INTERVAL_SECONDS = 600
DEFAULT_WATCHED_HISTORY_INTERVAL_SECONDS = 86400
MIN_WATCHED_HISTORY_INTERVAL_SECONDS = 86400
MAX_HISTORY_ITEMS = 20
SIMKL_ALLOWED_STATUSES = {"watching", "plantowatch", "completed", "hold", "dropped"}
ANILIST_ALLOWED_STATUSES = {"CURRENT", "PLANNING", "COMPLETED", "PAUSED", "DROPPED", "COMPLETED_ONA", "COMPLETED_OVA", "COMPLETED_MOVIE"}
ALLOWED_VISIBILITIES = {"private", "public"}
ALLOWED_ACTIVITY_SOURCES = {"off", "simkl", "trakt"}
DEFAULT_KEY_FILE_NAME = "profiles.key"

ACTIVITY_RESULT_NAMES = {
    "Watch History": "watch_history",
    "Resume Progress": "resume_progress",
    "Trakt Watch History": "watch_history",
    "Trakt Resume Progress": "resume_progress",
    "SIMKL Watch History": "watch_history",
    "SIMKL Resume Progress": "resume_progress",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalize_activity_results(raw_results: dict | None) -> dict:
    if not isinstance(raw_results, dict):
        return {}
    normalized = {}
    for key in {"watch_history", "resume_progress"}:
        value = raw_results.get(key)
        if isinstance(value, dict):
            normalized[key] = copy.deepcopy(value)
    return normalized


def _normalize_sync_job_snapshot(raw_snapshot: dict | None) -> dict:
    if not isinstance(raw_snapshot, dict):
        return {
            "job_id": "",
            "phase": "idle",
            "provider": "",
            "current_list": "",
            "started_at": None,
            "updated_at": None,
            "stopped_at": None,
            "results": [],
            "totals": {},
        }
    return {
        "job_id": str(raw_snapshot.get("job_id", "") or "").strip(),
        "phase": str(raw_snapshot.get("phase", "idle") or "idle").strip(),
        "provider": str(raw_snapshot.get("provider", "") or "").strip(),
        "current_list": str(raw_snapshot.get("current_list", "") or "").strip(),
        "started_at": raw_snapshot.get("started_at"),
        "updated_at": raw_snapshot.get("updated_at"),
        "stopped_at": raw_snapshot.get("stopped_at"),
        "results": copy.deepcopy(raw_snapshot.get("results", [])) if isinstance(raw_snapshot.get("results"), list) else [],
        "totals": copy.deepcopy(raw_snapshot.get("totals", {})) if isinstance(raw_snapshot.get("totals"), dict) else {},
    }


def _normalize_activity_state(raw_state: dict | None) -> dict:
    if not isinstance(raw_state, dict):
        return {
            "simkl_history_cursor": "",
            "trakt_history_cursor": "",
            "simkl_activities_ts": "",
            "trakt_activities_ts": "",
        }
    return {
        "simkl_history_cursor": str(raw_state.get("simkl_history_cursor", "") or "").strip(),
        "trakt_history_cursor": str(raw_state.get("trakt_history_cursor", "") or "").strip(),
        "simkl_activities_ts": str(raw_state.get("simkl_activities_ts", "") or "").strip(),
        "trakt_activities_ts": str(raw_state.get("trakt_activities_ts", "") or "").strip(),
    }


def _result_totals(rows: list[dict] | None) -> dict[str, int]:
    totals = {
        "lists": 0,
        "fetched": 0,
        "resolved": 0,
        "added": 0,
        "removed": 0,
        "duplicates": 0,
        "unresolved": 0,
        "errors": 0,
    }
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("list_name", "")).strip():
            totals["lists"] += 1
        totals["fetched"] += int(row.get("items_fetched") or 0)
        totals["resolved"] += int(row.get("items_resolved") or 0)
        totals["added"] += int(row.get("items_added") or 0)
        totals["removed"] += int(row.get("items_removed") or 0)
        totals["duplicates"] += int(row.get("items_skipped_duplicate") or 0)
        totals["unresolved"] += int(row.get("items_skipped_unresolved") or 0)
        totals["errors"] += int(row.get("error_count") or 0)
    return totals


def _configured_sources_for_profile(profile: dict) -> list[str]:
    credentials = normalize_credentials(profile.get("credentials"))
    options = normalize_profile_options(profile.get("options"))
    sources: list[str] = []

    if (
        credentials["simkl"]["client_id"]
        and credentials["simkl"]["access_token"]
        and (
            any(credentials["simkl"]["selected_statuses"].get(media_type) for media_type in ["shows", "movies", "anime"])
            or options["activity_history_source"] == "simkl"
        )
    ):
        sources.append("simkl")

    if credentials["anilist"]["username"] and credentials["anilist"]["selected_statuses"]:
        sources.append("anilist")

    if (
        credentials["trakt"]["client_id"]
        and credentials["trakt"]["access_token"]
        and (
            credentials["trakt"]["sync_watchlist_movies"]
            or credentials["trakt"]["sync_watchlist_shows"]
            or credentials["trakt"]["sync_liked_lists"]
            or credentials["trakt"]["selected_lists"]
            or options["activity_history_source"] == "trakt"
            or options["activity_resume_source"] == "trakt"
        )
    ):
        sources.append("trakt")

    if credentials["mdblist"]["api_key"] and credentials["mdblist"]["selected_lists"]:
        sources.append("mdblist")

    return sources


class CredentialCipher:
    """Encrypts stored source credentials with a Fernet key."""

    def __init__(self, storage_dir: str | Path):
        self._storage_dir = Path(storage_dir)
        self._fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        env_key = str(os.getenv("SYNCMETA_MASTER_KEY", "")).strip()
        if env_key:
            return env_key.encode("utf-8")

        key_file = Path(os.getenv("SYNCMETA_MASTER_KEY_FILE", self._storage_dir / DEFAULT_KEY_FILE_NAME))
        if key_file.exists():
            return key_file.read_text(encoding="utf-8").strip().encode("utf-8")

        key_file.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        key_file.write_text(key.decode("utf-8"), encoding="utf-8")
        return key

    def encrypt(self, payload: dict) -> str:
        serialized = json.dumps(payload, sort_keys=True).encode("utf-8")
        return self._fernet.encrypt(serialized).decode("utf-8")

    def decrypt(self, token: str) -> dict:
        try:
            data = self._fernet.decrypt(token.encode("utf-8"))
        except InvalidToken as exc:
            raise ValueError("Stored credentials could not be decrypted") from exc
        return json.loads(data.decode("utf-8"))


def normalize_credentials(credentials: dict | None) -> dict:
    raw = credentials or {}
    simkl = raw.get("simkl", {})
    anilist = raw.get("anilist", {})
    trakt = raw.get("trakt", {})
    mdblist = raw.get("mdblist", {})
    pmdb = raw.get("pmdb", {})
    trakt_default_catalogs_initialized = bool(trakt.get("default_catalogs_initialized", False))
    trakt_selected_lists = _normalize_trakt_selected_lists(trakt.get("selected_lists", []))
    legacy_sync_watchlist = bool(trakt.get("sync_watchlist", False))
    if not trakt_default_catalogs_initialized:
        trakt_selected_lists = [item for item in trakt_selected_lists if item.get("source") != "default"]
    return {
        "simkl": {
            "client_id": str(simkl.get("client_id", "")).strip(),
            "client_secret": str(simkl.get("client_secret", "")).strip(),
            "access_token": str(simkl.get("access_token", "")).strip(),
            "selected_statuses": _normalize_simkl_selected_statuses(simkl.get("selected_statuses")),
        },
        "anilist": {
            "username": str(anilist.get("username", "")).strip(),
            "access_token": str(anilist.get("access_token", "")).strip(),
            "selected_statuses": _normalize_anilist_selected_statuses(anilist.get("selected_statuses")),
        },
        "trakt": {
            "client_id": str(trakt.get("client_id", "")).strip(),
            "client_secret": str(trakt.get("client_secret", "")).strip(),
            "access_token": str(trakt.get("access_token", "")).strip(),
            "refresh_token": str(trakt.get("refresh_token", "")).strip(),
            "username": str(trakt.get("username", "")).strip(),
            "sync_watchlist": legacy_sync_watchlist,
            "sync_watchlist_movies": bool(trakt.get("sync_watchlist_movies", legacy_sync_watchlist)),
            "sync_watchlist_shows": bool(trakt.get("sync_watchlist_shows", legacy_sync_watchlist)),
            "sync_liked_lists": bool(trakt.get("sync_liked_lists", True)),
            "default_catalogs_initialized": trakt_default_catalogs_initialized,
            "selected_lists": trakt_selected_lists,
        },
        "mdblist": {
            "api_key": str(mdblist.get("api_key", "")).strip(),
            "selected_lists": _normalize_mdblist_selected_lists(mdblist.get("selected_lists", [])),
        },
        "pmdb": {
            "api_key": str(pmdb.get("api_key", "")).strip(),
        },
    }


def public_credentials(credentials: dict | None) -> dict:
    raw = normalize_credentials(credentials)
    return {
        "simkl": {
            "client_id": raw["simkl"]["client_id"],
            "client_secret_saved": bool(raw["simkl"]["client_secret"]),
            "access_token_saved": bool(raw["simkl"]["access_token"]),
            "selected_statuses": copy.deepcopy(raw["simkl"]["selected_statuses"]),
        },
        "anilist": {
            "username": raw["anilist"]["username"],
            "access_token_saved": bool(raw["anilist"]["access_token"]),
            "selected_statuses": list(raw["anilist"]["selected_statuses"]),
        },
        "trakt": {
            "client_id": raw["trakt"]["client_id"],
            "client_secret_saved": bool(raw["trakt"]["client_secret"]),
            "access_token_saved": bool(raw["trakt"]["access_token"]),
            "refresh_token_saved": bool(raw["trakt"]["refresh_token"]),
            "username": raw["trakt"]["username"],
            "sync_watchlist": raw["trakt"]["sync_watchlist"],
            "sync_watchlist_movies": raw["trakt"]["sync_watchlist_movies"],
            "sync_watchlist_shows": raw["trakt"]["sync_watchlist_shows"],
            "sync_liked_lists": raw["trakt"]["sync_liked_lists"],
            "default_catalogs_initialized": raw["trakt"]["default_catalogs_initialized"],
            "selected_lists": copy.deepcopy(raw["trakt"]["selected_lists"]),
        },
        "mdblist": {
            "api_key_saved": bool(raw["mdblist"]["api_key"]),
            "selected_lists": copy.deepcopy(raw["mdblist"]["selected_lists"]),
        },
        "pmdb": {
            "api_key_saved": bool(raw["pmdb"]["api_key"]),
        },
    }


def _normalize_simkl_selected_statuses(raw_statuses: dict | None) -> dict[str, list[str]]:
    incoming = raw_statuses if isinstance(raw_statuses, dict) else {}
    normalized: dict[str, list[str]] = {}
    for media_type, defaults in SIMKL_DEFAULT_SELECTED_STATUSES.items():
        values = incoming.get(media_type, defaults)
        if not isinstance(values, list):
            values = defaults
        deduped: list[str] = []
        for status in values:
            candidate = str(status).strip().lower()
            if candidate in SIMKL_ALLOWED_STATUSES and candidate not in deduped:
                deduped.append(candidate)
        normalized[media_type] = deduped
    return normalized


def _normalize_anilist_selected_statuses(raw_statuses: list | None) -> list[str]:
    values = raw_statuses if isinstance(raw_statuses, list) else ANILIST_DEFAULT_SELECTED_STATUSES
    normalized: list[str] = []
    for status in values:
        candidate = str(status).strip().upper()
        if candidate in ANILIST_ALLOWED_STATUSES and candidate not in normalized:
            normalized.append(candidate)
    return normalized


def _normalize_trakt_selected_lists(raw_lists: list | None) -> list[dict]:
    if not isinstance(raw_lists, list):
        return []

    selected: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_lists:
        if not isinstance(item, dict):
            continue
        user = str(item.get("user", "")).strip()
        slug = str(item.get("slug", "")).strip()
        name = str(item.get("name", "")).strip()
        if not user or not slug or not name:
            continue
        key = (user.lower(), slug.lower())
        if key in seen:
            continue
        seen.add(key)
        try:
            likes = int(item.get("likes", 0) or 0)
        except (TypeError, ValueError):
            likes = 0
        try:
            item_count = int(item.get("item_count", 0) or 0)
        except (TypeError, ValueError):
            item_count = 0
        source = str(item.get("source", "liked")).strip().lower()
        if source not in {"liked", "discover", "default", "personal"}:
            source = "liked"
        selected.append({
            "name": name,
            "description": str(item.get("description", "")).strip(),
            "user": user,
            "slug": slug,
            "trakt_id": item.get("trakt_id"),
            "item_count": item_count,
            "likes": likes,
            "share_link": str(item.get("share_link", "")).strip(),
            "source": source,
            "catalog_key": str(item.get("catalog_key", "")).strip(),
        })
    return selected


def _normalize_mdblist_selected_lists(raw_lists: list | None) -> list[dict]:
    if not isinstance(raw_lists, list):
        return []

    selected: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for item in raw_lists:
        if not isinstance(item, dict):
            continue
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
        try:
            items = int(item.get("items", 0) or 0)
        except (TypeError, ValueError):
            items = 0
        try:
            likes = int(item.get("likes", 0) or 0)
        except (TypeError, ValueError):
            likes = 0
        selected.append({
            "id": list_id,
            "name": name,
            "slug": str(item.get("slug", "")).strip(),
            "user_name": str(item.get("user_name", "")).strip(),
            "description": str(item.get("description", "")).strip(),
            "mediatype": mediatype,
            "items": items,
            "likes": likes,
            "type": str(item.get("type", "")).strip(),
            "private": bool(item.get("private", False)),
        })
    return selected


def _normalize_managed_lists(raw_lists: list | None) -> list[dict]:
    if not isinstance(raw_lists, list):
        return []

    selected: list[dict] = []
    seen: set[str] = set()
    for item in raw_lists:
        if not isinstance(item, dict):
            continue
        list_name = str(item.get("list_name", "")).strip()
        if not list_name or list_name in seen:
            continue
        seen.add(list_name)
        selected.append({
            "list_name": list_name,
            "list_id": str(item.get("list_id", "")).strip(),
            "display_name": str(item.get("display_name", "")).strip(),
            "source_name": str(item.get("source_name", "")).strip(),
            "selection": dict(item.get("selection", {})) if isinstance(item.get("selection"), dict) else {},
        })
    return selected


def merge_credentials(existing: dict | None, updates: dict | None) -> dict:
    current = normalize_credentials(existing)
    incoming = normalize_credentials(updates)

    def keep_secret(section: str, key: str) -> str:
        value = incoming[section][key]
        return value if value else current[section][key]

    return {
        "simkl": {
            "client_id": incoming["simkl"]["client_id"],
            "client_secret": keep_secret("simkl", "client_secret"),
            "access_token": keep_secret("simkl", "access_token"),
            "selected_statuses": incoming["simkl"]["selected_statuses"],
        },
        "anilist": {
            "username": incoming["anilist"]["username"],
            "access_token": keep_secret("anilist", "access_token"),
            "selected_statuses": incoming["anilist"]["selected_statuses"],
        },
        "trakt": {
            "client_id": incoming["trakt"]["client_id"],
            "client_secret": keep_secret("trakt", "client_secret"),
            "access_token": keep_secret("trakt", "access_token"),
            "refresh_token": keep_secret("trakt", "refresh_token"),
            "username": incoming["trakt"]["username"],
            "sync_watchlist": incoming["trakt"]["sync_watchlist"],
            "sync_watchlist_movies": incoming["trakt"]["sync_watchlist_movies"],
            "sync_watchlist_shows": incoming["trakt"]["sync_watchlist_shows"],
            "sync_liked_lists": incoming["trakt"]["sync_liked_lists"],
            "default_catalogs_initialized": incoming["trakt"]["default_catalogs_initialized"],
            "selected_lists": incoming["trakt"]["selected_lists"],
        },
        "mdblist": {
            "api_key": keep_secret("mdblist", "api_key"),
            "selected_lists": incoming["mdblist"]["selected_lists"],
        },
        "pmdb": {
            "api_key": keep_secret("pmdb", "api_key"),
        },
    }


def _normalize_visibility(value: object, default: str) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in ALLOWED_VISIBILITIES else default


def _normalize_activity_source(value: object, simkl_enabled: bool, trakt_enabled: bool) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in ALLOWED_ACTIVITY_SOURCES:
        return candidate
    if simkl_enabled:
        return "simkl"
    if trakt_enabled:
        return "trakt"
    return "off"


def _normalize_resume_source(value: object, trakt_enabled: bool) -> str:
    candidate = str(value or "").strip().lower()
    if candidate == "trakt":
        return "trakt"
    if trakt_enabled:
        return "trakt"
    return "off"


def normalize_profile_options(options: dict | None) -> dict:
    raw = options or {}
    interval_raw = raw.get("interval_seconds", DEFAULT_SYNC_INTERVAL_SECONDS)
    try:
        interval_seconds = int(interval_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Sync interval must be a whole number of seconds") from exc

    if interval_seconds < MIN_SYNC_INTERVAL_SECONDS:
        interval_seconds = MIN_SYNC_INTERVAL_SECONDS

    watched_interval_raw = raw.get("trakt_watched_history_interval_seconds", DEFAULT_WATCHED_HISTORY_INTERVAL_SECONDS)
    try:
        watched_history_interval_seconds = int(watched_interval_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Trakt watched history interval must be a whole number of seconds") from exc

    if watched_history_interval_seconds < MIN_WATCHED_HISTORY_INTERVAL_SECONDS:
        watched_history_interval_seconds = MIN_WATCHED_HISTORY_INTERVAL_SECONDS

    requested_types = raw.get("media_types", DEFAULT_MEDIA_TYPES)
    if not isinstance(requested_types, list):
        raise ValueError("Media types must be a list")

    media_types = []
    for media_type in requested_types:
        normalized = str(media_type).strip().lower()
        if normalized in ALLOWED_MEDIA_TYPES and normalized not in media_types:
            media_types.append(normalized)

    if not media_types:
        raise ValueError("Select at least one media type to sync")

    history_source = _normalize_activity_source(
        raw.get("activity_history_source"),
        bool(raw.get("simkl_sync_watched_history", False)),
        bool(raw.get("trakt_sync_watched_history", False)),
    )
    resume_source = _normalize_resume_source(
        raw.get("activity_resume_source"),
        bool(raw.get("trakt_sync_resume_progress", False)),
    )

    return {
        "remove_missing": bool(raw.get("remove_missing", False)),
        "delete_disabled_lists": bool(raw.get("delete_disabled_lists", False)),
        "media_types": media_types,
        "auto_sync": bool(raw.get("auto_sync", True)),
        "interval_seconds": interval_seconds,
        "activity_history_source": history_source,
        "activity_resume_source": resume_source,
        "auto_history_sync": bool(raw.get("auto_history_sync", False)),
        "simkl_sync_watched_history": history_source == "simkl",
        "simkl_history_anime_only": bool(raw.get("simkl_history_anime_only", False)),
        "simkl_sync_resume_progress": resume_source == "simkl",
        "simkl_resume_use_next_up_fallback": bool(raw.get("simkl_resume_use_next_up_fallback", False)),
        "trakt_sync_watched_history": history_source == "trakt",
        "trakt_watched_history_interval_seconds": watched_history_interval_seconds,
        "trakt_sync_full_watch_counts": False,
        "trakt_reconcile_watched_history": False,
        "trakt_sync_resume_progress": resume_source == "trakt",
        "simkl_visibility": _normalize_visibility(raw.get("simkl_visibility"), "private"),
        "anilist_visibility": _normalize_visibility(raw.get("anilist_visibility"), "private"),
        "trakt_personal_visibility": _normalize_visibility(raw.get("trakt_personal_visibility"), "private"),
        "trakt_public_visibility": _normalize_visibility(raw.get("trakt_public_visibility"), "public"),
        "mdblist_visibility": _normalize_visibility(raw.get("mdblist_visibility"), "public"),
        "simkl_sync_to_pmdb_watchlist": bool(raw.get("simkl_sync_to_pmdb_watchlist", False)),
        "trakt_sync_to_pmdb_watchlist": bool(raw.get("trakt_sync_to_pmdb_watchlist", False)),
        "anilist_sync_to_pmdb_watchlist": bool(raw.get("anilist_sync_to_pmdb_watchlist", False)),
    }


class ProfileStore:
    """JSON-backed profile storage with password authentication."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._cipher = CredentialCipher(self._path.parent)
        self._lock = threading.RLock()
        self._profiles: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        with self._lock:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
            else:
                data = {"profiles": {}}

            profiles: dict[str, dict] = {}
            changed = False
            for profile_id, raw_profile in data.get("profiles", {}).items():
                if not raw_profile.get("password_hash"):
                    continue
                try:
                    normalized_id = self._normalize_profile_id(profile_id)
                except ValueError:
                    continue
                hydrated = self._hydrate_profile(normalized_id, raw_profile)
                profiles[normalized_id] = hydrated
                if "credentials" in raw_profile and "credentials_encrypted" not in raw_profile:
                    changed = True
                # Persist rescheduled next_sync_at so restarts don't re-trigger immediately
                if hydrated.get("next_sync_at") != raw_profile.get("next_sync_at"):
                    changed = True

            self._profiles = profiles
            if changed:
                self._save_locked()

    def _hydrate_profile(self, profile_id: str, raw_profile: dict) -> dict:
        created_at = raw_profile.get("created_at") or utc_now_iso()
        options = normalize_profile_options(raw_profile.get("options"))
        next_sync_at = raw_profile.get("next_sync_at")
        if not options["auto_sync"]:
            next_sync_at = None
        elif not next_sync_at or (parse_iso_datetime(next_sync_at) or utc_now()) <= utc_now():
            # Missing or past-due: schedule from now + interval instead of firing immediately
            next_sync_at = (utc_now() + timedelta(seconds=options["interval_seconds"])).isoformat()
        next_resume_sync_at = raw_profile.get("next_resume_sync_at")
        if not options["auto_sync"] or options["activity_resume_source"] == "off":
            next_resume_sync_at = None
        elif not next_resume_sync_at or (parse_iso_datetime(next_resume_sync_at) or utc_now()) <= utc_now():
            next_resume_sync_at = (utc_now() + timedelta(seconds=DEFAULT_RESUME_SYNC_INTERVAL_SECONDS)).isoformat()
        next_history_sync_at = raw_profile.get("next_history_sync_at")
        if not options.get("auto_history_sync", False) or options["activity_history_source"] == "off":
            next_history_sync_at = None
        elif not next_history_sync_at or (parse_iso_datetime(next_history_sync_at) or utc_now()) <= utc_now():
            next_history_sync_at = (utc_now() + timedelta(seconds=options["trakt_watched_history_interval_seconds"])).isoformat()

        return {
            "profile_id": profile_id,
            "password_hash": raw_profile["password_hash"],
            "credentials": self._load_credentials(raw_profile),
            "options": options,
            "created_at": created_at,
            "updated_at": raw_profile.get("updated_at") or created_at,
            "last_sync": raw_profile.get("last_sync"),
            "last_results": list(raw_profile.get("last_results", [])),
            "sync_live_results": list(raw_profile.get("sync_live_results", [])),
            "sync_running": False,
            "sync_cancel_requested": False,
            "sync_error": raw_profile.get("sync_error"),
            "sync_status": raw_profile.get("sync_status") or "Idle",
            "sync_started_at": raw_profile.get("sync_started_at"),
            "sync_updated_at": raw_profile.get("sync_updated_at"),
            "history": list(raw_profile.get("history", []))[:MAX_HISTORY_ITEMS],
            "next_sync_at": next_sync_at,
            "last_history_sync": raw_profile.get("last_history_sync"),
            "next_history_sync_at": next_history_sync_at,
            "last_resume_sync": raw_profile.get("last_resume_sync"),
            "next_resume_sync_at": next_resume_sync_at,
            "activity_results": _normalize_activity_results(raw_profile.get("activity_results")),
            "activity_state": _normalize_activity_state(raw_profile.get("activity_state")),
            "managed_lists": _normalize_managed_lists(raw_profile.get("managed_lists", [])),
            "anime_manual_overrides": copy.deepcopy(raw_profile.get("anime_manual_overrides", {})) if isinstance(raw_profile.get("anime_manual_overrides"), dict) else {},
            "anime_review_decisions": copy.deepcopy(raw_profile.get("anime_review_decisions", {})) if isinstance(raw_profile.get("anime_review_decisions"), dict) else {},
            "last_sync_job_snapshot": _normalize_sync_job_snapshot(raw_profile.get("last_sync_job_snapshot")),
            "sync_job_id": str(raw_profile.get("sync_job_id", "") or "").strip(),
            "unresolved_items": copy.deepcopy(raw_profile.get("unresolved_items", [])) if isinstance(raw_profile.get("unresolved_items"), list) else [],
            "resolution_cache": copy.deepcopy(raw_profile.get("resolution_cache", {})) if isinstance(raw_profile.get("resolution_cache"), dict) else {},
            "failed_resolution_cache": copy.deepcopy(raw_profile.get("failed_resolution_cache", {})) if isinstance(raw_profile.get("failed_resolution_cache"), dict) else {},
            "manual_resolution_cache": copy.deepcopy(raw_profile.get("manual_resolution_cache", {})) if isinstance(raw_profile.get("manual_resolution_cache"), dict) else {},
            "manual_list_additions": copy.deepcopy(raw_profile.get("manual_list_additions", {})) if isinstance(raw_profile.get("manual_list_additions"), dict) else {},
        }

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "profiles": {
                profile_id: self._serialize_profile(profile)
                for profile_id, profile in self._profiles.items()
            }
        }
        tmp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self._path)

    def _serialize_profile(self, profile: dict) -> dict:
        return {
            "password_hash": profile["password_hash"],
            "credentials_encrypted": self._cipher.encrypt(profile["credentials"]),
            "options": copy.deepcopy(profile["options"]),
            "created_at": profile.get("created_at"),
            "updated_at": profile.get("updated_at"),
            "last_sync": profile.get("last_sync"),
            "last_results": copy.deepcopy(profile.get("last_results", [])),
            "sync_live_results": copy.deepcopy(profile.get("sync_live_results", [])),
            "sync_error": profile.get("sync_error"),
            "sync_status": profile.get("sync_status"),
            "sync_started_at": profile.get("sync_started_at"),
            "sync_updated_at": profile.get("sync_updated_at"),
            "sync_cancel_requested": bool(profile.get("sync_cancel_requested")),
            "history": copy.deepcopy(profile.get("history", []))[:MAX_HISTORY_ITEMS],
            "next_sync_at": profile.get("next_sync_at"),
            "last_history_sync": profile.get("last_history_sync"),
            "next_history_sync_at": profile.get("next_history_sync_at"),
            "last_resume_sync": profile.get("last_resume_sync"),
            "next_resume_sync_at": profile.get("next_resume_sync_at"),
            "activity_results": copy.deepcopy(profile.get("activity_results", {})),
            "activity_state": copy.deepcopy(profile.get("activity_state", {})),
            "managed_lists": copy.deepcopy(profile.get("managed_lists", [])),
            "anime_manual_overrides": copy.deepcopy(profile.get("anime_manual_overrides", {})),
            "anime_review_decisions": copy.deepcopy(profile.get("anime_review_decisions", {})),
            "last_sync_job_snapshot": copy.deepcopy(profile.get("last_sync_job_snapshot", {})),
            "sync_job_id": profile.get("sync_job_id"),
            "unresolved_items": copy.deepcopy(profile.get("unresolved_items", [])),
            "resolution_cache": copy.deepcopy(profile.get("resolution_cache", {})),
            "failed_resolution_cache": copy.deepcopy(profile.get("failed_resolution_cache", {})),
            "manual_resolution_cache": copy.deepcopy(profile.get("manual_resolution_cache", {})),
            "manual_list_additions": copy.deepcopy(profile.get("manual_list_additions", {})),
        }

    def _load_credentials(self, raw_profile: dict) -> dict:
        encrypted = str(raw_profile.get("credentials_encrypted", "")).strip()
        if encrypted:
            return normalize_credentials(self._cipher.decrypt(encrypted))
        return normalize_credentials(raw_profile.get("credentials"))

    @staticmethod
    def _normalize_profile_id(profile_id: str) -> str:
        try:
            return str(uuid.UUID(str(profile_id).strip()))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("Invalid profile UUID") from exc

    @staticmethod
    def _next_sync_iso(interval_seconds: int) -> str:
        return (utc_now() + timedelta(seconds=interval_seconds)).isoformat()

    @staticmethod
    def _next_resume_sync_iso() -> str:
        return (utc_now() + timedelta(seconds=DEFAULT_RESUME_SYNC_INTERVAL_SECONDS)).isoformat()

    @staticmethod
    def _next_history_sync_iso(interval_seconds: int) -> str:
        return (utc_now() + timedelta(seconds=interval_seconds)).isoformat()

    @staticmethod
    def _normalize_sync_modes(sync_modes: dict | None) -> dict[str, bool]:
        raw = sync_modes or {}
        return {
            "lists": bool(raw.get("lists", True)),
            "history": bool(raw.get("history", False)),
            "resume": bool(raw.get("resume", False)),
        }

    def _apply_next_run_schedule(self, profile: dict, sync_modes: dict[str, bool], dry_run: bool) -> None:
        if dry_run:
            return
        options = profile.get("options", {})
        auto_sync = bool(options.get("auto_sync", True))
        auto_history_sync = bool(options.get("auto_history_sync", False))
        if sync_modes["lists"]:
            profile["last_sync"] = utc_now_iso()
            profile["next_sync_at"] = self._next_sync_iso(options["interval_seconds"]) if auto_sync else None
        if sync_modes["history"]:
            profile["last_history_sync"] = utc_now_iso()
            if auto_history_sync and options.get("activity_history_source") != "off":
                profile["next_history_sync_at"] = self._next_history_sync_iso(
                    options["trakt_watched_history_interval_seconds"]
                )
            else:
                profile["next_history_sync_at"] = None
        if sync_modes["resume"]:
            profile["last_resume_sync"] = utc_now_iso()
            if auto_sync and options.get("activity_resume_source") != "off":
                profile["next_resume_sync_at"] = self._next_resume_sync_iso()
            else:
                profile["next_resume_sync_at"] = None

    @staticmethod
    def _append_history_entry(
        profile: dict,
        timestamp: str,
        dry_run: bool,
        results: list[dict] | None = None,
        status: str = "completed",
        error_message: str = "",
    ) -> None:
        # Drop the dry_run_preview items from history — it's only useful on the
        # most recent last_results, and keeping it in N history entries bloats
        # profiles.json without adding value.
        trimmed_results = []
        for row in results or []:
            if isinstance(row, dict) and "dry_run_preview" in row:
                row = {k: v for k, v in row.items() if k != "dry_run_preview"}
            trimmed_results.append(copy.deepcopy(row))
        history_entry = {
            "timestamp": timestamp,
            "dry_run": dry_run,
            "results": trimmed_results,
            "status": str(status or "completed"),
        }
        if error_message:
            history_entry["error_message"] = str(error_message)
        profile["history"].insert(0, history_entry)
        profile["history"] = profile["history"][:MAX_HISTORY_ITEMS]

    @staticmethod
    def _patch_resolved_stats(profile: dict, resolved_item: dict) -> None:
        """Decrement items_skipped_unresolved and increment items_resolved/items_added
        in the matching last_results row so the dashboard stats update immediately."""
        list_name = str(resolved_item.get("list_name") or "").strip()
        for row in profile.get("last_results", []):
            if not isinstance(row, dict):
                continue
            if list_name and str(row.get("list_name", "")).strip() != list_name:
                continue
            # Only patch the first matching row (or the only row if no list_name).
            unresolved = int(row.get("items_skipped_unresolved") or 0)
            if unresolved > 0:
                row["items_skipped_unresolved"] = unresolved - 1
            row["items_resolved"] = int(row.get("items_resolved") or 0) + 1
            row["items_added"] = int(row.get("items_added") or 0) + 1
            break

    @staticmethod
    def _replace_unresolved_items(profile: dict, results: list[dict]) -> None:
        """Replace the stored unresolved snapshot from the latest list sync.

        Items whose cache_key the user has already manually resolved are never
        re-added, even if the sync couldn't resolve them for some reason.
        """
        manually_resolved: set[str] = set(
            (profile.get("manual_resolution_cache") or {}).keys()
        )
        by_key: dict[str, dict] = {}
        for row in results:
            for item in row.get("unresolved_items", []):
                if not isinstance(item, dict):
                    continue
                key = item.get("cache_key")
                if key and key not in manually_resolved:
                    by_key[key] = item
        profile["unresolved_items"] = list(by_key.values())

    @staticmethod
    def _active_unresolved_counts(unresolved_items: list[dict]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in unresolved_items:
            if not isinstance(item, dict):
                continue
            list_name = str(item.get("list_name") or "").strip()
            if not list_name:
                continue
            counts[list_name] = counts.get(list_name, 0) + 1
        return counts

    @classmethod
    def _with_active_unresolved_counts(cls, rows: list[dict], unresolved_items: list[dict]) -> list[dict]:
        counts = cls._active_unresolved_counts(unresolved_items)
        normalized_rows: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            next_row = copy.deepcopy(row)
            list_name = str(next_row.get("list_name") or "").strip()
            if list_name:
                next_row["items_skipped_unresolved"] = counts.get(list_name, 0)
            normalized_rows.append(next_row)
        return normalized_rows

    @staticmethod
    def _merge_activity_results(profile: dict, results: list[dict], timestamp: str) -> None:
        merged = copy.deepcopy(profile.get("activity_results", {}))
        state = _normalize_activity_state(profile.get("activity_state"))
        for row in results:
            key = ACTIVITY_RESULT_NAMES.get(str(row.get("display_name", "")).strip())
            if not key:
                continue
            merged[key] = {
                "timestamp": timestamp,
                "row": copy.deepcopy(row),
            }
            if key == "watch_history":
                source_name = str(row.get("source_name", "")).strip().lower()
                history_cursor = str(row.get("history_cursor", "") or "").strip()
                if history_cursor:
                    if "simkl" in source_name:
                        state["simkl_history_cursor"] = history_cursor
                    if "trakt" in source_name:
                        state["trakt_history_cursor"] = history_cursor
            # Persist source freshness timestamps regardless of the result type —
            # any row from a source may carry an updated activities_ts.
            source_name = str(row.get("source_name", "")).strip().lower()
            activities_ts = str(row.get("activities_ts", "") or "").strip()
            if activities_ts:
                if "simkl" in source_name:
                    state["simkl_activities_ts"] = activities_ts
                if "trakt" in source_name:
                    state["trakt_activities_ts"] = activities_ts
        profile["activity_results"] = merged
        profile["activity_state"] = state

    def _public_profile(self, profile: dict, include_credentials: bool = False) -> dict:
        unresolved_items = copy.deepcopy(profile.get("unresolved_items", []))
        result = {
            "profile_id": profile["profile_id"],
            "created_at": profile.get("created_at"),
            "updated_at": profile.get("updated_at"),
            "last_sync": profile.get("last_sync"),
            "last_results": self._with_active_unresolved_counts(
                profile.get("last_results", []),
                unresolved_items,
            ),
            "sync_live_results": copy.deepcopy(profile.get("sync_live_results", [])),
            "sync_running": bool(profile.get("sync_running")),
            "sync_cancel_requested": bool(profile.get("sync_cancel_requested")),
            "sync_error": profile.get("sync_error"),
            "sync_status": profile.get("sync_status"),
            "sync_started_at": profile.get("sync_started_at"),
            "sync_updated_at": profile.get("sync_updated_at"),
            "history": copy.deepcopy(profile.get("history", [])),
            "next_sync_at": profile.get("next_sync_at"),
            "last_history_sync": profile.get("last_history_sync"),
            "next_history_sync_at": profile.get("next_history_sync_at"),
            "last_resume_sync": profile.get("last_resume_sync"),
            "next_resume_sync_at": profile.get("next_resume_sync_at"),
            "activity_results": copy.deepcopy(profile.get("activity_results", {})),
            "activity_state": copy.deepcopy(profile.get("activity_state", {})),
            "options": copy.deepcopy(profile.get("options", {})),
            "last_sync_job_snapshot": copy.deepcopy(profile.get("last_sync_job_snapshot", {})),
            "sync_job_id": profile.get("sync_job_id"),
            "anime_review_summary": {
                "manual_overrides": len(profile.get("anime_manual_overrides", {})),
                "reviewed": len(profile.get("anime_review_decisions", {})),
                "unresolved_anime": len([
                    item for item in unresolved_items
                    if str(item.get("simkl_type", "")).strip().lower() == "anime"
                ]),
                "ambiguous_anime": len([
                    item for item in unresolved_items
                    if str(item.get("simkl_type", "")).strip().lower() == "anime"
                    and str(item.get("match_confidence", "")).strip().lower() == "ambiguous"
                ]),
                "remapped_anime": len([
                    row for row in profile.get("last_results", [])
                    if isinstance(row, dict) and int((row.get("match_breakdown") or {}).get("root_series", 0) or 0) > 0
                ]),
            },
        }
        if include_credentials:
            result["credentials"] = public_credentials(profile.get("credentials", {}))
        return result

    def get_site_stats(self) -> dict:
        with self._lock:
            now = utc_now()
            cutoff_24h = now - timedelta(hours=24)
            source_usage = {"simkl": 0, "anilist": 0, "trakt": 0, "mdblist": 0}
            totals = {
                "profiles": len(self._profiles),
                "profiles_with_sync": 0,
                "profiles_syncing_now": 0,
                "managed_lists": 0,
                "current_synced_lists": 0,
                "sync_runs": 0,
            }
            current_totals = _result_totals([])
            last_24h = {
                "sync_runs": 0,
                "dry_runs": 0,
                "completed_runs": 0,
                "failed_runs": 0,
                "stopped_runs": 0,
                "lists": 0,
                "fetched": 0,
                "resolved": 0,
                "added": 0,
                "removed": 0,
                "duplicates": 0,
                "unresolved": 0,
                "errors": 0,
            }
            for profile in self._profiles.values():
                if profile.get("last_sync"):
                    totals["profiles_with_sync"] += 1
                if profile.get("sync_running"):
                    totals["profiles_syncing_now"] += 1
                totals["managed_lists"] += len(profile.get("managed_lists", []))
                profile_current_totals = _result_totals(profile.get("last_results", []))
                totals["current_synced_lists"] += profile_current_totals["lists"]
                for key in ["fetched", "resolved", "added", "removed", "duplicates", "unresolved", "errors"]:
                    current_totals[key] += profile_current_totals[key]

                for source in _configured_sources_for_profile(profile):
                    source_usage[source] += 1

                for entry in profile.get("history", []):
                    if not isinstance(entry, dict):
                        continue
                    totals["sync_runs"] += 1
                    timestamp = parse_iso_datetime(entry.get("timestamp"))
                    entry_totals = _result_totals(entry.get("results", []))
                    status = str(entry.get("status", "completed") or "completed").strip().lower()
                    if timestamp is None or timestamp < cutoff_24h:
                        continue
                    last_24h["sync_runs"] += 1
                    last_24h["dry_runs"] += int(bool(entry.get("dry_run", False)))
                    if status == "failed":
                        last_24h["failed_runs"] += 1
                    elif status == "stopped":
                        last_24h["stopped_runs"] += 1
                    else:
                        last_24h["completed_runs"] += 1
                    for key in ["lists", "fetched", "resolved", "added", "removed", "duplicates", "unresolved", "errors"]:
                        last_24h[key] += entry_totals[key]
                    if entry.get("error_message"):
                        last_24h["errors"] += 1

            return {
                "generated_at": now.isoformat(),
                "totals": {
                    **totals,
                    "current_items_fetched": current_totals["fetched"],
                    "current_items_resolved": current_totals["resolved"],
                    "current_items_added": current_totals["added"],
                    "current_items_removed": current_totals["removed"],
                    "current_items_unresolved": current_totals["unresolved"],
                    "current_errors": current_totals["errors"],
                },
                "last_24h": last_24h,
                "source_usage": source_usage,
            }

    def create_profile(self, password: str, credentials: dict, options: dict) -> dict:
        if not password:
            raise ValueError("Profile password is required")

        normalized_credentials = normalize_credentials(credentials)
        normalized_options = normalize_profile_options(options)
        profile_id = str(uuid.uuid4())
        now = utc_now_iso()
        profile = {
            "profile_id": profile_id,
            "password_hash": generate_password_hash(password, method="pbkdf2:sha256", salt_length=16),
            "credentials": normalized_credentials,
            "options": normalized_options,
            "created_at": now,
            "updated_at": now,
            "last_sync": None,
            "last_results": [],
            "sync_live_results": [],
            "sync_running": False,
            "sync_cancel_requested": False,
            "sync_error": None,
            "sync_status": "Idle",
            "sync_started_at": None,
            "sync_updated_at": None,
            "history": [],
            "next_sync_at": (
                self._next_sync_iso(normalized_options["interval_seconds"])
                if normalized_options["auto_sync"]
                else None
            ),
            "last_history_sync": None,
            "next_history_sync_at": (
                self._next_history_sync_iso(normalized_options["trakt_watched_history_interval_seconds"])
                if normalized_options.get("auto_history_sync") and normalized_options["activity_history_source"] != "off"
                else None
            ),
            "last_resume_sync": None,
            "next_resume_sync_at": (
                self._next_resume_sync_iso()
                if normalized_options["auto_sync"] and normalized_options["activity_resume_source"] != "off"
                else None
            ),
            "activity_results": {},
            "activity_state": _normalize_activity_state(None),
            "managed_lists": [],
            "anime_manual_overrides": {},
            "anime_review_decisions": {},
            "last_sync_job_snapshot": _normalize_sync_job_snapshot(None),
            "sync_job_id": "",
            "unresolved_items": [],
            "resolution_cache": {},
            "failed_resolution_cache": {},
            "manual_resolution_cache": {},
            "manual_list_additions": {},
        }

        with self._lock:
            self._profiles[profile_id] = profile
            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def get_profile(self, profile_id: str, password: str, include_credentials: bool = True) -> dict:
        with self._lock:
            profile = self._authenticate_locked(profile_id, password)
            return self._public_profile(profile, include_credentials=include_credentials)

    def get_profile_by_id(self, profile_id: str, include_credentials: bool = True) -> dict:
        with self._lock:
            profile = self._get_profile_locked(profile_id)
            return self._public_profile(profile, include_credentials=include_credentials)

    def get_private_profile_by_id(self, profile_id: str) -> dict:
        with self._lock:
            return copy.deepcopy(self._get_profile_locked(profile_id))

    def update_profile(self, profile_id: str, password: str, credentials: dict, options: dict) -> dict:
        normalized_options = normalize_profile_options(options)

        with self._lock:
            profile = self._authenticate_locked(profile_id, password)
            previous_auto_sync = bool(profile.get("options", {}).get("auto_sync", True))
            previous_next_sync_at = profile.get("next_sync_at")
            previous_resume_source = str(profile.get("options", {}).get("activity_resume_source", "off") or "off")
            previous_next_resume_sync_at = profile.get("next_resume_sync_at")
            profile["credentials"] = merge_credentials(profile.get("credentials"), credentials)
            profile["options"] = normalized_options
            profile["updated_at"] = utc_now_iso()
            profile["sync_error"] = None
            profile["sync_status"] = "Idle"
            if not profile["sync_running"]:
                profile["sync_live_results"] = []
                previous_auto_history_sync = bool(profile.get("options", {}).get("auto_history_sync", False))
                previous_history_source = str(profile.get("options", {}).get("activity_history_source", "off") or "off")
                previous_next_history_sync_at = profile.get("next_history_sync_at")
                if normalized_options["auto_sync"]:
                    profile["next_sync_at"] = (
                        previous_next_sync_at
                        if previous_auto_sync and previous_next_sync_at
                        else self._next_sync_iso(normalized_options["interval_seconds"])
                    )
                else:
                    profile["next_sync_at"] = None
                if normalized_options.get("auto_history_sync") and normalized_options["activity_history_source"] != "off":
                    profile["next_history_sync_at"] = (
                        previous_next_history_sync_at
                        if previous_auto_history_sync and previous_history_source != "off" and previous_next_history_sync_at
                        else self._next_history_sync_iso(normalized_options["trakt_watched_history_interval_seconds"])
                    )
                else:
                    profile["next_history_sync_at"] = None
                if normalized_options["auto_sync"] and normalized_options["activity_resume_source"] != "off":
                    profile["next_resume_sync_at"] = (
                        previous_next_resume_sync_at
                        if previous_auto_sync and previous_resume_source != "off" and previous_next_resume_sync_at
                        else self._next_resume_sync_iso()
                    )
                else:
                    profile["next_resume_sync_at"] = None
            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def update_profile_by_id(self, profile_id: str, credentials: dict, options: dict) -> dict:
        normalized_options = normalize_profile_options(options)

        with self._lock:
            profile = self._get_profile_locked(profile_id)
            previous_auto_sync = bool(profile.get("options", {}).get("auto_sync", True))
            previous_next_sync_at = profile.get("next_sync_at")
            previous_resume_source = str(profile.get("options", {}).get("activity_resume_source", "off") or "off")
            previous_next_resume_sync_at = profile.get("next_resume_sync_at")
            profile["credentials"] = merge_credentials(profile.get("credentials"), credentials)
            profile["options"] = normalized_options
            profile["updated_at"] = utc_now_iso()
            profile["sync_error"] = None
            profile["sync_status"] = "Idle"
            if not profile["sync_running"]:
                profile["sync_live_results"] = []
                previous_auto_history_sync = bool(profile.get("options", {}).get("auto_history_sync", False))
                previous_history_source = str(profile.get("options", {}).get("activity_history_source", "off") or "off")
                previous_next_history_sync_at = profile.get("next_history_sync_at")
                if normalized_options["auto_sync"]:
                    profile["next_sync_at"] = (
                        previous_next_sync_at
                        if previous_auto_sync and previous_next_sync_at
                        else self._next_sync_iso(normalized_options["interval_seconds"])
                    )
                else:
                    profile["next_sync_at"] = None
                if normalized_options.get("auto_history_sync") and normalized_options["activity_history_source"] != "off":
                    profile["next_history_sync_at"] = (
                        previous_next_history_sync_at
                        if previous_auto_history_sync and previous_history_source != "off" and previous_next_history_sync_at
                        else self._next_history_sync_iso(normalized_options["trakt_watched_history_interval_seconds"])
                    )
                else:
                    profile["next_history_sync_at"] = None
                if normalized_options["auto_sync"] and normalized_options["activity_resume_source"] != "off":
                    profile["next_resume_sync_at"] = (
                        previous_next_resume_sync_at
                        if previous_auto_sync and previous_resume_source != "off" and previous_next_resume_sync_at
                        else self._next_resume_sync_iso()
                    )
                else:
                    profile["next_resume_sync_at"] = None
            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def reset_profile_password_by_id(self, profile_id: str, new_password: str) -> dict:
        if not new_password:
            raise ValueError("New profile password is required")

        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles.get(normalized_id)
            if profile is None:
                raise KeyError(profile_id)
            profile["password_hash"] = generate_password_hash(new_password, method="pbkdf2:sha256", salt_length=16)
            profile["updated_at"] = utc_now_iso()
            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def claim_profile_for_sync(self, profile_id: str, password: str, sync_modes: dict | None = None) -> dict:
        with self._lock:
            profile = self._authenticate_locked(profile_id, password)
            if profile["sync_running"]:
                raise RuntimeError("Sync already in progress")
            now = utc_now_iso()
            profile["sync_running"] = True
            profile["sync_cancel_requested"] = False
            profile["sync_error"] = None
            profile["sync_status"] = "Queued"
            profile["sync_started_at"] = now
            profile["sync_updated_at"] = now
            profile["sync_live_results"] = []
            profile["sync_job_id"] = str(uuid.uuid4())
            profile["last_sync_job_snapshot"] = {
                "job_id": profile["sync_job_id"],
                "phase": "queued",
                "provider": "",
                "current_list": "",
                "started_at": now,
                "updated_at": now,
                "stopped_at": None,
                "results": [],
                "totals": _result_totals([]),
            }
            self._save_locked()
            claimed = copy.deepcopy(profile)
            claimed["pending_sync_modes"] = self._normalize_sync_modes(sync_modes)
            return claimed

    def claim_profile_for_sync_by_id(self, profile_id: str, sync_modes: dict | None = None) -> dict:
        with self._lock:
            profile = self._get_profile_locked(profile_id)
            if profile["sync_running"]:
                raise RuntimeError("Sync already in progress")
            now = utc_now_iso()
            profile["sync_running"] = True
            profile["sync_cancel_requested"] = False
            profile["sync_error"] = None
            profile["sync_status"] = "Queued"
            profile["sync_started_at"] = now
            profile["sync_updated_at"] = now
            profile["sync_live_results"] = []
            profile["sync_job_id"] = str(uuid.uuid4())
            profile["last_sync_job_snapshot"] = {
                "job_id": profile["sync_job_id"],
                "phase": "queued",
                "provider": "",
                "current_list": "",
                "started_at": now,
                "updated_at": now,
                "stopped_at": None,
                "results": [],
                "totals": _result_totals([]),
            }
            self._save_locked()
            claimed = copy.deepcopy(profile)
            claimed["pending_sync_modes"] = self._normalize_sync_modes(sync_modes)
            return claimed

    def claim_due_profiles(self) -> list[dict]:
        due_profiles: list[dict] = []
        now = utc_now()
        with self._lock:
            changed = False
            for profile in self._profiles.values():
                if profile["sync_running"]:
                    continue
                options = profile.get("options", {})
                if not options.get("auto_sync", True) and not options.get("auto_history_sync", False):
                    continue
                due_modes = {
                    "lists": False,
                    "history": False,
                    "resume": False,
                }
                next_sync_at = parse_iso_datetime(profile.get("next_sync_at"))
                if options.get("auto_sync", True) and (next_sync_at is None or next_sync_at <= now):
                    due_modes["lists"] = True
                next_history_sync_at = parse_iso_datetime(profile.get("next_history_sync_at"))
                if options.get("auto_history_sync", False) and profile.get("options", {}).get("activity_history_source") != "off" and (
                    next_history_sync_at is None or next_history_sync_at <= now
                ):
                    due_modes["history"] = True
                next_resume_sync_at = parse_iso_datetime(profile.get("next_resume_sync_at"))
                if options.get("auto_sync", True) and profile.get("options", {}).get("activity_resume_source") != "off" and (
                    next_resume_sync_at is None or next_resume_sync_at <= now
                ):
                    due_modes["resume"] = True
                if not any(due_modes.values()):
                    continue
                now_iso = utc_now_iso()
                profile["sync_running"] = True
                profile["sync_cancel_requested"] = False
                profile["sync_error"] = None
                profile["sync_status"] = "Queued"
                profile["sync_started_at"] = now_iso
                profile["sync_updated_at"] = now_iso
                profile["sync_live_results"] = []
                profile["sync_job_id"] = str(uuid.uuid4())
                profile["last_sync_job_snapshot"] = {
                    "job_id": profile["sync_job_id"],
                    "phase": "queued",
                    "provider": "",
                    "current_list": "",
                    "started_at": now_iso,
                    "updated_at": now_iso,
                    "stopped_at": None,
                    "results": [],
                    "totals": _result_totals([]),
                }
                claimed = copy.deepcopy(profile)
                claimed["pending_sync_modes"] = due_modes
                due_profiles.append(claimed)
                changed = True

            if changed:
                self._save_locked()

        return due_profiles

    def record_sync_success(
        self,
        profile_id: str,
        results: list[dict],
        dry_run: bool = False,
        managed_lists: list[dict] | None = None,
        sync_modes: dict | None = None,
        resolution_cache: dict | None = None,
        failed_resolution_cache: dict | None = None,
    ) -> dict:
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles[normalized_id]
            now = utc_now_iso()
            normalized_modes = self._normalize_sync_modes(sync_modes)
            if normalized_modes["lists"]:
                profile["last_results"] = [
                    copy.deepcopy(row)
                    for row in results
                    if str(row.get("list_name", "")).strip()
                ]
            profile["sync_live_results"] = []
            if managed_lists is not None:
                profile["managed_lists"] = _normalize_managed_lists(managed_lists)
            if resolution_cache is not None and not dry_run:
                # Merge: sync-discovered entries go in, but manual entries
                # (written by resolve_item_manually) always win because they
                # are re-applied on top from manual_resolution_cache.
                merged_rc = dict(profile.get("resolution_cache") or {})
                merged_rc.update(resolution_cache)
                manual_rc = profile.get("manual_resolution_cache") or {}
                merged_rc.update(manual_rc)  # Manual always wins
                profile["resolution_cache"] = merged_rc
            if failed_resolution_cache is not None and not dry_run:
                # Don't let the sync re-add a failed entry for a key the user
                # has already manually resolved.
                manual_keys = set((profile.get("manual_resolution_cache") or {}).keys())
                merged_frc = dict(profile.get("failed_resolution_cache") or {})
                merged_frc.update(failed_resolution_cache)
                for key in manual_keys:
                    merged_frc.pop(key, None)
                profile["failed_resolution_cache"] = merged_frc
            if normalized_modes["lists"] and not dry_run:
                self._replace_unresolved_items(profile, results)
            self._merge_activity_results(profile, results, now)
            profile["sync_error"] = None
            profile["sync_running"] = False
            profile["sync_cancel_requested"] = False
            profile["sync_status"] = "Completed"
            profile["updated_at"] = now
            profile["sync_updated_at"] = now
            profile["last_sync_job_snapshot"] = {
                "job_id": profile.get("sync_job_id", ""),
                "phase": "completed",
                "provider": "",
                "current_list": "",
                "started_at": profile.get("sync_started_at"),
                "updated_at": now,
                "stopped_at": now,
                "results": copy.deepcopy(results),
                "totals": _result_totals(results),
            }

            self._append_history_entry(profile, now, dry_run, results=results, status="completed")

            self._apply_next_run_schedule(profile, normalized_modes, dry_run)
            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def record_sync_error(
        self,
        profile_id: str,
        error_message: str,
        dry_run: bool = False,
        sync_modes: dict | None = None,
    ) -> dict:
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles[normalized_id]
            now = utc_now_iso()
            normalized_modes = self._normalize_sync_modes(sync_modes)
            profile["sync_running"] = False
            profile["sync_cancel_requested"] = False
            profile["sync_error"] = error_message
            profile["sync_status"] = f"Failed: {error_message}"
            profile["updated_at"] = now
            profile["sync_updated_at"] = now
            profile["sync_live_results"] = []
            profile["last_sync_job_snapshot"] = {
                "job_id": profile.get("sync_job_id", ""),
                "phase": "failed",
                "provider": "",
                "current_list": "",
                "started_at": profile.get("sync_started_at"),
                "updated_at": now,
                "stopped_at": now,
                "results": [],
                "totals": {},
            }
            self._append_history_entry(
                profile,
                now,
                dry_run,
                results=[],
                status="failed",
                error_message=error_message,
            )
            self._apply_next_run_schedule(profile, normalized_modes, dry_run)
            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def get_unresolved_items(self, profile_id: str) -> list[dict]:
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles[normalized_id]
            return copy.deepcopy(profile.get("unresolved_items", []))

    def resolve_item_manually(self, profile_id: str, cache_key: str, tmdb_id: int) -> list[dict]:
        """Add a manual TMDB mapping and remove from unresolved.

        Manual mappings are stored in ``manual_resolution_cache`` — a separate
        key that ``record_sync_success`` never overwrites.  This prevents the
        common race where a sync finishes *after* the user clicks Map and dumps
        its own (pre-manual) resolution_cache on top, erasing the mapping and
        causing the item to reappear as unresolved on the next sync.
        """
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles[normalized_id]
            # Manual cache: never touched by sync workers.
            mrc = dict(profile.get("manual_resolution_cache") or {})
            mrc[cache_key] = tmdb_id
            profile["manual_resolution_cache"] = mrc
            # Also write through to the live resolution_cache so the *next*
            # sync picks it up immediately without needing a full cache rebuild.
            rc = dict(profile.get("resolution_cache") or {})
            rc[cache_key] = tmdb_id
            profile["resolution_cache"] = rc
            # Remove from failed resolution cache so it gets retried.
            frc = dict(profile.get("failed_resolution_cache") or {})
            frc.pop(cache_key, None)
            profile["failed_resolution_cache"] = frc
            # Find the item being resolved so we can update the stats row.
            resolved_item = next(
                (i for i in profile.get("unresolved_items", []) if i.get("cache_key") == cache_key),
                None,
            )
            profile["unresolved_items"] = [
                item for item in profile.get("unresolved_items", [])
                if item.get("cache_key") != cache_key
            ]
            # Patch last_results so the dashboard table reflects the fix immediately
            # without waiting for the next full sync.
            if resolved_item:
                self._patch_resolved_stats(profile, resolved_item)
                if str(resolved_item.get("simkl_type", "")).strip().lower() == "anime":
                    overrides = dict(profile.get("anime_manual_overrides") or {})
                    overrides[cache_key] = {
                        "tmdb_id": tmdb_id,
                        "title": resolved_item.get("title"),
                        "media_type": resolved_item.get("media_type"),
                        "saved_at": utc_now_iso(),
                    }
                    profile["anime_manual_overrides"] = overrides
                    decisions = dict(profile.get("anime_review_decisions") or {})
                    decisions[cache_key] = "manual_map"
                    profile["anime_review_decisions"] = decisions
                # Track manual list addition so the sync worker never removes it.
                list_name = resolved_item.get("list_name") or ""
                media_type = resolved_item.get("media_type") or "movie"
                if list_name:
                    mla = dict(profile.get("manual_list_additions") or {})
                    entries = list(mla.get(list_name) or [])
                    entry = {"tmdb_id": tmdb_id, "media_type": media_type}
                    if not any(e.get("tmdb_id") == tmdb_id and e.get("media_type") == media_type for e in entries):
                        entries.append(entry)
                    mla[list_name] = entries
                    profile["manual_list_additions"] = mla
            self._save_locked()
            return copy.deepcopy(profile["unresolved_items"])

    def dismiss_unresolved_item(self, profile_id: str, cache_key: str) -> list[dict]:
        """Remove an unresolved item without resolving it (user dismisses it)."""
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles[normalized_id]
            dismissed_item = next(
                (i for i in profile.get("unresolved_items", []) if i.get("cache_key") == cache_key),
                None,
            )
            profile["unresolved_items"] = [
                item for item in profile.get("unresolved_items", [])
                if item.get("cache_key") != cache_key
            ]
            if dismissed_item and str(dismissed_item.get("simkl_type", "")).strip().lower() == "anime":
                decisions = dict(profile.get("anime_review_decisions") or {})
                decisions[cache_key] = "dismissed"
                profile["anime_review_decisions"] = decisions
            self._save_locked()
            return copy.deepcopy(profile["unresolved_items"])

    def request_sync_cancel(self, profile_id: str) -> dict:
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles[normalized_id]
            if not profile.get("sync_running"):
                raise RuntimeError("No sync is currently running")
            now = utc_now_iso()
            profile["sync_cancel_requested"] = True
            profile["sync_status"] = "Stopping..."
            profile["sync_updated_at"] = now
            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def is_sync_cancel_requested(self, profile_id: str) -> bool:
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles[normalized_id]
            return bool(profile.get("sync_cancel_requested"))

    def record_sync_cancelled(self, profile_id: str, dry_run: bool = False, sync_modes: dict | None = None) -> dict:
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles[normalized_id]
            now = utc_now_iso()
            normalized_modes = self._normalize_sync_modes(sync_modes)
            profile["sync_running"] = False
            profile["sync_cancel_requested"] = False
            profile["sync_error"] = None
            profile["sync_status"] = "Stopped"
            profile["updated_at"] = now
            profile["sync_updated_at"] = now
            profile["sync_live_results"] = []
            snapshot = _normalize_sync_job_snapshot(profile.get("last_sync_job_snapshot"))
            snapshot["phase"] = "stopped"
            snapshot["updated_at"] = now
            snapshot["stopped_at"] = now
            profile["last_sync_job_snapshot"] = snapshot
            self._append_history_entry(profile, now, dry_run, results=[], status="stopped")
            self._apply_next_run_schedule(profile, normalized_modes, dry_run)
            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def update_sync_status(self, profile_id: str, status: str) -> dict:
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles[normalized_id]
            now = utc_now_iso()
            profile["sync_status"] = status
            profile["sync_updated_at"] = now
            if profile["sync_started_at"] is None:
                profile["sync_started_at"] = now
            snapshot = _normalize_sync_job_snapshot(profile.get("last_sync_job_snapshot"))
            snapshot["job_id"] = str(profile.get("sync_job_id", "") or snapshot.get("job_id", ""))
            snapshot["phase"] = "running"
            snapshot["updated_at"] = now
            snapshot["started_at"] = snapshot.get("started_at") or profile.get("sync_started_at") or now
            snapshot["current_list"] = status
            provider = ""
            if "simkl" in status.lower():
                provider = "SIMKL"
            elif "anilist" in status.lower():
                provider = "AniList"
            elif "trakt" in status.lower():
                provider = "Trakt"
            elif "mdblist" in status.lower():
                provider = "MDBList"
            snapshot["provider"] = provider
            profile["last_sync_job_snapshot"] = snapshot
            # No disk save — status is in-memory only; polls read from memory.
            return self._public_profile(profile, include_credentials=True)

    def update_sync_progress(self, profile_id: str, results: list[dict]) -> dict:
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles[normalized_id]
            now = utc_now_iso()
            profile["sync_live_results"] = copy.deepcopy(results or [])
            profile["sync_updated_at"] = now
            if profile["sync_started_at"] is None:
                profile["sync_started_at"] = now
            snapshot = _normalize_sync_job_snapshot(profile.get("last_sync_job_snapshot"))
            snapshot["job_id"] = str(profile.get("sync_job_id", "") or snapshot.get("job_id", ""))
            snapshot["phase"] = "running"
            snapshot["updated_at"] = now
            snapshot["started_at"] = snapshot.get("started_at") or profile.get("sync_started_at") or now
            snapshot["results"] = copy.deepcopy(results or [])
            snapshot["totals"] = _result_totals(results or [])
            profile["last_sync_job_snapshot"] = snapshot
            # No disk save — progress is in-memory only.
            return self._public_profile(profile, include_credentials=True)

    def delete_managed_list_by_id(self, profile_id: str, list_name: str, credentials: dict) -> dict:
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles[normalized_id]
            if profile.get("sync_running"):
                raise RuntimeError("Cannot delete a managed list while a sync is in progress")

            normalized_credentials = normalize_credentials(credentials)
            now = utc_now_iso()
            profile["credentials"] = normalized_credentials
            profile["managed_lists"] = [
                item for item in profile.get("managed_lists", [])
                if str(item.get("list_name", "")).strip() != list_name
            ]
            profile["last_results"] = [
                item for item in profile.get("last_results", [])
                if str(item.get("list_name", "")).strip() != list_name
            ]

            trimmed_history = []
            for entry in profile.get("history", []):
                if not isinstance(entry, dict):
                    continue
                next_entry = copy.deepcopy(entry)
                next_entry["results"] = [
                    item for item in next_entry.get("results", [])
                    if str(item.get("list_name", "")).strip() != list_name
                ]
                trimmed_history.append(next_entry)

            profile["history"] = trimmed_history[:MAX_HISTORY_ITEMS]
            profile["updated_at"] = now
            profile["sync_status"] = "Managed list deleted"
            profile["sync_updated_at"] = now
            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def delete_profile_by_id(self, profile_id: str) -> None:
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles.get(normalized_id)
            if not profile:
                raise KeyError("Profile not found")
            if profile.get("sync_running"):
                raise RuntimeError("Cannot delete a profile while a sync is in progress")
            del self._profiles[normalized_id]
            self._save_locked()

    def reset_history_import_state_by_id(self, profile_id: str) -> dict:
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles.get(normalized_id)
            if not profile:
                raise KeyError("Profile not found")

            activity_state = _normalize_activity_state(profile.get("activity_state"))
            activity_state["simkl_history_cursor"] = ""
            activity_state["trakt_history_cursor"] = ""
            activity_state["simkl_activities_ts"] = ""
            activity_state["trakt_activities_ts"] = ""
            profile["activity_state"] = activity_state

            activity_results = copy.deepcopy(profile.get("activity_results", {}))
            if "watch_history" in activity_results:
                activity_results.pop("watch_history", None)
            profile["activity_results"] = activity_results
            profile["last_history_sync"] = None
            profile["updated_at"] = utc_now_iso()
            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def _authenticate_locked(self, profile_id: str, password: str) -> dict:
        profile = self._get_profile_locked(profile_id)
        if not password or not check_password_hash(profile["password_hash"], password):
            raise PermissionError("Invalid profile password")
        return profile

    def _get_profile_locked(self, profile_id: str) -> dict:
        normalized_id = self._normalize_profile_id(profile_id)
        profile = self._profiles.get(normalized_id)
        if not profile:
            raise KeyError("Profile not found")
        return profile
