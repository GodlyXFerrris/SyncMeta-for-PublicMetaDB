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
DEFAULT_SYNC_INTERVAL_SECONDS = 1800
MIN_SYNC_INTERVAL_SECONDS = 300
MAX_HISTORY_ITEMS = 20
SIMKL_ALLOWED_STATUSES = {"watching", "plantowatch", "completed", "hold", "dropped"}
ANILIST_ALLOWED_STATUSES = {"CURRENT", "PLANNING", "COMPLETED", "PAUSED", "DROPPED"}
DEFAULT_KEY_FILE_NAME = "profiles.key"


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
            "sync_watchlist": bool(trakt.get("sync_watchlist", True)),
            "sync_liked_lists": bool(trakt.get("sync_liked_lists", True)),
            "selected_lists": _normalize_trakt_selected_lists(trakt.get("selected_lists", [])),
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
            "sync_liked_lists": raw["trakt"]["sync_liked_lists"],
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
        if source not in {"liked", "discover", "default"}:
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
            "sync_liked_lists": incoming["trakt"]["sync_liked_lists"],
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


def normalize_profile_options(options: dict | None) -> dict:
    raw = options or {}
    interval_raw = raw.get("interval_seconds", DEFAULT_SYNC_INTERVAL_SECONDS)
    try:
        interval_seconds = int(interval_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Sync interval must be a whole number of seconds") from exc

    if interval_seconds < MIN_SYNC_INTERVAL_SECONDS:
        raise ValueError(f"Sync interval must be at least {MIN_SYNC_INTERVAL_SECONDS} seconds")

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

    return {
        "remove_missing": bool(raw.get("remove_missing", False)),
        "delete_disabled_lists": bool(raw.get("delete_disabled_lists", False)),
        "media_types": media_types,
        "auto_sync": bool(raw.get("auto_sync", True)),
        "interval_seconds": interval_seconds,
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
                profiles[normalized_id] = self._hydrate_profile(normalized_id, raw_profile)
                if "credentials" in raw_profile and "credentials_encrypted" not in raw_profile:
                    changed = True

            self._profiles = profiles
            if changed:
                self._save_locked()

    def _hydrate_profile(self, profile_id: str, raw_profile: dict) -> dict:
        created_at = raw_profile.get("created_at") or utc_now_iso()
        options = normalize_profile_options(raw_profile.get("options"))
        next_sync_at = raw_profile.get("next_sync_at")
        if options["auto_sync"] and not next_sync_at:
            next_sync_at = utc_now_iso()
        if not options["auto_sync"]:
            next_sync_at = None

        return {
            "profile_id": profile_id,
            "password_hash": raw_profile["password_hash"],
            "credentials": self._load_credentials(raw_profile),
            "options": options,
            "created_at": created_at,
            "updated_at": raw_profile.get("updated_at") or created_at,
            "last_sync": raw_profile.get("last_sync"),
            "last_results": list(raw_profile.get("last_results", [])),
            "sync_running": False,
            "sync_error": raw_profile.get("sync_error"),
            "sync_status": raw_profile.get("sync_status") or "Idle",
            "sync_started_at": raw_profile.get("sync_started_at"),
            "sync_updated_at": raw_profile.get("sync_updated_at"),
            "history": list(raw_profile.get("history", []))[:MAX_HISTORY_ITEMS],
            "next_sync_at": next_sync_at,
            "managed_lists": _normalize_managed_lists(raw_profile.get("managed_lists", [])),
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
            "sync_error": profile.get("sync_error"),
            "sync_status": profile.get("sync_status"),
            "sync_started_at": profile.get("sync_started_at"),
            "sync_updated_at": profile.get("sync_updated_at"),
            "history": copy.deepcopy(profile.get("history", []))[:MAX_HISTORY_ITEMS],
            "next_sync_at": profile.get("next_sync_at"),
            "managed_lists": copy.deepcopy(profile.get("managed_lists", [])),
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

    def _public_profile(self, profile: dict, include_credentials: bool = False) -> dict:
        result = {
            "profile_id": profile["profile_id"],
            "created_at": profile.get("created_at"),
            "updated_at": profile.get("updated_at"),
            "last_sync": profile.get("last_sync"),
            "last_results": copy.deepcopy(profile.get("last_results", [])),
            "sync_running": bool(profile.get("sync_running")),
            "sync_error": profile.get("sync_error"),
            "sync_status": profile.get("sync_status"),
            "sync_started_at": profile.get("sync_started_at"),
            "sync_updated_at": profile.get("sync_updated_at"),
            "history": copy.deepcopy(profile.get("history", [])),
            "next_sync_at": profile.get("next_sync_at"),
            "options": copy.deepcopy(profile.get("options", {})),
        }
        if include_credentials:
            result["credentials"] = public_credentials(profile.get("credentials", {}))
        return result

    def create_profile(self, password: str, credentials: dict, options: dict) -> dict:
        if not password:
            raise ValueError("Profile password is required")

        normalized_credentials = normalize_credentials(credentials)
        normalized_options = normalize_profile_options(options)
        profile_id = str(uuid.uuid4())
        now = utc_now_iso()
        profile = {
            "profile_id": profile_id,
            "password_hash": generate_password_hash(password),
            "credentials": normalized_credentials,
            "options": normalized_options,
            "created_at": now,
            "updated_at": now,
            "last_sync": None,
            "last_results": [],
            "sync_running": False,
            "sync_error": None,
            "sync_status": "Idle",
            "sync_started_at": None,
            "sync_updated_at": None,
            "history": [],
            "next_sync_at": now if normalized_options["auto_sync"] else None,
            "managed_lists": [],
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
            profile["credentials"] = merge_credentials(profile.get("credentials"), credentials)
            profile["options"] = normalized_options
            profile["updated_at"] = utc_now_iso()
            profile["sync_error"] = None
            profile["sync_status"] = "Idle"
            if not profile["sync_running"]:
                profile["next_sync_at"] = (
                    utc_now_iso() if normalized_options["auto_sync"] else None
                )
            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def update_profile_by_id(self, profile_id: str, credentials: dict, options: dict) -> dict:
        normalized_options = normalize_profile_options(options)

        with self._lock:
            profile = self._get_profile_locked(profile_id)
            profile["credentials"] = merge_credentials(profile.get("credentials"), credentials)
            profile["options"] = normalized_options
            profile["updated_at"] = utc_now_iso()
            profile["sync_error"] = None
            profile["sync_status"] = "Idle"
            if not profile["sync_running"]:
                profile["next_sync_at"] = utc_now_iso() if normalized_options["auto_sync"] else None
            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def claim_profile_for_sync(self, profile_id: str, password: str) -> dict:
        with self._lock:
            profile = self._authenticate_locked(profile_id, password)
            if profile["sync_running"]:
                raise RuntimeError("Sync already in progress")
            now = utc_now_iso()
            profile["sync_running"] = True
            profile["sync_error"] = None
            profile["sync_status"] = "Queued"
            profile["sync_started_at"] = now
            profile["sync_updated_at"] = now
            self._save_locked()
            return copy.deepcopy(profile)

    def claim_profile_for_sync_by_id(self, profile_id: str) -> dict:
        with self._lock:
            profile = self._get_profile_locked(profile_id)
            if profile["sync_running"]:
                raise RuntimeError("Sync already in progress")
            now = utc_now_iso()
            profile["sync_running"] = True
            profile["sync_error"] = None
            profile["sync_status"] = "Queued"
            profile["sync_started_at"] = now
            profile["sync_updated_at"] = now
            self._save_locked()
            return copy.deepcopy(profile)

    def claim_due_profiles(self) -> list[dict]:
        due_profiles: list[dict] = []
        now = utc_now()
        with self._lock:
            changed = False
            for profile in self._profiles.values():
                if profile["sync_running"]:
                    continue
                options = profile.get("options", {})
                if not options.get("auto_sync", True):
                    continue
                next_sync_at = parse_iso_datetime(profile.get("next_sync_at"))
                if next_sync_at is not None and next_sync_at > now:
                    continue
                now_iso = utc_now_iso()
                profile["sync_running"] = True
                profile["sync_error"] = None
                profile["sync_status"] = "Queued"
                profile["sync_started_at"] = now_iso
                profile["sync_updated_at"] = now_iso
                due_profiles.append(copy.deepcopy(profile))
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
    ) -> dict:
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles[normalized_id]
            now = utc_now_iso()
            profile["last_results"] = copy.deepcopy(results)
            if managed_lists is not None:
                profile["managed_lists"] = _normalize_managed_lists(managed_lists)
            profile["sync_error"] = None
            profile["sync_running"] = False
            profile["sync_status"] = "Completed"
            profile["updated_at"] = now
            profile["sync_updated_at"] = now

            history_entry = {
                "timestamp": now,
                "dry_run": dry_run,
                "results": copy.deepcopy(results),
            }
            profile["history"].insert(0, history_entry)
            profile["history"] = profile["history"][:MAX_HISTORY_ITEMS]

            if not dry_run:
                profile["last_sync"] = now
                if profile["options"]["auto_sync"]:
                    profile["next_sync_at"] = self._next_sync_iso(profile["options"]["interval_seconds"])
                else:
                    profile["next_sync_at"] = None

            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def record_sync_error(self, profile_id: str, error_message: str, dry_run: bool = False) -> dict:
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles[normalized_id]
            now = utc_now_iso()
            profile["sync_running"] = False
            profile["sync_error"] = error_message
            profile["sync_status"] = f"Failed: {error_message}"
            profile["updated_at"] = now
            profile["sync_updated_at"] = now
            if not dry_run:
                if profile["options"]["auto_sync"]:
                    profile["next_sync_at"] = self._next_sync_iso(profile["options"]["interval_seconds"])
                else:
                    profile["next_sync_at"] = None
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
