"""Flask web UI for persistent SIMKL/AniList -> PublicMetaDB sync profiles."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from src.config import (
    AniListConfig,
    AppConfig,
    PublicMetaDBConfig,
    SimklConfig,
    SyncConfig,
    validate_config,
)
from src.profile_store import ProfileStore, normalize_credentials, normalize_profile_options
from src.sync_service import SyncService, SyncStats

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

_profile_store = ProfileStore(PROFILE_STORE_FILE)
_scheduler_lock = threading.Lock()
_scheduler_started = False


def _stats_to_dict(stats: SyncStats) -> dict:
    data = asdict(stats)
    data["error_count"] = len(data.pop("errors", []))
    return data


def _config_from_profile(profile: dict, dry_run: bool = False) -> AppConfig:
    credentials = normalize_credentials(profile.get("credentials"))
    options = normalize_profile_options(profile.get("options"))
    anilist_username = credentials["anilist"]["username"]

    return AppConfig(
        simkl=SimklConfig(
            client_id=credentials["simkl"]["client_id"],
            client_secret=credentials["simkl"]["client_secret"],
            access_token=credentials["simkl"]["access_token"],
        ),
        anilist=AniListConfig(
            username=anilist_username,
            access_token=credentials["anilist"]["access_token"],
            enabled=bool(anilist_username),
        ),
        pmdb=PublicMetaDBConfig(api_key=credentials["pmdb"]["api_key"]),
        sync=SyncConfig(
            remove_missing=options["remove_missing"],
            dry_run=dry_run,
            media_types=options["media_types"],
        ),
    )


def _configured_sources(config: AppConfig) -> list[str]:
    sources = []
    if config.simkl.client_id and config.simkl.access_token:
        sources.append("simkl")
    if config.anilist.enabled:
        sources.append("anilist")
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
        return None, ["Configure at least one source (SIMKL or AniList)"]

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


def _run_profile_sync(profile: dict, dry_run: bool = False) -> None:
    profile_id = profile["profile_id"]
    try:
        service = SyncService(_config_from_profile(profile, dry_run=dry_run))
        results = service.run()
        result_dicts = [_stats_to_dict(stats) for stats in results]
        _profile_store.record_sync_success(profile_id, result_dicts, dry_run=dry_run)
    except Exception as exc:  # pragma: no cover - exercised in integration use
        logger.exception("Sync failed for profile %s", profile_id[:8])
        _profile_store.record_sync_error(profile_id, str(exc), dry_run=dry_run)


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
                    args=(profile, False),
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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/profile/login", methods=["POST"])
def api_profile_login():
    body = request.get_json(silent=True) or {}
    profile_id = body.get("profile_id", "")
    password = body.get("password", "")

    try:
        profile = _profile_store.get_profile(profile_id, password, include_credentials=True)
    except KeyError:
        return _json_error("Profile not found", 404)
    except PermissionError:
        return _json_error("Invalid profile password", 401)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    return _profile_response(profile, include_credentials=True)


@app.route("/api/profile/save", methods=["POST"])
def api_profile_save():
    body = request.get_json(silent=True) or {}
    credentials = body.get("credentials", {})
    options = body.get("options", {})
    password = body.get("password", "")
    profile_id = str(body.get("profile_id", "")).strip()

    _, errors = _validate_profile_configuration(credentials, options)
    if errors:
        return _json_error("Configuration errors", 400, errors)

    try:
        if profile_id:
            profile = _profile_store.update_profile(profile_id, password, credentials, options)
            created = False
        else:
            profile = _profile_store.create_profile(password, credentials, options)
            created = True
    except KeyError:
        return _json_error("Profile not found", 404)
    except PermissionError:
        return _json_error("Invalid profile password", 401)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    response = {"profile": profile, "created": created}
    return jsonify(response)


@app.route("/api/profile/status", methods=["POST"])
def api_profile_status():
    body = request.get_json(silent=True) or {}
    profile_id = body.get("profile_id", "")
    password = body.get("password", "")

    try:
        profile = _profile_store.get_profile(profile_id, password, include_credentials=False)
    except KeyError:
        return _json_error("Profile not found", 404)
    except PermissionError:
        return _json_error("Invalid profile password", 401)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    return _profile_response(profile, include_credentials=False)


@app.route("/api/profile/sync", methods=["POST"])
def api_profile_sync():
    body = request.get_json(silent=True) or {}
    profile_id = body.get("profile_id", "")
    password = body.get("password", "")
    dry_run = bool(body.get("dry_run", False))

    try:
        profile = _profile_store.claim_profile_for_sync(profile_id, password)
    except KeyError:
        return _json_error("Profile not found", 404)
    except PermissionError:
        return _json_error("Invalid profile password", 401)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except RuntimeError:
        return _json_error("Sync already in progress", 409)

    thread = threading.Thread(
        target=_run_profile_sync,
        args=(profile, dry_run),
        name=f"manual-sync-{profile['profile_id'][:8]}",
        daemon=True,
    )
    thread.start()
    return jsonify({"status": "started", "dry_run": dry_run})


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync dashboard web UI")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug)
