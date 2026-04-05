import unittest

from src.config import TraktConfig
from src.trakt_client import TraktClient


class TraktClientTests(unittest.TestCase):
    def test_normalize_movie_watchlist_entry(self) -> None:
        client = TraktClient(TraktConfig())

        item = client._normalize_watchlist_entry({
            "type": "movie",
            "listed_at": "2026-01-01T00:00:00.000Z",
            "movie": {
                "title": "Movie",
                "year": 2024,
                "ids": {"tmdb": 123, "imdb": "tt123"},
            },
        })

        self.assertEqual(item["media_type"], "movie")
        self.assertEqual(item["tmdb_id"], "123")
        self.assertEqual(item["imdb_id"], "tt123")

    def test_normalize_show_watchlist_entry(self) -> None:
        client = TraktClient(TraktConfig())

        item = client._normalize_watchlist_entry({
            "type": "show",
            "listed_at": "2026-01-01T00:00:00.000Z",
            "show": {
                "title": "Show",
                "year": 2024,
                "ids": {"tmdb": 456, "tvdb": 789},
            },
        })

        self.assertEqual(item["media_type"], "tv")
        self.assertEqual(item["tmdb_id"], "456")
        self.assertEqual(item["tvdb_id"], "789")

    def test_normalize_liked_list_metadata(self) -> None:
        client = TraktClient(TraktConfig())

        item = client._normalize_list_metadata({
            "type": "list",
            "list": {
                "name": "Anime",
                "description": "Favorites",
                "item_count": 22,
                "likes": 300,
                "share_link": "https://trakt.tv/users/demo/lists/anime",
                "ids": {"trakt": 12, "slug": "anime"},
                "user": {"username": "demo", "ids": {"slug": "demo"}},
            },
        }, source="liked")

        self.assertEqual(item["name"], "Anime")
        self.assertEqual(item["user"], "demo")
        self.assertEqual(item["slug"], "anime")
        self.assertEqual(item["source"], "liked")


if __name__ == "__main__":
    unittest.main()
