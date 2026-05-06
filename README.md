# SyncMeta for PublicMetaDB

[![Deploy to Docker](https://github.com/Febsho/SyncMeta-for-PublicMetaDB/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Febsho/SyncMeta-for-PublicMetaDB/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Self-hosted web app that automatically syncs watchlists, watch history, and resume progress from SIMKL, AniList, Trakt, and MDBList into [PublicMetaDB](https://publicmetadb.com).

## Features

- **Multi-source sync** — Watchlists and history from SIMKL, AniList, Trakt, and MDBList in a single run
- **Watch history** — Imports completed titles from SIMKL and Trakt; Trakt entries at ≥ 80% progress are marked as watched automatically
- **Resume progress** — Saves Trakt playback progress as PMDB resume points
- **PMDB watchlist** — Merges plan-to-watch entries from multiple sources into the native PMDB watchlist
- **Anime support** — Prequel-chain cache, Fribb mapping, AniList/MAL resolution, and per-episode PMDB fallback
- **Multi-profile** — Each profile has its own AES-encrypted credentials and password
- **Admin panel** — Profile overview, manual syncs, and queue view (enabled via `ADMIN_PASSWORD`)
- **Background scheduler** — Automatic sync on a configurable interval

## Quick Start

```bash
git clone https://github.com/Febsho/SyncMeta-for-PublicMetaDB
cd SyncMeta-for-PublicMetaDB
cp .env.example .env   # adjust values as needed
docker compose up -d syncmeta
```

The app is available at `http://127.0.0.1:8080`.

1. **Create a profile** — Set a UUID and password in the web UI
2. **Connect sources** — Enter API keys for PublicMetaDB, SIMKL, AniList, Trakt, and/or MDBList
3. **Choose lists & history** — Enable status filters, history sync, and resume sync
4. **Run a sync** — Trigger manually from the dashboard or let the scheduler handle it

## Environment Variables

All sync settings (API keys, lists, history) are stored per-profile in the web UI. The following server-level variables are set once in `.env`:

| Variable | Required | Description |
|---|---|---|
| `SYNCMETA_MASTER_KEY` | Recommended | Encryption key for profile credentials. Keep this stable across restarts — losing it invalidates all saved credentials. Auto-generated if left empty. |
| `ADMIN_PASSWORD` | Optional | Enables the admin panel at `/admin`. The panel is fully disabled when this is not set. |
| `SITE_ACCESS_PASSWORD` | Optional | Global password gate shown before the app loads. Useful for public-facing deployments. |
| `SYNCMETA_MAX_CONCURRENT_SYNCS` | Optional | Maximum number of profile syncs running in parallel (default: `4`). |
| `PROFILE_STORE_FILE` | Optional | Path to the profile database file (default: `/app/data/profiles.json`). |
| `DISABLE_PROFILE_SCHEDULER` | Optional | Set to `1` to disable the background scheduler entirely. |

See `.env.example` for a full list with defaults and comments.

## Development

```bash
pip install -r requirements.txt
python web.py                        # http://127.0.0.1:8080
python -m unittest discover -v       # run tests
```

## License

MIT — see the `LICENSE` file for details.
