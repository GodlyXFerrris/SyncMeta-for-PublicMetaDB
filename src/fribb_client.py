"""Backward-compatible helpers around the offline anime mapping store."""

from __future__ import annotations

from . import anime_mapping_store as _store


def lookup_by_anilist(anilist_id: int) -> dict | None:
    return _store.lookup_fribb(anilist_id=int(anilist_id))


def lookup_by_mal(mal_id: int) -> dict | None:
    return _store.lookup_fribb(mal_id=int(mal_id))


def lookup_by_anidb(anidb_id: int) -> dict | None:
    return _store.lookup_fribb(anidb_id=int(anidb_id))


def lookup_by_simkl(simkl_id: int) -> dict | None:
    return _store.lookup_fribb(simkl_id=int(simkl_id))


def lookup_by_tmdb(tmdb_id: int) -> dict | None:
    return _store.lookup_fribb(tmdb_id=int(tmdb_id))


def lookup_by_imdb(imdb_id: str) -> dict | None:
    return _store.lookup_fribb(imdb_id=str(imdb_id))


def validate_tmdb(entry: dict | None, tmdb_id: int | None) -> bool:
    return _store.validate_tmdb(entry, tmdb_id)


def cache_metadata() -> dict:
    return _store.cache_metadata()


def force_refresh() -> dict:
    return _store.force_refresh()
