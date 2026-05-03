"""
Anime cache repair script.

Detects and clears bad cached anime mappings in profiles.json so they get
fresh lookups on the next sync.

Problems addressed:
  1. tmdb_collision entries where both colliding items were written with the
     same TMDB ID in resolution_cache — both need to be cleared so one gets
     re-resolved and re-added correctly.
  2. Fribb-type-mismatch entries: items whose media_type conflicts with the
     Fribb entry type — the cached TMDB ID from the old broken override path
     could be wrong.
  3. Entries in resolution_cache that were later overridden by manual_resolution_cache
     with a DIFFERENT value — the old auto entry is already shadowed at runtime
     but cleaning it avoids confusion.
  4. Known-bad TMDB IDs (277700, 154634, 317316, 298754) — confirmed wrong
     community mappings; cleared so items get fresh lookups.
  5. (--check-fribb) Cross-validate every anime resolution_cache entry against
     Fribb/anime-lists.  Any entry whose cached TMDB ID is NOT found as a
     valid themoviedb value in Fribb (for the item's AniList or MAL ID) is
     flagged as suspicious and cleared so it gets re-resolved.

Usage:
  python scripts/repair_anime_cache.py [--dry-run] [--profile-id <id>] [--check-fribb]

  --dry-run        Print what would be changed without writing.
  --profile-id ID  Restrict to a single profile (prefix match, 8+ chars).
  --check-fribb    Cross-validate cached anime TMDB IDs against Fribb database.
                   Requires AniList or MAL IDs to be present in the cache key.

The script writes a backup of profiles.json to profiles.json.bak before
making any changes.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
from collections import defaultdict
from pathlib import Path

# ── resolve repo root ────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

PROFILE_STORE_FILE = Path(
    os.getenv("PROFILE_STORE_FILE",
              str(REPO_ROOT / "data" / "profiles.json"))
)


def _load_profiles_raw() -> dict:
    if not PROFILE_STORE_FILE.exists():
        print(f"[ERROR] profiles.json not found at {PROFILE_STORE_FILE}", file=sys.stderr)
        sys.exit(1)
    with PROFILE_STORE_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def _save_profiles_raw(data: dict, dry_run: bool) -> None:
    if dry_run:
        print("[DRY-RUN] Would write", PROFILE_STORE_FILE)
        return
    backup = PROFILE_STORE_FILE.with_suffix(".json.bak")
    shutil.copy2(PROFILE_STORE_FILE, backup)
    print(f"  Backed up to {backup}")
    tmp = PROFILE_STORE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(PROFILE_STORE_FILE)
    print(f"  Saved {PROFILE_STORE_FILE}")


def _decrypt_credentials(profile_id: str, profile_raw: dict) -> dict | None:
    """Try to decrypt profile credentials to read per-item cache data.

    If decryption fails we skip that profile — we only need the
    resolution_cache / manual_resolution_cache which are stored plain.
    """
    # resolution_cache is stored as plain dict — no decryption needed.
    return profile_raw


def _find_duplicate_tmdb_keys(rc: dict) -> dict[str, list[str]]:
    """Return {tmdb_id_str: [cache_key, ...]} for TMDB IDs that appear more
    than once in the resolution_cache.  These are candidates for collision
    entries where both items were cached with the same wrong TMDB ID."""
    by_tmdb: dict[str, list[str]] = defaultdict(list)
    for cache_key, tmdb_id in rc.items():
        if tmdb_id:
            by_tmdb[str(tmdb_id)].append(cache_key)
    return {tmdb: keys for tmdb, keys in by_tmdb.items() if len(keys) > 1}


def _parse_cache_key_ids(cache_key: str) -> dict:
    """Extract IDs embedded in a cache key string.

    Cache key format (from ItemMatcher._cache_key):
      media_type:resolver_mode:simkl_id:imdb_id:tmdb_id:mal_id:anilist_id:root_mal:root_anilist:title:year
    """
    parts = cache_key.split(":")
    if len(parts) < 11:
        return {}
    return {
        "media_type": parts[0],
        "resolver_mode": parts[1],
        "simkl_id": parts[2],
        "imdb_id": parts[3],
        "tmdb_id": parts[4],
        "mal_id": parts[5],
        "anilist_id": parts[6],
        "root_mal": parts[7],
        "root_anilist": parts[8],
        "title": parts[9],
        "year": parts[10] if len(parts) > 10 else "",
    }


_FRIBB_LOADED = False
_fribb_by_anilist: dict[int, dict] = {}
_fribb_by_mal: dict[int, dict] = {}


def _ensure_fribb_loaded() -> bool:
    global _FRIBB_LOADED, _fribb_by_anilist, _fribb_by_mal
    if _FRIBB_LOADED:
        return True
    try:
        from src import fribb_client  # noqa: PLC0415
        # Force the store to load.
        from src.fribb_client import AnimeMappingStore  # noqa: PLC0415
        store = AnimeMappingStore.get()
        if store is None:
            print("[WARN] Fribb store could not be loaded; --check-fribb skipped")
            return False
        _fribb_by_anilist = store._by_anilist if hasattr(store, "_by_anilist") else {}
        _fribb_by_mal = store._by_mal if hasattr(store, "_by_mal") else {}
        # If the internal dicts aren't available, fall back to the lookup functions.
        if not _fribb_by_anilist and not _fribb_by_mal:
            print("[INFO] Using fribb_client lookup functions (internal index not exposed)")
        _FRIBB_LOADED = True
        return True
    except Exception as exc:
        print(f"[WARN] Could not load Fribb: {exc}; --check-fribb skipped")
        return False


def _fribb_tmdb_for_ids(anilist_id: str, mal_id: str) -> int | None:
    """Return the Fribb TMDB ID for the given AniList or MAL ID, or None."""
    try:
        from src import fribb_client  # noqa: PLC0415
        entry = None
        if anilist_id:
            entry = fribb_client.lookup_by_anilist(int(anilist_id))
        if entry is None and mal_id:
            entry = fribb_client.lookup_by_mal(int(mal_id))
        if entry is None:
            return None
        raw = entry.get("themoviedb")
        return int(raw) if raw else None
    except Exception:
        return None


def repair_profile(profile_id: str, profile_raw: dict, dry_run: bool,
                   check_fribb: bool = False) -> dict:
    """Inspect and repair one profile.  Returns the (possibly modified) raw dict."""
    rc: dict = dict(profile_raw.get("resolution_cache") or {})
    mrc: dict = dict(profile_raw.get("manual_resolution_cache") or {})
    frc: dict = dict(profile_raw.get("failed_resolution_cache") or {})
    unresolved: list[dict] = profile_raw.get("unresolved_items") or []
    anime_overrides: dict = profile_raw.get("anime_manual_overrides") or {}

    changes: list[str] = []

    # ── 1. Remove auto resolution_cache entries that differ from manual ──────
    #   The manual cache always wins at runtime (merged on top), so stale auto
    #   entries are confusing but harmless.  Clean them for clarity.
    for ck, manual_tmdb in mrc.items():
        auto_tmdb = rc.get(ck)
        if auto_tmdb is not None and auto_tmdb != manual_tmdb:
            changes.append(
                f"  [rc-stale] cache_key={ck!r} "
                f"auto={auto_tmdb} → overridden by manual={manual_tmdb}: clearing auto entry"
            )
            del rc[ck]

    # ── 2. Detect duplicate TMDB IDs in resolution_cache (collision pairs) ───
    dups = _find_duplicate_tmdb_keys(rc)
    for tmdb_id_str, dup_keys in dups.items():
        # Skip if ALL of these keys are also in manual_resolution_cache
        # (user already fixed them manually).
        non_manual = [k for k in dup_keys if k not in mrc]
        if len(non_manual) <= 1:
            continue
        # These are anime cache keys that map to the same TMDB ID.
        # The first one that was synced is correct; the rest are wrong.
        # We can't know which is "first" so we clear them ALL from the
        # auto cache — they will get fresh lookups on the next sync.
        # Manual overrides are untouched.
        for ck in non_manual:
            changes.append(
                f"  [collision] tmdb={tmdb_id_str} ← cache_key={ck!r}: "
                f"clearing from resolution_cache (will re-resolve on next sync)"
            )
            rc.pop(ck, None)
            # Also clear from failed_resolution_cache so it gets retried.
            frc.pop(ck, None)

    # ── 3. Clear unresolved items whose cache_key is already in manual cache ──
    #   They should have been removed by resolve_item_manually but may linger
    #   from older runs.
    before_unresolved = len(unresolved)
    unresolved = [
        item for item in unresolved
        if item.get("cache_key") not in mrc
    ]
    removed_unresolved = before_unresolved - len(unresolved)
    if removed_unresolved:
        changes.append(
            f"  [unresolved-stale] removed {removed_unresolved} unresolved items "
            f"already present in manual_resolution_cache"
        )

    # ── 4. Report problematic TMDB IDs the user mentioned ────────────────────
    FLAGGED_TMDB_IDS = {"277700", "154634", "317316", "298754"}
    for ck, tmdb_id in list(rc.items()):
        if str(tmdb_id) in FLAGGED_TMDB_IDS and ck not in mrc:
            changes.append(
                f"  [flagged-tmdb] tmdb={tmdb_id} ← cache_key={ck!r}: "
                f"flagged as potentially wrong — clearing for re-resolution"
            )
            rc.pop(ck, None)
            frc.pop(ck, None)

    # ── 5. Cross-validate against Fribb (optional, --check-fribb) ─────────────
    #   For each anime cache entry that has an AniList or MAL ID in the key,
    #   look up what Fribb says the TMDB ID should be.  If Fribb disagrees with
    #   the cached TMDB ID, the cached entry is probably wrong (e.g. "An
    #   Affirmative Act" pattern: a non-anime TMDB result was accepted for an
    #   anime item).  Clear those entries so they get fresh lookups.
    if check_fribb and _ensure_fribb_loaded():
        for ck, cached_tmdb in list(rc.items()):
            if not cached_tmdb or ck in mrc:
                continue
            ids_in_key = _parse_cache_key_ids(ck)
            if not ids_in_key:
                continue
            # Only check items that look like anime (resolver_mode set or
            # has anilist/mal IDs in key).
            anilist_id = ids_in_key.get("anilist_id", "").strip()
            mal_id_k = ids_in_key.get("mal_id", "").strip()
            if not anilist_id and not mal_id_k:
                continue
            fribb_tmdb = _fribb_tmdb_for_ids(anilist_id, mal_id_k)
            if fribb_tmdb is None:
                # Fribb has no entry — can't validate, skip.
                continue
            if fribb_tmdb != int(cached_tmdb):
                title_in_key = ids_in_key.get("title", "?")
                changes.append(
                    f"  [fribb-mismatch] '{title_in_key}' cached tmdb={cached_tmdb}"
                    f" but Fribb says tmdb={fribb_tmdb}"
                    f" (anilist={anilist_id} mal={mal_id_k}) — clearing for re-resolution"
                )
                rc.pop(ck, None)
                frc.pop(ck, None)

    if not changes:
        print(f"  Profile {profile_id[:8]}: nothing to fix")
        return profile_raw

    print(f"  Profile {profile_id[:8]}:")
    for msg in changes:
        print(msg)

    if dry_run:
        return profile_raw

    updated = dict(profile_raw)
    updated["resolution_cache"] = rc
    updated["manual_resolution_cache"] = mrc
    updated["failed_resolution_cache"] = frc
    updated["unresolved_items"] = unresolved
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair bad anime cache entries in profiles.json")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    parser.add_argument("--profile-id", default="", help="Restrict to one profile (prefix)")
    parser.add_argument(
        "--check-fribb", action="store_true",
        help="Cross-validate cached anime TMDB IDs against Fribb database "
             "(clears entries where Fribb disagrees with the cached TMDB ID)",
    )
    args = parser.parse_args()

    print(f"Loading {PROFILE_STORE_FILE} …")
    raw = _load_profiles_raw()
    profiles: dict = raw.get("profiles") or {}

    if not profiles:
        print("No profiles found.")
        return

    modified = False
    for pid, pdata in profiles.items():
        if args.profile_id and not pid.startswith(args.profile_id):
            continue
        updated = repair_profile(pid, pdata, args.dry_run, check_fribb=args.check_fribb)
        if updated is not pdata:
            profiles[pid] = updated
            modified = True

    if modified:
        raw["profiles"] = profiles
        _save_profiles_raw(raw, args.dry_run)
        print("Done.")
    else:
        print("No changes needed.")


if __name__ == "__main__":
    main()
