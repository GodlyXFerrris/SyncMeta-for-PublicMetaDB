# SyncMeta for PublicMetaDB

SyncMeta is a self-hosted web app that syncs your watchlists and watch history from:

- **SIMKL** — movies, shows, anime (including anime movies)
- **AniList** — anime lists
- **Trakt** — watchlists, liked lists, default catalogs, public lists
- **MDBList** — your lists and public list discovery

into [PublicMetaDB](https://publicmetadb.com).

Each user gets a persistent profile with encrypted credentials. The server keeps lists updated automatically in the background.

## Runtime

- Python 3.12+ is required for local development and test runs.
- The included Docker image already uses Python 3.12.

---

## Features

### List Sync
- Syncs selected watchlist statuses into named PublicMetaDB lists
- Supports movies, shows, and anime (TV and movies)
- When AniList is enabled, AniList handles all TV anime; SIMKL still syncs anime movies separately
- Concurrent SIMKL + AniList sync for faster runs
- Shared anime prequel-chain cache across providers — no duplicate AniList API calls
- Removes items no longer in the source (optional)
- Deletes SyncMeta-managed PMDB lists when you disable them (optional)
- Contributes resolved external ID mappings back to PMDB for the community
- Uses PMDB anime-season mappings for accurate multi-season episode remapping

### Watch History
- SIMKL watch history import
- Trakt watch history import
- Delta sync via cursor — only fetches new entries since the last run
- Bulk delete via PMDB's bulk delete endpoint (one API call per title, not per episode)

### Resume Progress
- Trakt playback progress sync

### Profile System
- UUID + password profile per user
- Credentials encrypted at rest
- Secret fields are overwrite-only — leaving blank keeps the existing value
- Rate-limited login and site-access

### Sync Controls
- Background auto-sync on a configurable interval (default 6 hours)
- Manual sync from the dashboard
- Stop button cancels the running sync at the next safe checkpoint
- Progress shown live per list

---

## Supported Sources

### SIMKL
- Movies, shows, and anime lists (TV series + anime movies separated)
- Status-based list selection: Watching, Plan to Watch, Completed, On Hold, Dropped
- PIN auth in the web UI
- Watch history import with delta cursor

### AniList
- Anime list sync by status: Watching, Completed, Planning, Paused, Dropped
- Public username (no token required)
- Prequel-chain walking to resolve sequels to their root series TMDB entry

### Trakt
- Watchlist (movies and/or shows)
- Default catalogs (Popular, Recommended, etc.)
- Liked lists
- Personal lists
- Public discover lists
- Device auth in the web UI
- Watch history import with delta cursor
- Resume progress sync

### MDBList
- My Lists from your account
- Public list search in the web UI
- Per-list selection

---

## Docker Setup

The included `docker-compose.yml` is the recommended deployment path.

```yaml
services:
  web:
    build: .
    ports:
      - "8080:8080"
    restart: unless-stopped
    environment:
      PROFILE_STORE_FILE: /app/data/profiles.json
      SYNCMETA_MASTER_KEY: ${SYNCMETA_MASTER_KEY:-}
    volumes:
      - ./data:/app/data
```

Start:

```bash
docker compose up -d --build web
```

Stop:

```bash
docker compose down
```

Then open `http://127.0.0.1:8080`.

---

## Quick Start

### 1. Start SyncMeta

```bash
docker compose up -d --build web
```

### 2. Create a profile

In the web UI:

- Enter a profile UUID (or generate one)
- Choose a password
- Save the profile

### 3. Connect your accounts

Enter whichever of these you use:

- **PublicMetaDB** API key
- **SIMKL** client ID + PIN auth
- **AniList** username
- **Trakt** client ID + device auth
- **MDBList** API key

### 4. Choose what to sync

Select statuses per source, for example:

- SIMKL `Watching` anime + `Completed` anime
- AniList `Watching` + `Completed`
- Trakt watchlist
- Selected MDBList lists

### 5. Use the dashboard

| Button | What it does |
|---|---|
| Sync Lists | Syncs all selected lists into PublicMetaDB |
| Dry Run Lists | Simulates the sync without writing anything |
| Sync Watch History | Imports watched history from your selected source |
| Sync Resume Progress | Imports Trakt playback progress |
| Stop | Cancels the running sync at the next checkpoint |

---

## Watch History Setup

1. Go to Settings
2. Set **Watch History Source** to `SIMKL` or `Trakt`
3. Save the profile
4. Press **Sync Watch History** on the dashboard

If you link SIMKL for the first time, the history source is automatically set to SIMKL.

Delta sync is used — only entries newer than the last run are fetched. To re-import everything, reset the cursor from the dashboard.

---

## Anime Notes

- When both SIMKL and AniList are enabled, **AniList handles all TV anime** and SIMKL handles anime movies. Both run concurrently.
- The prequel-chain cache is shared across providers. If AniList runs first, SIMKL benefits from the same cached chain data with no extra API calls.
- Anime episodes are remapped using PMDB's anime-season mappings when available, falling back to TMDB season data.
- SIMKL anime entries without a MAL ID and without a recognised `anime_type` are filtered out to prevent non-anime content from slipping in.

---

## Persistent Data

Keep these files intact across rebuilds:

- `./data/profiles.json` — all profile data
- `./data/profiles.key` — encryption key (if not using `SYNCMETA_MASTER_KEY`)

If you move hosts, bring both the `data` folder and the same encryption key.

---

## Updates

```bash
docker compose up -d --build web
```

Profile data and credentials are preserved as long as the `data` folder and encryption key are intact.

---

## Optional `.env`

Most deployments do not need a `.env` file. Create one only if you need to override defaults:

```bash
cp .env.example .env
```

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `PROFILE_STORE_FILE` | Path to the profile JSON store |
| `SYNCMETA_MASTER_KEY` | Encryption key for stored credentials |
| `SYNCMETA_MASTER_KEY_FILE` | Custom path for the generated key file |
| `SITE_ACCESS_PASSWORD` | Optional shared password before the app loads |
| `DISABLE_PROFILE_SCHEDULER` | Disables automatic background list syncing |
| `SYNCMETA_SESSION_TTL_SECONDS` | Browser session lifetime |
| `SYNCMETA_LOGIN_MAX_ATTEMPTS` | Login rate-limit max attempts |
| `SYNCMETA_LOGIN_WINDOW_SECONDS` | Login rate-limit window |
| `SYNCMETA_ACCESS_MAX_ATTEMPTS` | Site-access max attempts |
| `SYNCMETA_ACCESS_WINDOW_SECONDS` | Site-access rate-limit window |

---

## Security

- Profile passwords are hashed
- Source credentials are encrypted at rest
- Server-side sessions only — saved secrets are never sent back to the browser
- Rate-limited login and site-access attempts
- Secret fields are overwrite-only

---

## How List Names Work

SyncMeta creates PMDB lists with predictable names based on source and status:

| Source | Status | List name |
|---|---|---|
| SIMKL | Watching anime | `Watching - Anime` |
| SIMKL | Completed anime movies | `Completed - Anime Movies` |
| AniList | Completed | `Completed - Anime` |
| SIMKL | Plan to Watch movies | `Plan to Watch - Movies` |
| Trakt | Watchlist shows | `Watchlist - Series` |

If two sources would produce the same list name, SyncMeta keeps them separate.

---

## Local Run Without Docker

```bash
pip install -r requirements.txt
python web.py
```

Then open `http://127.0.0.1:8080`.

---

## Development

Run tests:

```bash
python -m unittest discover -v
```

---

## Project Layout

```
web.py                 Flask app and routes
src/
  sync_service.py      Sync orchestration
  matcher.py           External ID → TMDB resolution
  simkl_client.py      SIMKL API client
  anilist_client.py    AniList GraphQL client
  trakt_client.py      Trakt API client
  mdblist_client.py    MDBList API client
  publicmetadb_client.py  PublicMetaDB API client
  profile_store.py     Profile persistence and encryption
  config.py            Configuration dataclasses
templates/
  index.html           Single-page web UI
tests/
docker-compose.yml
Dockerfile
requirements.txt
```

---

## Notes

- Background auto-sync covers list syncing only
- Watch history and resume progress are triggered manually from the dashboard
- The scheduler runs inside the web process — one worker is expected
- Auto-sync interval defaults to 6 hours

---

## License

See `LICENSE`.
