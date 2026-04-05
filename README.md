# SIMKL → PublicMetaDB List Sync

One-way sync of your **Watching** and **Plan to Watch** lists from [SIMKL](https://simkl.com) to [PublicMetaDB](https://publicmetadb.com).

## Features

- Creates **6 separate private lists** in PublicMetaDB:
  - SIMKL – Series – Watching
  - SIMKL – Series – Plan to Watch
  - SIMKL – Movies – Watching
  - SIMKL – Movies – Plan to Watch
  - SIMKL – Anime – Watching
  - SIMKL – Anime – Plan to Watch
- Matches items by TMDB ID (direct) → IMDB → MAL → AniDB → TVDB (lookup chain)
- Avoids duplicates — only adds items not already in the PMDB list
- Optional removal of items no longer in SIMKL (`--remove-missing`)
- Dry-run mode to preview changes
- Interval mode for continuous sync
- Rate limiting with automatic backoff
- Retries on transient failures (429, 5xx)

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Get API credentials

**SIMKL:**
1. Go to https://simkl.com/settings/developer/
2. Create an app → note the **Client ID** and **Client Secret**

**PublicMetaDB:**
1. Go to https://publicmetadb.com/api-docs
2. Create an API key (format: `pm-...`)

### 3. Configure

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Or use a JSON config file (see `config.example.json`).

### 4. Authenticate with SIMKL

```bash
python main.py auth
```

This opens a browser for PIN-based OAuth. Enter the code, then copy the access token into your `.env` file.

## Usage

### One-time sync

```bash
python main.py sync
```

### Dry run (preview only)

```bash
python main.py sync --dry-run
```

### Remove items no longer in SIMKL

```bash
python main.py sync --remove-missing
```

### Continuous sync (every 30 minutes)

```bash
python main.py sync --interval 30
```

### Verbose logging

```bash
python main.py -v sync
```

### Using a config file

```bash
python main.py -c config.json sync
```

## Cron setup

To run every 6 hours via cron:

```cron
0 */6 * * * cd /path/to/simkl_list_to_pmdb && /path/to/python main.py sync >> sync.log 2>&1
```

On Windows Task Scheduler, create a task that runs:
```
python C:\path\to\simkl_list_to_pmdb\main.py sync
```

## Project structure

```
├── main.py                  # CLI entry point
├── src/
│   ├── config.py            # Configuration loading and validation
│   ├── simkl_client.py      # SIMKL API client (OAuth, watchlist fetch)
│   ├── publicmetadb_client.py  # PublicMetaDB API client (lists, items, mappings)
│   ├── matcher.py           # TMDB ID resolution (TMDB → IMDB → MAL → AniDB → TVDB)
│   └── sync_service.py      # Sync orchestration logic
├── .env.example             # Environment variable template
├── config.example.json      # JSON config template
└── requirements.txt         # Python dependencies
```

## How it works

1. Fetches "Watching" and "Plan to Watch" items from SIMKL, grouped by media type (shows, movies, anime)
2. Resolves each item to a TMDB ID via a lookup chain: direct TMDB → IMDB → MAL → AniDB ��� TVDB
3. Creates/finds 6 separate **private** lists in PublicMetaDB (one per media type × status)
4. Adds any items not already present in each PMDB list
5. Optionally removes items from PMDB that are no longer in the SIMKL list

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `SIMKL_CLIENT_ID` | Yes | SIMKL app client ID |
| `SIMKL_CLIENT_SECRET` | No | SIMKL app client secret |
| `SIMKL_ACCESS_TOKEN` | Yes | OAuth access token (get via `python main.py auth`) |
| `PMDB_API_KEY` | Yes | PublicMetaDB API key (`pm-...`) |
| `SYNC_REMOVE_MISSING` | No | Remove stale items from PMDB (default: `false`) |
| `SYNC_DRY_RUN` | No | Preview mode (default: `false`) |
| `SYNC_INTERVAL_MINUTES` | No | Repeat interval in minutes (default: `0` = once) |
| `SYNC_MEDIA_TYPES` | No | Comma-separated types (default: `shows,movies,anime`) |
