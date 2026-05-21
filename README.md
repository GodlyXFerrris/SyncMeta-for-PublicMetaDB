# SyncMeta for PublicMetaDB

[![Deploy to Docker](https://github.com/Febsho/SyncMeta-for-PublicMetaDB/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Febsho/SyncMeta-for-PublicMetaDB/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Self-hosted web app that syncs watchlists, watch history, resume progress, and selected catalog lists from SIMKL, AniList, Trakt, and MDBList into [PublicMetaDB](https://publicmetadb.com).

## Features

- Multi-profile web dashboard with encrypted per-profile credentials.
- Core sync rules for PMDB watchlist, watched history, and resume progress.
- Catalog imports for SIMKL/AniList statuses, Trakt selected lists, and MDBList selected lists.
- Anime-aware mapping with Fribb anime-lists, Anime-Lists XML, AniList/MAL/SIMKL/TMDB IDs, manual overrides, and unresolved mapping review.
- Detailed sync diagnostics: latest results, row-level errors, failed/unresolved samples, timings, and last 25 detailed run records.
- Dry-run mode for previewing sync work before writing to PMDB.
- Admin dashboard with profile overview, queue state, API request counters, anime cache repair, and anime mapping refresh.
- Docker-first deployment with health checks and conservative defaults for small VPS hosts.

## Quick Start

```bash
git clone https://github.com/Febsho/SyncMeta-for-PublicMetaDB
cd SyncMeta-for-PublicMetaDB
cp .env.example .env
docker compose up -d syncmeta
```

Open `http://127.0.0.1:8080`.

1. Create or load a profile with a UUID and password.
2. Add a PublicMetaDB API key.
3. Connect SIMKL, AniList, Trakt, and/or MDBList.
4. Choose core sync rules and catalog imports.
5. Run a dry run first, then run a real sync.

## Scheduling Defaults

SyncMeta is intentionally conservative so it does not slow down other containers on small servers.

| Sync type | Default | Minimum | Notes |
|---|---:|---:|---|
| Lists/catalog imports | automatic every 12h | 6h | Per-profile deterministic jitter prevents all profiles from starting together. |
| Watch history | manual | 24h | Can be enabled for automatic background sync. |
| Resume progress | manual | 24h | Can be enabled for automatic background sync. |

Manual sync and dry-run buttons are still immediate.

## Environment Variables

Most API credentials and sync rules are configured per profile in the UI. Server-level settings belong in `.env`.

| Variable | Default | Description |
|---|---:|---|
| `PROFILE_STORE_FILE` | `/app/data/profiles.json` | JSON profile database path. Mount `/app/data` for persistence. |
| `SYNCMETA_MASTER_KEY` | generated | Fernet key for profile credentials. Keep stable across restarts. |
| `SYNCMETA_MASTER_KEY_FILE` | `/app/data/profiles.key` | File used when `SYNCMETA_MASTER_KEY` is empty. |
| `ADMIN_PASSWORD` | empty | Enables `/admin` when set. |
| `SITE_ACCESS_PASSWORD` | empty | Optional password gate before the app loads. |
| `SYNCMETA_MAX_CONCURRENT_SYNCS` | `1` | Number of profiles that may sync at the same time. |
| `SYNCMETA_SOURCE_SYNC_WORKERS` | `2` | Source fetch worker cap per sync. |
| `SYNCMETA_SIMKL_FETCH_WORKERS` | `2` | SIMKL status fetch worker cap. |
| `SYNCMETA_LIST_RESOLVE_WORKERS` | `2` | Mapping/resolve worker cap for list rows. |
| `SYNCMETA_LIST_WRITE_WORKERS` | `1` | PMDB list write worker cap. |
| `SYNCMETA_ACTIVITY_SOURCE_WORKERS` | `2` | History/resume source worker cap. |
| `SYNCMETA_ACTIVITY_WRITE_WORKERS` | `1` | History/resume PMDB write worker cap. |
| `SYNCMETA_PREWARM_WORKERS` | `2` | Anime prewarm worker cap. |
| `SYNCMETA_ANILIST_PREWARM_LIMIT` | `50` | Max AniList root-context prewarm items per run. Use `0` to disable. |
| `SYNCMETA_SCHEDULE_JITTER_SECONDS` | `900` | Default max jitter for automatic schedules. |
| `SYNCMETA_LIST_SYNC_JITTER_SECONDS` | schedule jitter | List-specific jitter override. |
| `SYNCMETA_HISTORY_SYNC_JITTER_SECONDS` | schedule jitter | History-specific jitter override. |
| `SYNCMETA_RESUME_SYNC_JITTER_SECONDS` | schedule jitter | Resume-specific jitter override. |
| `SYNCMETA_GUNICORN_WORKERS` | `1` | Gunicorn worker count. |
| `SYNCMETA_GUNICORN_THREADS` | `2` | Gunicorn thread count. |
| `SYNCMETA_GUNICORN_TIMEOUT` | `120` | Gunicorn request timeout. |
| `DISABLE_PROFILE_SCHEDULER` | `0` | Set to `1` to disable background automatic sync. |

## Oracle Free VPS Guidance

The checked-in `docker-compose.yml` is tuned for a small shared host:

- `SYNCMETA_MAX_CONCURRENT_SYNCS=1`
- PMDB writes limited to one worker by default
- Gunicorn limited to one worker and two threads
- Docker CPU/memory limits available through `.env`
- Automatic schedules staggered by deterministic jitter

Start with the defaults. If SyncMeta still competes with other containers, lower:

```env
SYNCMETA_CPU_LIMIT=0.5
SYNCMETA_MEMORY_LIMIT=1024m
SYNCMETA_ANILIST_PREWARM_LIMIT=0
```

## Sync Diagnostics

The dashboard keeps `/api/profile/status` lightweight for polling. Detailed error data is loaded only when needed:

- `POST /api/profile/sync/runs` returns recent detailed run summaries.
- `POST /api/profile/sync/run-details` returns one run with row diagnostics.

Run details include sanitized errors, row type, provider/list name, unresolved reasons, failed title samples, timing counters, and PMDB metrics.

## Docker Verification

```bash
docker compose config
docker compose pull
docker compose up -d syncmeta
docker compose ps
docker compose logs -f syncmeta
curl http://127.0.0.1:8080/healthz
```

Expected health response:

```json
{"ok":true}
```

## Development

```bash
pip install -r requirements.txt
python web.py
python -m unittest discover -v
python -m compileall src web.py
```

The app runs at `http://127.0.0.1:8080`.

## Troubleshooting

- High CPU: keep one concurrent sync, increase list/history/resume intervals, lower `SYNCMETA_ANILIST_PREWARM_LIMIT`, and keep PMDB write workers at `1`.
- Bad anime mappings: use the dashboard unresolved mapping tools or `/admin` anime cache repair actions.
- Stale Fribb/Anime-Lists data: use `/admin` -> `Update Anime Lists`; SyncMeta uses ETag-aware refresh and preserves current data if refresh fails.
- Expired tokens: reconnect the affected provider in the Connections area.
- PMDB write errors: open Latest Sync Results -> Details or Sync History -> Details to inspect sanitized row errors.

## License

MIT. See [LICENSE](LICENSE).
