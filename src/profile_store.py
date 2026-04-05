"""Persistent profile storage for the web dashboard."""

from __future__ import annotations

import copy
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

ALLOWED_MEDIA_TYPES = {"shows", "movies", "anime"}
DEFAULT_MEDIA_TYPES = ["shows", "movies", "anime"]
DEFAULT_SYNC_INTERVAL_SECONDS = 1800
MIN_SYNC_INTERVAL_SECONDS = 300
MAX_HISTORY_ITEMS = 20


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


def normalize_credentials(credentials: dict | None) -> dict:
    raw = credentials or {}
    simkl = raw.get("simkl", {})
    anilist = raw.get("anilist", {})
    pmdb = raw.get("pmdb", {})
    return {
        "simkl": {
            "client_id": str(simkl.get("client_id", "")).strip(),
            "client_secret": str(simkl.get("client_secret", "")).strip(),
            "access_token": str(simkl.get("access_token", "")).strip(),
        },
        "anilist": {
            "username": str(anilist.get("username", "")).strip(),
            "access_token": str(anilist.get("access_token", "")).strip(),
        },
        "pmdb": {
            "api_key": str(pmdb.get("api_key", "")).strip(),
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
        "media_types": media_types,
        "auto_sync": bool(raw.get("auto_sync", True)),
        "interval_seconds": interval_seconds,
    }


class ProfileStore:
    """JSON-backed profile storage with password authentication."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
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
            for profile_id, raw_profile in data.get("profiles", {}).items():
                if not raw_profile.get("password_hash"):
                    continue
                try:
                    normalized_id = self._normalize_profile_id(profile_id)
                    profiles[normalized_id] = self._hydrate_profile(normalized_id, raw_profile)
                except ValueError:
                    continue

            self._profiles = profiles

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
            "credentials": normalize_credentials(raw_profile.get("credentials")),
            "options": options,
            "created_at": created_at,
            "updated_at": raw_profile.get("updated_at") or created_at,
            "last_sync": raw_profile.get("last_sync"),
            "last_results": list(raw_profile.get("last_results", [])),
            "sync_running": False,
            "sync_error": raw_profile.get("sync_error"),
            "history": list(raw_profile.get("history", []))[:MAX_HISTORY_ITEMS],
            "next_sync_at": next_sync_at,
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

    @staticmethod
    def _serialize_profile(profile: dict) -> dict:
        return {
            "password_hash": profile["password_hash"],
            "credentials": copy.deepcopy(profile["credentials"]),
            "options": copy.deepcopy(profile["options"]),
            "created_at": profile.get("created_at"),
            "updated_at": profile.get("updated_at"),
            "last_sync": profile.get("last_sync"),
            "last_results": copy.deepcopy(profile.get("last_results", [])),
            "sync_error": profile.get("sync_error"),
            "history": copy.deepcopy(profile.get("history", []))[:MAX_HISTORY_ITEMS],
            "next_sync_at": profile.get("next_sync_at"),
        }

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
            "history": copy.deepcopy(profile.get("history", [])),
            "next_sync_at": profile.get("next_sync_at"),
            "options": copy.deepcopy(profile.get("options", {})),
        }
        if include_credentials:
            result["credentials"] = copy.deepcopy(profile.get("credentials", {}))
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
            "history": [],
            "next_sync_at": now if normalized_options["auto_sync"] else None,
        }

        with self._lock:
            self._profiles[profile_id] = profile
            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def get_profile(self, profile_id: str, password: str, include_credentials: bool = True) -> dict:
        with self._lock:
            profile = self._authenticate_locked(profile_id, password)
            return self._public_profile(profile, include_credentials=include_credentials)

    def update_profile(self, profile_id: str, password: str, credentials: dict, options: dict) -> dict:
        normalized_credentials = normalize_credentials(credentials)
        normalized_options = normalize_profile_options(options)

        with self._lock:
            profile = self._authenticate_locked(profile_id, password)
            profile["credentials"] = normalized_credentials
            profile["options"] = normalized_options
            profile["updated_at"] = utc_now_iso()
            profile["sync_error"] = None
            if not profile["sync_running"]:
                profile["next_sync_at"] = (
                    utc_now_iso() if normalized_options["auto_sync"] else None
                )
            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def claim_profile_for_sync(self, profile_id: str, password: str) -> dict:
        with self._lock:
            profile = self._authenticate_locked(profile_id, password)
            if profile["sync_running"]:
                raise RuntimeError("Sync already in progress")
            profile["sync_running"] = True
            profile["sync_error"] = None
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
                profile["sync_running"] = True
                profile["sync_error"] = None
                due_profiles.append(copy.deepcopy(profile))
                changed = True

            if changed:
                self._save_locked()

        return due_profiles

    def record_sync_success(self, profile_id: str, results: list[dict], dry_run: bool = False) -> dict:
        with self._lock:
            normalized_id = self._normalize_profile_id(profile_id)
            profile = self._profiles[normalized_id]
            now = utc_now_iso()
            profile["last_results"] = copy.deepcopy(results)
            profile["sync_error"] = None
            profile["sync_running"] = False
            profile["updated_at"] = now

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
            profile["updated_at"] = now
            if not dry_run:
                if profile["options"]["auto_sync"]:
                    profile["next_sync_at"] = self._next_sync_iso(profile["options"]["interval_seconds"])
                else:
                    profile["next_sync_at"] = None
            self._save_locked()
            return self._public_profile(profile, include_credentials=True)

    def _authenticate_locked(self, profile_id: str, password: str) -> dict:
        normalized_id = self._normalize_profile_id(profile_id)
        profile = self._profiles.get(normalized_id)
        if not profile:
            raise KeyError("Profile not found")
        if not password or not check_password_hash(profile["password_hash"], password):
            raise PermissionError("Invalid profile password")
        return profile
