# SyncMeta

SyncMeta keeps SIMKL, Trakt, and optionally AniList watchlists and lists synced into private PublicMetaDB lists.

It supports:

- A Flask web dashboard with persistent profiles
- UUID + password based profile login
- Background automatic syncing on the server
- Manual sync and dry run
- CLI usage for one-off or interval-based sync runs
- SIMKL for shows, movies, and anime
- Trakt for watchlist, liked lists, and selected public lists
- AniList for anime when you want AniList to replace SIMKL anime

## What It Syncs

By default, SyncMeta creates separate private PublicMetaDB lists for each source, media type, and status:

- `SIMKL - Series - Watching`
- `SIMKL - Series - Plan to Watch`
- `SIMKL - Movies - Watching`
- `SIMKL - Movies - Plan to Watch`
- `SIMKL - Anime - Watching`
- `SIMKL - Anime - Plan to Watch`
- `AniList - Anime - Watching`
- `AniList - Anime - Plan to Watch`
- `Trakt - Series - Watchlist`
- `Trakt - Movies - Watchlist`
- `Trakt List - username - list-name`

Anime behaves like this:

- If AniList is not configured, anime syncs from SIMKL.
- If AniList is configured, AniList replaces SIMKL for anime.

## How Matching Works

Items are resolved to TMDB IDs in this order:

1. Direct TMDB ID
2. IMDb
3. MAL
4. AniList
5. AniDB
6. TVDB
7. Root-series MAL or AniList fallback for anime sequels

That fallback helps map entries like later anime seasons back to the main series when needed.

## Web Dashboard

The web app now uses persistent server-side profiles instead of browser-only session state.

Each profile has:

- A generated UUID
- A password you choose
- Stored source credentials and sync settings
- Sync history and latest results
- Automatic background scheduling

Important:

- The server stores your source API credentials so it can continue syncing while your browser is closed.
- Profile passwords are hashed before storage.
- Automatic sync intervals have a minimum of `300` seconds.

### Web flow

1. Open the dashboard
2. Enter your API credentials
3. Choose media types and sync options
4. Set an update interval of at least `300` seconds
5. Save the profile
6. Keep the generated profile UUID and your password somewhere safe

After that, the server keeps syncing in the background based on that profile.

SIMKL access tokens can be obtained directly inside the Settings page through the built-in PIN auth helper.

Trakt also has a built-in device auth flow in Settings, plus a tabbed catalog picker:

- `Default` lets you enable your Trakt watchlist
- `My Lists` loads your liked Trakt lists so you can pick specific ones
- `Discover` searches public Trakt lists and lets you add selected ones

If you prefer the older behavior, you can still choose to sync all liked Trakt lists at once.

## CLI

The CLI is still available for manual or standalone use.

### Authenticate with SIMKL

```bash
python main.py auth
```

This starts the SIMKL PIN flow and prints an access token you can place in `.env` or a JSON config file.

### One-time sync

```bash
python main.py sync
```

### Dry run

```bash
python main.py sync --dry-run
```

### Remove items missing from the source

```bash
python main.py sync --remove-missing
```

### Continuous CLI sync every 30 minutes

```bash
python main.py sync --interval 30
```

### Verbose logging

```bash
python main.py -v sync
```

### Use a JSON config file

```bash
python main.py -c config.json sync
```

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Get API credentials

SIMKL:

1. Go to <https://simkl.com/settings/developer/>
2. Create an app
3. Copy the client ID and client secret

PublicMetaDB:

1. Go to <https://publicmetadb.com/api-docs>
2. Create an API key

AniList:

- Username is enough for public anime lists
- An access token is only needed for private AniList lists

### 3. Create a local `.env`

Copy `.env.example` to `.env` and fill in values if you want to use the CLI or containerized setup:

```bash
cp .env.example .env
```

You can also use `config.example.json` as a starting point for CLI config files.

## Running the Web App

### Local Flask

```bash
python web.py
```

Default address:

- `http://127.0.0.1:8080`

### Docker

```bash
docker compose up --build web
```

The included Docker setup runs the web app on port `8080`.

Notes:

- Profiles are stored in `data/profiles.json` by default.
- You can override that path with `PROFILE_STORE_FILE`.
- The background scheduler runs inside the web process.

## Environment Variables

These mainly matter for CLI use and deployment configuration.

### Source credentials

| Variable | Required | Description |
|---|---|---|
| `SIMKL_CLIENT_ID` | CLI: Yes | SIMKL app client ID |
| `SIMKL_CLIENT_SECRET` | No | SIMKL app client secret |
| `SIMKL_ACCESS_TOKEN` | CLI: Yes | SIMKL access token from `python main.py auth` |
| `ANILIST_USERNAME` | No | Enables AniList anime sync |
| `ANILIST_ACCESS_TOKEN` | No | Needed only for private AniList lists |
| `TRAKT_CLIENT_ID` | No | Trakt app client ID |
| `TRAKT_CLIENT_SECRET` | No | Required for Trakt device auth and syncing |
| `TRAKT_ACCESS_TOKEN` | No | Trakt access token from the web auth flow |
| `TRAKT_REFRESH_TOKEN` | No | Trakt refresh token |
| `PMDB_API_KEY` | CLI: Yes | PublicMetaDB API key |

### Sync options

| Variable | Required | Description |
|---|---|---|
| `SYNC_REMOVE_MISSING` | No | Remove stale items from PMDB |
| `SYNC_DRY_RUN` | No | Preview changes without writing |
| `SYNC_INTERVAL_MINUTES` | No | CLI repeat interval in minutes |
| `SYNC_MEDIA_TYPES` | No | Comma-separated media types such as `shows,movies,anime` |

### Web app options

| Variable | Required | Description |
|---|---|---|
| `PROFILE_STORE_FILE` | No | Path to the JSON profile store |
| `DISABLE_PROFILE_SCHEDULER` | No | Set to `1` to disable background scheduling |

## Project Structure

```text
main.py
web.py
src/
  config.py
  simkl_client.py
  anilist_client.py
  publicmetadb_client.py
  matcher.py
  sync_service.py
  profile_store.py
templates/
  index.html
tests/
  test_anilist_client.py
  test_matcher.py
  test_profile_store.py
```

## Development Notes

- PublicMetaDB requests use retry logic and rate limiting.
- Profile history keeps the latest sync snapshots.
- Dry runs are recorded in profile history but do not advance the automatic schedule.
- The scheduler checks for due profiles in the web process and starts sync jobs in background threads.

## Testing

Run the test suite with:

```bash
python -m unittest discover -v
```

## Current Limits

- Automatic syncing depends on the web process staying alive.
- If you run multiple web workers against the same profile store, you should think carefully about scheduler coordination.
- The current Docker setup uses a single web worker, which fits the built-in scheduler model well.
