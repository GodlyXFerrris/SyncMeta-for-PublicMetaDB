"""MDBList API client for loading a user's lists and items."""

from __future__ import annotations

import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import MdbListConfig

logger = logging.getLogger(__name__)


class MdbListClient:
    """Client for the MDBList REST API."""

    def __init__(self, config: MdbListConfig):
        self._config = config
        self._session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"Accept": "application/json"})

        retry = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        request_params = dict(params or {})
        request_params["apikey"] = self._config.api_key
        url = f"{self._config.base_url}{path}"
        logger.debug("GET %s params=%s", url, request_params)
        response = self._session.get(url, params=request_params, timeout=30)
        response.raise_for_status()
        return response

    def get_user_lists(self) -> list[dict]:
        """Fetch the authenticated user's MDBList lists."""
        response = self._get("/lists/user/", {"sort": "rank", "unified": "false"})
        payload = response.json() or []
        items = [self._normalize_list_metadata(item) for item in payload]
        return [item for item in items if item]

    def get_list_items(self, list_id: int) -> list[dict]:
        """Fetch all items for a list, following offset pagination."""
        offset = 0
        limit = 200
        items: list[dict] = []

        while True:
            response = self._get(
                f"/lists/{list_id}/items",
                {
                    "limit": limit,
                    "offset": offset,
                    "append_to_response": "genre",
                },
            )
            payload = response.json() or []
            normalized = [self._normalize_item(item) for item in payload]
            items.extend(item for item in normalized if item)

            has_more = str(response.headers.get("X-Has-More", "")).strip().lower() == "true"
            if not has_more or not payload:
                break
            offset += limit

        logger.info("MDBList: fetched %d items for list %s", len(items), list_id)
        return items

    @staticmethod
    def _normalize_list_metadata(item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None

        try:
            list_id = int(item.get("id"))
        except (TypeError, ValueError):
            return None

        mediatype = str(item.get("mediatype", "")).strip().lower()
        name = str(item.get("name", "")).strip()
        if mediatype not in {"movie", "show"} or not name:
            return None

        try:
            item_count = int(item.get("items", 0) or 0)
        except (TypeError, ValueError):
            item_count = 0

        try:
            likes = int(item.get("likes", 0) or 0)
        except (TypeError, ValueError):
            likes = 0

        return {
            "id": list_id,
            "name": name,
            "slug": str(item.get("slug", "")).strip(),
            "user_name": str(item.get("user_name", "")).strip(),
            "description": str(item.get("description", "")).strip(),
            "mediatype": mediatype,
            "items": item_count,
            "likes": likes,
            "type": str(item.get("type", "")).strip(),
            "private": bool(item.get("private", False)),
        }

    @staticmethod
    def _normalize_item(item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None

        mediatype = str(item.get("mediatype", "")).strip().lower()
        if mediatype == "movie":
            media_type = "movie"
        elif mediatype == "show":
            media_type = "tv"
        else:
            return None

        ids = item.get("ids") or {}
        imdb_id = item.get("imdb_id") or ids.get("imdb")
        tmdb_id = ids.get("tmdb")
        tvdb_id = item.get("tvdb_id") or ids.get("tvdb")

        return {
            "title": item.get("title") or "Unknown",
            "year": item.get("release_year"),
            "media_type": media_type,
            "simkl_type": mediatype,
            "imdb_id": str(imdb_id) if imdb_id else None,
            "tmdb_id": str(tmdb_id) if tmdb_id else None,
            "mal_id": None,
            "anilist_id": None,
            "anidb_id": None,
            "tvdb_id": str(tvdb_id) if tvdb_id else None,
            "ids": ids,
            "status": None,
            "added_at": None,
        }
