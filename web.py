"""Flask web UI for persistent SIMKL/AniList -> PublicMetaDB sync profiles."""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, redirect, render_template, request, url_for

from src.config import (
    AniListConfig,
    AppConfig,
    MdbListConfig,
    PublicMetaDBConfig,
    SimklConfig,
    SyncConfig,
    TraktConfig,
    validate_config,
)
from src.mdblist_client import MdbListClient
from src.publicmetadb_client import PublicMetaDBClient
from src.profile_store import ProfileStore, merge_credentials, normalize_credentials, normalize_profile_options
from src.simkl_client import SimklClient
from src.sync_service import SyncCancelled, SyncService, SyncStats, _status_list_name
from src.trakt_client import TraktClient

load_dotenv(Path(__file__).resolve().parent / ".env")

app = Flask(__name__, template_folder="templates", static_folder="static")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logger = logging.getLogger("web")

PROFILE_STORE_FILE = Path(
    os.getenv("PROFILE_STORE_FILE", str(Path(__file__).resolve().parent / "data" / "profiles.json"))
)
SCHEDULER_POLL_SECONDS = 5
SESSION_COOKIE_NAME = "syncmeta_session"
ACCESS_COOKIE_NAME = "syncmeta_site_access"
SESSION_TTL_SECONDS = int(os.getenv("SYNCMETA_SESSION_TTL_SECONDS", "2592000"))
LOGIN_MAX_ATTEMPTS = int(os.getenv("SYNCMETA_LOGIN_MAX_ATTEMPTS", "10"))
LOGIN_WINDOW_SECONDS = int(os.getenv("SYNCMETA_LOGIN_WINDOW_SECONDS", "900"))
SITE_ACCESS_PASSWORD = os.getenv("SITE_ACCESS_PASSWORD", "").strip()
ACCESS_MAX_ATTEMPTS = int(os.getenv("SYNCMETA_ACCESS_MAX_ATTEMPTS", "10"))
ACCESS_WINDOW_SECONDS = int(os.getenv("SYNCMETA_ACCESS_WINDOW_SECONDS", "900"))

SIMKL_STATUS_BY_LABEL = {
    "watching": "watching",
    "plan to watch": "plantowatch",
    "completed": "completed",
    "on hold": "hold",
    "dropped": "dropped",
}

ANILIST_STATUS_BY_LABEL = {
    "watching": "CURRENT",
    "planning": "PLANNING",
    "completed": "COMPLETED",
    "completed ona": "COMPLETED_ONA",
    "completed ova": "COMPLETED_OVA",
    "completed movie": "COMPLETED_MOVIE",
    "paused": "PAUSED",
    "dropped": "DROPPED",
}

_profile_store = ProfileStore(PROFILE_STORE_FILE)
_scheduler_lock = threading.Lock()
_scheduler_started = False


class ServerSessionStore:
    """Simple in-memory session store keyed by opaque cookies."""

    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS):
        self._ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}

    def create(self, profile_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._sessions[token] = {
                "profile_id": profile_id,
                "expires_at": now + self._ttl_seconds,
            }
        return token

    def get_profile_id(self, token: str | None) -> str | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(token)
            if not session:
                return None
            if session["expires_at"] <= now:
                self._sessions.pop(token, None)
                return None
            session["expires_at"] = now + self._ttl_seconds
            return session["profile_id"]

    def destroy(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)


class LoginAttemptLimiter:
    """Sliding-window limiter for profile login attempts."""

    def __init__(self, max_attempts: int = LOGIN_MAX_ATTEMPTS, window_seconds: int = LOGIN_WINDOW_SECONDS):
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._lock = threading.RLock()
        self._attempts: dict[str, list[float]] = {}

    def is_limited(self, key: str) -> bool:
        with self._lock:
            attempts = self._prune_locked(key)
            return len(attempts) >= self._max_attempts

    def record_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            attempts = self._prune_locked(key)
            attempts.append(now)
            self._attempts[key] = attempts

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)

    def _prune_locked(self, key: str) -> list[float]:
        now = time.time()
        attempts = [stamp for stamp in self._attempts.get(key, []) if now - stamp < self._window_seconds]
        if attempts:
            self._attempts[key] = attempts
        else:
            self._attempts.pop(key, None)
        return attempts


_session_store = ServerSessionStore()
_login_limiter = LoginAttemptLimiter()
_access_store = ServerSessionStore()
_access_limiter = LoginAttemptLimiter(max_attempts=ACCESS_MAX_ATTEMPTS, window_seconds=ACCESS_WINDOW_SECONDS)


def _stats_to_dict(stats: SyncStats) -> dict:
    data = asdict(stats)
    data["error_count"] = len(data.pop("errors", []))
    # Keep unresolved_items for persistence but don't bloat live progress payloads.
    return data


def _config_from_profile(profile: dict, dry_run: bool = False, sync_modes: dict | None = None) -> AppConfig:
    credentials = normalize_credentials(profile.get("credentials"))
    options = normalize_profile_options(profile.get("options"))
    activity_state = profile.get("activity_state", {}) if isinstance(profile.get("activity_state"), dict) else {}
    anilist_username = credentials["anilist"]["username"]
    trakt_username = credentials["trakt"]["username"]
    modes = {
        "lists": True,
        "history": options["activity_history_source"] != "off",
        "resume": options["activity_resume_source"] != "off",
    }
    if isinstance(sync_modes, dict):
        modes["lists"] = bool(sync_modes.get("lists", False))
        modes["history"] = bool(sync_modes.get("history", False)) and options["activity_history_source"] != "off"
        modes["resume"] = bool(sync_modes.get("resume", False)) and options["activity_resume_source"] != "off"

    return AppConfig(
        simkl=SimklConfig(
            client_id=credentials["simkl"]["client_id"],
            client_secret=credentials["simkl"]["client_secret"],
            access_token=credentials["simkl"]["access_token"],
            selected_statuses=credentials["simkl"]["selected_statuses"],
        ),
        anilist=AniListConfig(
            username=anilist_username,
            access_token=credentials["anilist"]["access_token"],
            enabled=bool(anilist_username),
            selected_statuses=credentials["anilist"]["selected_statuses"],
        ),
        trakt=TraktConfig(
            client_id=credentials["trakt"]["client_id"],
            client_secret=credentials["trakt"]["client_secret"],
            access_token=credentials["trakt"]["access_token"],
            refresh_token=credentials["trakt"]["refresh_token"],
            username=trakt_username,
            enabled=bool(credentials["trakt"]["client_id"] and credentials["trakt"]["access_token"]),
            sync_watchlist=credentials["trakt"]["sync_watchlist"],
            sync_watchlist_movies=credentials["trakt"]["sync_watchlist_movies"],
            sync_watchlist_shows=credentials["trakt"]["sync_watchlist_shows"],
            sync_liked_lists=credentials["trakt"]["sync_liked_lists"],
            selected_lists=credentials["trakt"]["selected_lists"],
        ),
        mdblist=MdbListConfig(
            api_key=credentials["mdblist"]["api_key"],
            enabled=bool(credentials["mdblist"]["api_key"] and credentials["mdblist"]["selected_lists"]),
            selected_lists=credentials["mdblist"]["selected_lists"],
        ),
        pmdb=PublicMetaDBConfig(api_key=credentials["pmdb"]["api_key"]),
        sync=SyncConfig(
            remove_missing=options["remove_missing"],
            delete_disabled_lists=options["delete_disabled_lists"],
            dry_run=dry_run,
            media_types=options["media_types"],
            simkl_sync_watched_history=modes["history"] and options["activity_history_source"] == "simkl",
            simkl_history_anime_only=bool(options.get("simkl_history_anime_only", False)),
            trakt_sync_watched_history=modes["history"] and options["activity_history_source"] == "trakt",
            simkl_history_cursor=str(activity_state.get("simkl_history_cursor", "") or "").strip(),
            trakt_history_cursor=str(activity_state.get("trakt_history_cursor", "") or "").strip(),
            trakt_watched_history_interval_seconds=options["trakt_watched_history_interval_seconds"],
            trakt_sync_full_watch_counts=False,
            trakt_reconcile_watched_history=False,
            trakt_sync_resume_progress=modes["resume"] and options["activity_resume_source"] == "trakt",
            simkl_visibility=options["simkl_visibility"],
            anilist_visibility=options["anilist_visibility"],
            trakt_personal_visibility=options["trakt_personal_visibility"],
            trakt_public_visibility=options["trakt_public_visibility"],
            mdblist_visibility=options["mdblist_visibility"],
        ),
    )


def _configured_sources(config: AppConfig) -> list[str]:
    sources = []
    if (
        config.simkl.client_id
        and config.simkl.access_token
        and (
            any(config.simkl.selected_statuses.get(media_type) for media_type in ["shows", "movies", "anime"])
            or config.sync.simkl_sync_watched_history
        )
    ):
        sources.append("simkl")
    if config.anilist.enabled and config.anilist.selected_statuses:
        sources.append("anilist")
    if (
        config.trakt.enabled
        and (
            config.trakt.sync_watchlist_movies
            or config.trakt.sync_watchlist_shows
            or config.trakt.sync_liked_lists
            or config.trakt.selected_lists
            or config.sync.trakt_sync_watched_history
            or config.sync.trakt_sync_resume_progress
        )
    ):
        sources.append("trakt")
    if config.mdblist.enabled:
        sources.append("mdblist")
    return sources


def _validate_profile_configuration(credentials: dict, options: dict) -> tuple[AppConfig | None, list[str]]:
    try:
        normalized_profile = {
            "credentials": normalize_credentials(credentials),
            "options": normalize_profile_options(options),
        }
    except ValueError as exc:
        return None, [str(exc)]

    config = _config_from_profile(normalized_profile, dry_run=False)
    sources = _configured_sources(config)
    if not sources:
        return None, ["Configure at least one source (SIMKL, AniList, Trakt, or MDBList)"]

    errors = validate_config(config, sources)
    return config, errors


def _json_error(message: str, status_code: int, details: list[str] | None = None):
    payload = {"error": message}
    if details:
        payload["details"] = details
    return jsonify(payload), status_code


def _profile_response(profile: dict, include_credentials: bool = False):
    payload = dict(profile)
    if not include_credentials:
        payload.pop("credentials", None)
    return jsonify({"profile": payload})


def _request_client_key() -> str:
    forwarded = str(request.headers.get("X-Forwarded-For", "")).split(",")[0].strip()
    return forwarded or request.remote_addr or "unknown"


def _session_token() -> str | None:
    return request.cookies.get(SESSION_COOKIE_NAME)


def _access_token() -> str | None:
    return request.cookies.get(ACCESS_COOKIE_NAME)


def _current_profile_id() -> str | None:
    return _session_store.get_profile_id(_session_token())


def _has_site_access() -> bool:
    if not SITE_ACCESS_PASSWORD:
        return True
    return bool(_access_store.get_profile_id(_access_token()))


def _current_public_profile(include_credentials: bool = False) -> dict | None:
    profile_id = _current_profile_id()
    if not profile_id:
        return None
    try:
        return _profile_store.get_profile_by_id(profile_id, include_credentials=include_credentials)
    except KeyError:
        _session_store.destroy(_session_token())
        return None


def _current_private_profile() -> dict | None:
    profile_id = _current_profile_id()
    if not profile_id:
        return None
    try:
        return _profile_store.get_private_profile_by_id(profile_id)
    except KeyError:
        _session_store.destroy(_session_token())
        return None


def _cookie_secure() -> bool:
    forwarded_proto = str(request.headers.get("X-Forwarded-Proto", "")).split(",")[0].strip().lower()
    return request.is_secure or forwarded_proto == "https"


def _with_session_cookie(response, session_token: str):
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="Lax",
        secure=_cookie_secure(),
    )
    return response


def _with_access_cookie(response, access_token: str):
    response.set_cookie(
        ACCESS_COOKIE_NAME,
        access_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="Lax",
        secure=_cookie_secure(),
    )
    return response


def _clear_session_cookie(response):
    response.delete_cookie(SESSION_COOKIE_NAME, httponly=True, samesite="Lax", secure=_cookie_secure())
    return response


def _clear_access_cookie(response):
    response.delete_cookie(ACCESS_COOKIE_NAME, httponly=True, samesite="Lax", secure=_cookie_secure())
    return response


def _run_profile_sync(profile: dict, dry_run: bool = False, sync_modes: dict | None = None) -> None:
    profile_id = profile["profile_id"]
    modes = sync_modes or profile.get("pending_sync_modes") or {"lists": True, "history": False, "resume": False}
    try:
        _profile_store.update_sync_status(profile_id, "Starting sync")
        service = SyncService(
            _config_from_profile(profile, dry_run=dry_run, sync_modes=modes),
            status_callback=lambda status: _profile_store.update_sync_status(profile_id, status),
            progress_callback=lambda results: _profile_store.update_sync_progress(profile_id, results),
            managed_lists=profile.get("managed_lists", []),
            cancel_requested_callback=lambda: _profile_store.is_sync_cancel_requested(profile_id),
            sync_modes=modes,
            resolution_cache=profile.get("resolution_cache", {}),
            failed_resolution_cache=profile.get("failed_resolution_cache", {}),
        )
        results = service.run()
        result_dicts = [_stats_to_dict(stats) for stats in results]
        _profile_store.record_sync_success(
            profile_id,
            result_dicts,
            dry_run=dry_run,
            managed_lists=service.managed_lists,
            sync_modes=modes,
            resolution_cache=service.resolution_cache,
            failed_resolution_cache=service.failed_resolution_cache,
        )
    except SyncCancelled:
        logger.info("Sync stopped for profile %s", profile_id[:8])
        _profile_store.record_sync_cancelled(profile_id, dry_run=dry_run, sync_modes=modes)
    except Exception as exc:  # pragma: no cover - exercised in integration use
        logger.exception("Sync failed for profile %s", profile_id[:8])
        _profile_store.record_sync_error(profile_id, str(exc), dry_run=dry_run, sync_modes=modes)


def _find_managed_list(profile: dict, list_name: str) -> dict | None:
    for item in profile.get("managed_lists", []):
        if str(item.get("list_name", "")).strip() == list_name:
            return item
    return None


def _remove_trakt_selected_list(selected_lists: list[dict], selection: dict) -> list[dict]:
    user = str(selection.get("user", "")).strip().lower()
    slug = str(selection.get("slug", "")).strip().lower()
    source = str(selection.get("list_source", "")).strip().lower()
    name = str(selection.get("name", "")).strip()

    remaining = []
    for item in selected_lists:
        item_user = str(item.get("user", "")).strip().lower()
        item_slug = str(item.get("slug", "")).strip().lower()
        item_source = str(item.get("source", "")).strip().lower()
        if user and slug and item_user == user and item_slug == slug:
            continue
        if source and name and item_source == source and str(item.get("name", "")).strip() == name:
            continue
        remaining.append(item)
    return remaining


def _display_status_label(display_name: str) -> str:
    return str(display_name or "").split(" - ", 1)[0].strip().lower()


def _remove_managed_selection(profile: dict, managed_entry: dict) -> dict:
    credentials = normalize_credentials(profile.get("credentials"))
    selection = managed_entry.get("selection") if isinstance(managed_entry.get("selection"), dict) else {}
    source = str(selection.get("source", "")).strip().lower()

    if source == "simkl":
        media_type = str(selection.get("media_type", "")).strip().lower()
        status = str(selection.get("status", "")).strip()
        if media_type in credentials["simkl"]["selected_statuses"]:
            credentials["simkl"]["selected_statuses"][media_type] = [
                item for item in credentials["simkl"]["selected_statuses"][media_type]
                if item != status
            ]
        return credentials

    if source == "anilist":
        status = str(selection.get("status", "")).strip()
        credentials["anilist"]["selected_statuses"] = [
            item for item in credentials["anilist"]["selected_statuses"]
            if item != status
        ]
        return credentials

    if source == "trakt":
        kind = str(selection.get("kind", "")).strip().lower()
        if kind == "watchlist":
            media_type = str(selection.get("media_type", "")).strip().lower()
            if media_type == "movies":
                credentials["trakt"]["sync_watchlist_movies"] = False
            elif media_type == "shows":
                credentials["trakt"]["sync_watchlist_shows"] = False
            else:
                credentials["trakt"]["sync_watchlist_movies"] = False
                credentials["trakt"]["sync_watchlist_shows"] = False
            credentials["trakt"]["sync_watchlist"] = (
                credentials["trakt"]["sync_watchlist_movies"] or credentials["trakt"]["sync_watchlist_shows"]
            )
            return credentials
        if kind == "default":
            catalog_key = str(selection.get("catalog_key", "")).strip()
            name = str(selection.get("name", "")).strip()
            credentials["trakt"]["selected_lists"] = [
                item for item in credentials["trakt"]["selected_lists"]
                if not (
                    str(item.get("source", "")).strip().lower() == "default"
                    and (
                        (catalog_key and str(item.get("catalog_key", "")).strip() == catalog_key)
                        or (name and str(item.get("name", "")).strip() == name)
                    )
                )
            ]
            return credentials
        if kind == "selected-list":
            credentials["trakt"]["selected_lists"] = _remove_trakt_selected_list(
                credentials["trakt"]["selected_lists"],
                selection,
            )
            return credentials
        if kind == "liked-auto":
            trakt_config = TraktConfig(
                client_id=credentials["trakt"]["client_id"],
                client_secret=credentials["trakt"]["client_secret"],
                access_token=credentials["trakt"]["access_token"],
                refresh_token=credentials["trakt"]["refresh_token"],
                username=credentials["trakt"]["username"],
            )
            liked_lists = TraktClient(trakt_config).get_liked_lists_metadata()
            remaining_liked = [
                item for item in liked_lists
                if not (
                    str(item.get("user", "")).strip().lower() == str(selection.get("user", "")).strip().lower()
                    and str(item.get("slug", "")).strip().lower() == str(selection.get("slug", "")).strip().lower()
                )
            ]
            existing_non_liked = [
                item for item in credentials["trakt"]["selected_lists"]
                if str(item.get("source", "")).strip().lower() != "liked"
            ]
            credentials["trakt"]["sync_liked_lists"] = False
            credentials["trakt"]["selected_lists"] = existing_non_liked + remaining_liked
            return credentials

    if source == "mdblist":
        list_id = str(selection.get("id", "")).strip()
        mediatype = str(selection.get("mediatype", "")).strip().lower()
        credentials["mdblist"]["selected_lists"] = [
            item for item in credentials["mdblist"]["selected_lists"]
            if not (
                str(item.get("id", "")).strip() == list_id
                and str(item.get("mediatype", "")).strip().lower() == mediatype
            )
        ]
        return credentials

    # Fallbacks for older managed-list records without selection metadata.
    source_name = str(managed_entry.get("source_name", "")).strip()
    display_name = str(managed_entry.get("display_name", "")).strip()
    if source_name == "SIMKL":
        status = SIMKL_STATUS_BY_LABEL.get(_display_status_label(display_name), "")
        if status:
            for media_type, statuses in credentials["simkl"]["selected_statuses"].items():
                credentials["simkl"]["selected_statuses"][media_type] = [
                    item for item in statuses
                    if item != status
                ]
    elif source_name == "AniList":
        status = ANILIST_STATUS_BY_LABEL.get(_display_status_label(display_name), "")
        credentials["anilist"]["selected_statuses"] = [
            item for item in credentials["anilist"]["selected_statuses"]
            if item != status
        ]
    elif source_name == "MDBList":
        credentials["mdblist"]["selected_lists"] = [
            item for item in credentials["mdblist"]["selected_lists"]
            if str(item.get("name", "")).strip() != display_name
        ]
    elif source_name.startswith("Trakt"):
        if display_name == _status_list_name("movies", "watchlist"):
            credentials["trakt"]["sync_watchlist_movies"] = False
            credentials["trakt"]["sync_watchlist"] = credentials["trakt"]["sync_watchlist_shows"]
        elif display_name == _status_list_name("shows", "watchlist"):
            credentials["trakt"]["sync_watchlist_shows"] = False
            credentials["trakt"]["sync_watchlist"] = credentials["trakt"]["sync_watchlist_movies"]
        else:
            credentials["trakt"]["selected_lists"] = [
                item for item in credentials["trakt"]["selected_lists"]
                if str(item.get("name", "")).strip() != display_name
            ]
    return credentials


def _remove_matching_list_name(credentials: dict, list_name: str) -> dict:
    target = str(list_name).strip()
    if not target:
        return credentials

    for media_type, statuses in credentials["simkl"]["selected_statuses"].items():
        credentials["simkl"]["selected_statuses"][media_type] = [
            status for status in statuses
            if _status_list_name(media_type, status) != target
        ]

    credentials["anilist"]["selected_statuses"] = [
        status for status in credentials["anilist"]["selected_statuses"]
        if _status_list_name("anime", status) != target
    ]

    if target in {
        _status_list_name("shows", "watchlist"),
        _status_list_name("movies", "watchlist"),
    }:
        if target == _status_list_name("movies", "watchlist"):
            credentials["trakt"]["sync_watchlist_movies"] = False
        if target == _status_list_name("shows", "watchlist"):
            credentials["trakt"]["sync_watchlist_shows"] = False
        credentials["trakt"]["sync_watchlist"] = (
            credentials["trakt"]["sync_watchlist_movies"] or credentials["trakt"]["sync_watchlist_shows"]
        )

    credentials["trakt"]["selected_lists"] = [
        item for item in credentials["trakt"]["selected_lists"]
        if str(item.get("name", "")).strip() != target
    ]

    credentials["mdblist"]["selected_lists"] = [
        item for item in credentials["mdblist"]["selected_lists"]
        if str(item.get("name", "")).strip() != target
    ]

    return credentials


class ProfileScheduler:
    """Polls stored profiles and runs due syncs in background threads."""

    def __init__(self, store: ProfileStore, poll_seconds: int = SCHEDULER_POLL_SECONDS):
        self._store = store
        self._poll_seconds = poll_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="profile-scheduler", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            for profile in self._store.claim_due_profiles():
                threading.Thread(
                    target=_run_profile_sync,
                    args=(profile, False, profile.get("pending_sync_modes")),
                    name=f"sync-{profile['profile_id'][:8]}",
                    daemon=True,
                ).start()
            self._stop.wait(self._poll_seconds)


_scheduler = ProfileScheduler(_profile_store)


def _ensure_scheduler_started() -> None:
    global _scheduler_started
    if os.getenv("DISABLE_PROFILE_SCHEDULER") == "1":
        return
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler.start()
        _scheduler_started = True


@app.before_request
def _before_request() -> None:
    _ensure_scheduler_started()
    if not SITE_ACCESS_PASSWORD:
        return
    # Profile login and creation must always be reachable so users can
    # authenticate before they have a site-access cookie.
    allowed_paths = {"/access", "/api/profile/login", "/api/profile/save"}
    if request.path in allowed_paths or request.path.startswith("/static/"):
        return
    if _has_site_access():
        return
    if request.path.startswith("/api/"):
        # Return 401 without clearing the cookie — the cookie may still be valid
        # for other requests and clearing it would cascade-lock the user out.
        return make_response(jsonify({"error": "Site password required"}), 401)
    return _clear_access_cookie(make_response(render_template("access.html", error=None), 401))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/access", methods=["GET", "POST"])
def access():
    if not SITE_ACCESS_PASSWORD:
        return redirect(url_for("index"))
    if _has_site_access():
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        body = request.form if request.form else (request.get_json(silent=True) or {})
        password = str(body.get("password", "")).strip()
        client_key = _request_client_key()

        if _access_limiter.is_limited(client_key):
            error = "Too many access attempts. Please wait and try again."
        elif password != SITE_ACCESS_PASSWORD:
            _access_limiter.record_failure(client_key)
            error = "Wrong site password."
        else:
            _access_limiter.clear(client_key)
            access_token = _access_store.create("site-access")
            response = redirect(url_for("index"))
            return _with_access_cookie(response, access_token)

    return render_template("access.html", error=error)


@app.route("/api/profile/login", methods=["POST"])
def api_profile_login():
    body = request.get_json(silent=True) or {}
    profile_id = body.get("profile_id", "")
    password = body.get("password", "")
    client_key = _request_client_key()

    if _login_limiter.is_limited(client_key):
        return _json_error("Too many login attempts. Please wait and try again.", 429)

    try:
        profile = _profile_store.get_profile(profile_id, password, include_credentials=True)
    except KeyError:
        _login_limiter.record_failure(client_key)
        return _json_error("Profile not found", 404)
    except PermissionError:
        _login_limiter.record_failure(client_key)
        return _json_error("Invalid profile password", 401)
    except ValueError as exc:
        _login_limiter.record_failure(client_key)
        return _json_error(str(exc), 400)

    _login_limiter.clear(client_key)
    session_token = _session_store.create(profile["profile_id"])
    return _with_session_cookie(_profile_response(profile, include_credentials=True), session_token)


@app.route("/api/profile/logout", methods=["POST"])
def api_profile_logout():
    _session_store.destroy(_session_token())
    return _clear_session_cookie(make_response(jsonify({"status": "logged_out"})))


@app.route("/api/profile/delete", methods=["POST"])
def api_profile_delete():
    body = request.get_json(silent=True) or {}
    confirm_text = str(body.get("confirm_text", "")).strip().upper()
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    if confirm_text != "DELETE":
        return _json_error("Type DELETE to confirm profile deletion", 400)

    try:
        _profile_store.delete_profile_by_id(profile_id)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    except RuntimeError as exc:
        return _json_error(str(exc), 409)

    _session_store.destroy(_session_token())
    return _clear_session_cookie(make_response(jsonify({"status": "deleted"})))


@app.route("/api/simkl/pin/start", methods=["POST"])
def api_simkl_pin_start():
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    client_id = str(body.get("client_id", "")).strip()
    if not client_id and private_profile:
        client_id = private_profile["credentials"]["simkl"]["client_id"]

    if not client_id:
        return _json_error("SIMKL client ID is required", 400)

    try:
        client = SimklClient(SimklConfig(client_id=client_id))
        pin_data = client.request_pin()
    except Exception as exc:
        logger.exception("Failed to start SIMKL PIN auth")
        return _json_error(f"Failed to start SIMKL auth: {exc}", 400)

    response = {
        "user_code": pin_data.get("user_code"),
        "verification_url": pin_data.get("verification_url"),
        "interval": pin_data.get("interval", 5),
        "expires_in": pin_data.get("expires_in", 900),
    }
    return jsonify(response)


@app.route("/api/simkl/pin/check", methods=["POST"])
def api_simkl_pin_check():
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    client_id = str(body.get("client_id", "")).strip()
    user_code = str(body.get("user_code", "")).strip()
    if not client_id and private_profile:
        client_id = private_profile["credentials"]["simkl"]["client_id"]

    if not client_id:
        return _json_error("SIMKL client ID is required", 400)
    if not user_code:
        return _json_error("SIMKL user code is required", 400)

    try:
        client = SimklClient(SimklConfig(client_id=client_id))
        check = client.check_pin(user_code) or {}
    except Exception as exc:
        logger.exception("Failed to check SIMKL PIN auth")
        return _json_error(f"Failed to check SIMKL auth: {exc}", 400)

    if check.get("result") == "OK" and check.get("access_token"):
        return jsonify({
            "status": "approved",
            "access_token": check["access_token"],
        })

    return jsonify({
        "status": "pending",
        "message": check.get("message", ""),
    })


@app.route("/api/trakt/device/start", methods=["POST"])
def api_trakt_device_start():
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    client_id = str(body.get("client_id", "")).strip()
    client_secret = str(body.get("client_secret", "")).strip()
    if private_profile:
        client_id = client_id or private_profile["credentials"]["trakt"]["client_id"]
        client_secret = client_secret or private_profile["credentials"]["trakt"]["client_secret"]

    if not client_id:
        return _json_error("Trakt client ID is required", 400)
    if not client_secret:
        return _json_error("Trakt client secret is required", 400)

    try:
        client = TraktClient(TraktConfig(client_id=client_id, client_secret=client_secret))
        data = client.request_device_code()
    except Exception as exc:
        logger.exception("Failed to start Trakt device auth")
        return _json_error(f"Failed to start Trakt auth: {exc}", 400)

    return jsonify({
        "device_code": data.get("device_code"),
        "user_code": data.get("user_code"),
        "verification_url": data.get("verification_url") or "https://trakt.tv/activate",
        "interval": data.get("interval", 5),
        "expires_in": data.get("expires_in", 600),
    })


@app.route("/api/trakt/device/check", methods=["POST"])
def api_trakt_device_check():
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    client_id = str(body.get("client_id", "")).strip()
    client_secret = str(body.get("client_secret", "")).strip()
    device_code = str(body.get("device_code", "")).strip()
    if private_profile:
        client_id = client_id or private_profile["credentials"]["trakt"]["client_id"]
        client_secret = client_secret or private_profile["credentials"]["trakt"]["client_secret"]

    if not client_id:
        return _json_error("Trakt client ID is required", 400)
    if not client_secret:
        return _json_error("Trakt client secret is required", 400)
    if not device_code:
        return _json_error("Trakt device code is required", 400)

    try:
        client = TraktClient(TraktConfig(client_id=client_id, client_secret=client_secret))
        data = client.poll_device_token(device_code) or {}
    except Exception as exc:
        response = getattr(exc, "response", None)
        payload = {}
        if response is not None and getattr(response, "text", ""):
            try:
                payload = response.json() or {}
            except ValueError:
                payload = {}

        error_code = str(payload.get("error", "")).strip().lower()
        if error_code in {"authorization_pending", "slow_down"}:
            return jsonify({
                "status": "pending",
                "message": payload.get("error_description") or payload.get("message") or payload.get("error") or "",
            })
        if error_code in {"expired_token", "access_denied"}:
            return jsonify({
                "status": "failed",
                "message": payload.get("error_description") or payload.get("message") or payload.get("error") or "",
            }), 400

        logger.exception("Failed to check Trakt device auth")
        return _json_error(f"Failed to check Trakt auth: {exc}", 400)

    if data.get("access_token"):
        return jsonify({
            "status": "approved",
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token", ""),
        })

    return jsonify({
        "status": "pending",
        "message": data.get("error", "") or data.get("message", ""),
    })


@app.route("/api/trakt/catalogs", methods=["POST"])
def api_trakt_catalogs():
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    client_id = str(body.get("client_id", "")).strip()
    access_token = str(body.get("access_token", "")).strip()
    query = str(body.get("query", "")).strip()
    if private_profile:
        client_id = client_id or private_profile["credentials"]["trakt"]["client_id"]
        access_token = access_token or private_profile["credentials"]["trakt"]["access_token"]

    if not client_id:
        return _json_error("Trakt client ID is required", 400)
    if not access_token:
        return _json_error("Trakt access token is required", 400)

    try:
        client = TraktClient(TraktConfig(client_id=client_id, access_token=access_token))
        if query:
            items = client.search_lists(query)
        else:
            items = client.get_personal_lists_metadata() + client.get_liked_lists_metadata()
    except Exception as exc:
        logger.exception("Failed to load Trakt catalogs")
        return _json_error(f"Failed to load Trakt catalogs: {exc}", 400)

    return jsonify({"items": items, "query": query})


@app.route("/api/mdblist/lists", methods=["POST"])
def api_mdblist_lists():
    body = request.get_json(silent=True) or {}
    private_profile = _current_private_profile()
    api_key = str(body.get("api_key", "")).strip()
    query = str(body.get("query", "")).strip()
    if not api_key and private_profile:
        api_key = private_profile["credentials"]["mdblist"]["api_key"]

    if not api_key:
        return _json_error("MDBList API key is required", 400)

    try:
        client = MdbListClient(MdbListConfig(api_key=api_key))
        items = client.search_public_lists(query) if query else client.get_user_lists()
    except Exception as exc:
        logger.exception("Failed to load MDBList lists")
        return _json_error(f"Failed to load MDBList lists: {exc}", 400)

    return jsonify({"items": items, "query": query})


@app.route("/api/profile/save", methods=["POST"])
def api_profile_save():
    body = request.get_json(silent=True) or {}
    credentials = body.get("credentials", {})
    options = body.get("options", {})
    password = body.get("password", "")
    profile_id = str(body.get("profile_id", "")).strip()
    session_profile_id = _current_profile_id()
    validation_credentials = credentials

    try:
        if session_profile_id:
            existing_profile = _profile_store.get_private_profile_by_id(session_profile_id)
            validation_credentials = merge_credentials(existing_profile.get("credentials"), credentials)
        elif profile_id and password:
            existing_profile = _profile_store.get_private_profile_by_id(profile_id)
            validation_credentials = merge_credentials(existing_profile.get("credentials"), credentials)
    except KeyError:
        pass

    _, errors = _validate_profile_configuration(validation_credentials, options)
    if errors:
        return _json_error("Configuration errors", 400, errors)

    try:
        if session_profile_id:
            if profile_id and profile_id != session_profile_id:
                return _json_error("You are already signed into a different profile", 409)
            profile = _profile_store.update_profile_by_id(session_profile_id, credentials, options)
            created = False
            session_token = _session_token()
        elif profile_id:
            profile = _profile_store.update_profile(profile_id, password, credentials, options)
            created = False
            session_token = _session_store.create(profile["profile_id"])
        else:
            profile = _profile_store.create_profile(password, credentials, options)
            created = True
            session_token = _session_store.create(profile["profile_id"])
    except KeyError:
        return _json_error("Profile not found", 404)
    except PermissionError:
        return _json_error("Invalid profile password", 401)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    response = {"profile": profile, "created": created}
    return _with_session_cookie(make_response(jsonify(response)), session_token)


@app.route("/api/profile/status", methods=["POST"])
def api_profile_status():
    body = request.get_json(silent=True) or {}
    include_credentials = bool(body.get("include_credentials", False))
    profile = _current_public_profile(include_credentials=include_credentials)
    if not profile:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401

    return _profile_response(profile, include_credentials=include_credentials)


@app.route("/api/profile/list/delete", methods=["POST"])
def api_profile_list_delete():
    body = request.get_json(silent=True) or {}
    list_name = str(body.get("list_name", "")).strip()
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    if not list_name:
        return _json_error("List name is required", 400)

    profile = _current_private_profile()
    if not profile:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    if profile.get("sync_running"):
        return _json_error("Wait for the current sync to finish before deleting a list", 409)

    managed_entry = _find_managed_list(profile, list_name)
    if not managed_entry:
        return _json_error("Managed list not found", 404)

    try:
        updated_credentials = _remove_managed_selection(profile, managed_entry)
        updated_credentials = _remove_matching_list_name(updated_credentials, list_name)
        pmdb_client = PublicMetaDBClient(_config_from_profile(profile).pmdb)
        list_id = str(managed_entry.get("list_id", "")).strip()
        if list_id:
            pmdb_client.delete_list(list_id)
        else:
            existing = pmdb_client.find_list_by_name(list_name)
            if existing:
                pmdb_client.delete_list(str(existing.get("id", "")).strip())
        updated_profile = _profile_store.delete_managed_list_by_id(profile_id, list_name, updated_credentials)
    except Exception as exc:
        logger.exception("Failed to delete managed list %s for profile %s", list_name, profile_id[:8])
        return _json_error(str(exc), 500)

    return _profile_response(updated_profile, include_credentials=True)


@app.route("/api/profile/sync", methods=["POST"])
def api_profile_sync():
    body = request.get_json(silent=True) or {}
    dry_run = bool(body.get("dry_run", False))
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401

    try:
        profile = _profile_store.claim_profile_for_sync_by_id(profile_id, sync_modes={
            "lists": True,
            "history": False,
            "resume": False,
        })
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    except RuntimeError:
        return _json_error("Sync already in progress", 409)

    thread = threading.Thread(
        target=_run_profile_sync,
        args=(profile, dry_run, {"lists": True, "history": False, "resume": False}),
        name=f"manual-sync-{profile['profile_id'][:8]}",
        daemon=True,
    )
    thread.start()
    return jsonify({"status": "started", "dry_run": dry_run})


@app.route("/api/profile/activity/sync", methods=["POST"])
def api_profile_activity_sync():
    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode", "")).strip().lower()
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    if mode not in {"history", "resume"}:
        return _json_error("Activity sync mode must be 'history' or 'resume'", 400)

    profile = _current_private_profile()
    if not profile:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404

    options = normalize_profile_options(profile.get("options"))
    if mode == "history" and options["activity_history_source"] == "off":
        return _json_error("Select a watch history source in Settings first", 409)
    if mode == "resume" and options["activity_resume_source"] == "off":
        return _json_error("Select a resume progress source in Settings first", 409)

    sync_modes = {
        "lists": False,
        "history": mode == "history",
        "resume": mode == "resume",
    }

    try:
        claimed = _profile_store.claim_profile_for_sync_by_id(profile_id, sync_modes=sync_modes)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    except RuntimeError:
        return _json_error("Sync already in progress", 409)

    thread = threading.Thread(
        target=_run_profile_sync,
        args=(claimed, False, sync_modes),
        name=f"activity-sync-{mode}-{claimed['profile_id'][:8]}",
        daemon=True,
    )
    thread.start()
    return jsonify({"status": "started", "mode": mode})


@app.route("/api/profile/activity/history/clear", methods=["POST"])
def api_profile_activity_history_clear():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401

    profile = _current_private_profile()
    if not profile:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    if profile.get("sync_running"):
        return _json_error("Wait for the current sync to finish before clearing watch history", 409)

    credentials = normalize_credentials(profile.get("credentials"))
    if not credentials["pmdb"]["api_key"]:
        return _json_error("Save your PublicMetaDB API key first", 409)

    try:
        deleted_count = PublicMetaDBClient(_config_from_profile(profile).pmdb).clear_watched_history()
    except Exception as exc:
        logger.exception("Failed to clear PublicMetaDB watch history for profile %s", profile_id[:8])
        return _json_error(str(exc), 500)

    try:
        profile = _profile_store.reset_history_import_state_by_id(profile_id)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404

    return jsonify({"status": "cleared", "deleted_count": deleted_count, "profile": profile})


@app.route("/api/profile/sync/stop", methods=["POST"])
def api_profile_sync_stop():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401

    try:
        profile = _profile_store.request_sync_cancel(profile_id)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    except RuntimeError as exc:
        return _json_error(str(exc), 409)

    return jsonify({"status": "stopping", "profile": profile})


@app.route("/api/profile/unresolved", methods=["POST"])
def api_profile_unresolved():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    try:
        items = _profile_store.get_unresolved_items(profile_id)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    return jsonify({"items": items})


@app.route("/api/profile/unresolved/resolve", methods=["POST"])
def api_profile_unresolved_resolve():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    body = request.get_json(silent=True) or {}
    cache_key = str(body.get("cache_key", "")).strip()
    tmdb_id_raw = body.get("tmdb_id")
    if not cache_key:
        return _json_error("cache_key is required", 400)
    try:
        tmdb_id = int(tmdb_id_raw)
        if tmdb_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return _json_error("tmdb_id must be a positive integer", 400)
    try:
        remaining = _profile_store.resolve_item_manually(profile_id, cache_key, tmdb_id)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    return jsonify({"status": "resolved", "items": remaining})


@app.route("/api/profile/unresolved/dismiss", methods=["POST"])
def api_profile_unresolved_dismiss():
    profile_id = _current_profile_id()
    if not profile_id:
        return _clear_session_cookie(_json_error("Sign in first", 401)[0]), 401
    body = request.get_json(silent=True) or {}
    cache_key = str(body.get("cache_key", "")).strip()
    if not cache_key:
        return _json_error("cache_key is required", 400)
    try:
        remaining = _profile_store.dismiss_unresolved_item(profile_id, cache_key)
    except KeyError:
        return _clear_session_cookie(_json_error("Profile not found", 404)[0]), 404
    return jsonify({"status": "dismissed", "items": remaining})


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync dashboard web UI")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug)
