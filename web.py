"""Flask web UI for SIMKL/AniList → PublicMetaDB sync.

Credentials are sent per-request from the browser (stored in localStorage).
Nothing is persisted server-side — each user's sync state lives in memory
keyed by a client-generated session ID.
"""

import logging
import threading
from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime, timezone
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
from src.sync_service import SyncService, SyncStats

load_dotenv(Path(__file__).resolve().parent / ".env")

app = Flask(__name__, template_folder="templates", static_folder="static")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logger = logging.getLogger("web")

# ── Per-session state (max 200 sessions in memory) ────────────────
MAX_SESSIONS = 200
_sessions_lock = threading.Lock()
_sessions: OrderedDict[str, dict] = OrderedDict()


def _get_session(session_id: str) -> dict:
    with _sessions_lock:
        if session_id not in _sessions:
            _sessions[session_id] = {
                "last_sync": None,
                "last_results": [],
                "sync_running": False,
                "sync_error": None,
                "history": [],
            }
            # Evict oldest if over limit
            while len(_sessions) > MAX_SESSIONS:
                _sessions.popitem(last=False)
        else:
            _sessions.move_to_end(session_id)
        return _sessions[session_id]


def _config_from_body(body: dict) -> AppConfig:
    """Build an AppConfig entirely from the request JSON. No server-side env vars used for credentials."""
    creds = body.get("credentials", {})
    simkl = creds.get("simkl", {})
    anilist = creds.get("anilist", {})
    pmdb = creds.get("pmdb", {})
    opts = body.get("options", {})

    anilist_username = anilist.get("username", "")

    return AppConfig(
        simkl=SimklConfig(
            client_id=simkl.get("client_id", ""),
            client_secret=simkl.get("client_secret", ""),
            access_token=simkl.get("access_token", ""),
        ),
        anilist=AniListConfig(
            username=anilist_username,
            access_token=anilist.get("access_token", ""),
            enabled=bool(anilist_username),
        ),
        pmdb=PublicMetaDBConfig(
            api_key=pmdb.get("api_key", ""),
        ),
        sync=SyncConfig(
            remove_missing=opts.get("remove_missing", False),
            dry_run=opts.get("dry_run", False),
            media_types=opts.get("media_types", ["shows", "movies", "anime"]),
        ),
    )


def _stats_to_dict(stats: SyncStats) -> dict:
    d = asdict(stats)
    d["error_count"] = len(d.pop("errors", []))
    return d


def _run_sync(session_id: str, config: AppConfig) -> None:
    state = _get_session(session_id)
    try:
        service = SyncService(config)
        results = service.run()
        now = datetime.now(timezone.utc).isoformat()
        dicts = [_stats_to_dict(s) for s in results]
        with _sessions_lock:
            state["last_sync"] = now
            state["last_results"] = dicts
            state["sync_error"] = None
            state["history"].insert(0, {"timestamp": now, "results": dicts})
            state["history"] = state["history"][:20]
    except Exception as e:
        logger.exception("Sync failed for session %s", session_id[:8])
        with _sessions_lock:
            state["sync_error"] = str(e)
    finally:
        with _sessions_lock:
            state["sync_running"] = False


# ── Routes ────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    session_id = request.args.get("session_id", "")
    if not session_id:
        return jsonify({"error": "session_id required"}), 400
    state = _get_session(session_id)
    with _sessions_lock:
        return jsonify(state)


@app.route("/api/sync", methods=["POST"])
def api_sync():
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id", "")
    if not session_id:
        return jsonify({"error": "session_id required"}), 400

    state = _get_session(session_id)
    with _sessions_lock:
        if state["sync_running"]:
            return jsonify({"error": "Sync already in progress"}), 409

    config = _config_from_body(body)

    # Validate only the sources the user configured
    sources = []
    if config.simkl.client_id and config.simkl.access_token:
        sources.append("simkl")
    if config.anilist.enabled:
        sources.append("anilist")

    if not sources:
        return jsonify({"error": "Configure at least one source (SIMKL or AniList)"}), 400

    errors = validate_config(config, sources)
    if errors:
        return jsonify({"error": "Configuration errors", "details": errors}), 400

    with _sessions_lock:
        state["sync_running"] = True
        state["sync_error"] = None

    thread = threading.Thread(target=_run_sync, args=(session_id, config), daemon=True)
    thread.start()
    return jsonify({"status": "started"})


@app.route("/api/history")
def api_history():
    session_id = request.args.get("session_id", "")
    if not session_id:
        return jsonify({"error": "session_id required"}), 400
    state = _get_session(session_id)
    with _sessions_lock:
        return jsonify(state.get("history", []))


# ── Entrypoint ────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Sync dashboard web UI")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug)
