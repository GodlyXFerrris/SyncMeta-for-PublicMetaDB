"""Trakt API client for watchlist and list syncing."""

from __future__ import annotations

import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import TraktConfig

logger = logging.getLogger(__name__)


class TraktClient:
    """Client for the Trakt API."""

    def __init__(self, config: TraktConfig):
        self._config = config
        self._session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "Content-Type": "application/json",
            "trakt-api-version": "2",
        })
        if self._config.client_id:
            session.headers["trakt-api-key"] = self._config.client_id
        if self._config.access_token:
            session.headers["Authorization"] = f"Bearer {self._config.access_token}"

        retry = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _request_response(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self._config.base_url}{path}"
        response = self._session.request(method, url, timeout=30, **kwargs)
        response.raise_for_status()
        return response

    def _request(self, method: str, path: str, **kwargs) -> dict | list | None:
        response = self._request_response(method, path, **kwargs)
        if response.status_code == 204 or not response.text:
            return None
        return response.json()

    def _get(self, path: str, params: dict | None = None) -> dict | list | None:
        return self._request("GET", path, params=params)

    def _post(self, path: str, data: dict) -> dict | list | None:
        return self._request("POST", path, json=data)

    def request_device_code(self) -> dict:
        data = self._post("/oauth/device/code", {"client_id": self._config.client_id})
        if not data:
            raise RuntimeError("Failed to start Trakt device authentication")
        return data

    def poll_device_token(self, device_code: str) -> dict:
        try:
            data = self._post("/oauth/device/token", {
                "code": device_code,
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
            })
            if not data:
                raise RuntimeError("Failed to retrieve Trakt device token")
            return data
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.text:
                try:
                    return exc.response.json()
                except ValueError:
                    pass
            raise

    def get_watchlist(self) -> list[dict]:
        raw = self._get("/sync/watchlist", params={"extended": "full"}) or []
        return [item for item in (self._normalize_watchlist_entry(entry) for entry in raw) if item]

    def get_liked_lists(self) -> list[dict]:
        liked_lists = []
        for meta in self.get_liked_lists_metadata():
            items = self.get_list_items(meta["user"], meta["slug"])
            liked_lists.append({**meta, "items": items})
        return liked_lists

    def get_liked_lists_metadata(self) -> list[dict]:
        raw = self._get("/users/likes/lists", params={"extended": "full", "limit": 100}) or []
        metadata = []
        for entry in raw:
            normalized = self._normalize_list_metadata(entry, source="liked")
            if normalized:
                metadata.append(normalized)
        return metadata

    def search_lists(self, query: str) -> list[dict]:
        query = str(query or "").strip()
        if not query:
            return []
        raw = self._get("/search/list", params={
            "query": query,
            "extended": "full",
            "limit": 20,
            "page": 1,
        }) or []
        results = []
        for entry in raw:
            normalized = self._normalize_list_metadata(entry, source="discover")
            if normalized:
                results.append(normalized)
        return results

    def get_list_items(self, username: str, slug: str) -> list[dict]:
        raw = self._get(f"/users/{username}/lists/{slug}/items/movie,show", params={"extended": "full"}) or []
        items = []
        for entry in raw:
            normalized = self._normalize_watchlist_entry(entry)
            if normalized:
                items.append(normalized)
        return items

    def _normalize_list_metadata(self, entry: dict, source: str) -> dict | None:
        list_data = entry.get("list") if isinstance(entry, dict) and entry.get("list") else entry
        if not isinstance(list_data, dict):
            return None

        user = list_data.get("user") or {}
        ids = list_data.get("ids") or {}
        username = (
            user.get("ids", {}).get("slug")
            or user.get("username")
            or self._config.username
            or "me"
        )
        slug = ids.get("slug") or ids.get("trakt")
        if not username or not slug:
            return None

        return {
            "name": list_data.get("name") or f"List {slug}",
            "description": list_data.get("description") or "",
            "user": str(username),
            "slug": str(slug),
            "trakt_id": ids.get("trakt"),
            "item_count": int(list_data.get("item_count") or 0),
            "likes": int(list_data.get("likes") or 0),
            "share_link": list_data.get("share_link") or "",
            "source": source,
        }

    @staticmethod
    def _normalize_watchlist_entry(entry: dict) -> dict | None:
        entry_type = entry.get("type")
        if entry_type == "movie":
            media = entry.get("movie")
            media_type = "movie"
            trakt_type = "movies"
        elif entry_type == "show":
            media = entry.get("show")
            media_type = "tv"
            trakt_type = "shows"
        else:
            return None

        if not media:
            return None

        ids = media.get("ids", {})
        return {
            "title": media.get("title", "Unknown"),
            "year": media.get("year"),
            "media_type": media_type,
            "simkl_type": trakt_type,
            "imdb_id": ids.get("imdb"),
            "tmdb_id": str(ids["tmdb"]) if ids.get("tmdb") else None,
            "mal_id": None,
            "anilist_id": None,
            "anidb_id": None,
            "tvdb_id": str(ids["tvdb"]) if ids.get("tvdb") else None,
            "ids": ids,
            "status": entry.get("listed_at"),
            "added_at": entry.get("listed_at"),
        }
