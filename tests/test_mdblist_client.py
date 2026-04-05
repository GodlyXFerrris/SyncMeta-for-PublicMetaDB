import unittest
from unittest.mock import Mock, patch

from src.config import MdbListConfig
from src.mdblist_client import MdbListClient


class MdbListClientTests(unittest.TestCase):
    def test_get_user_lists_normalizes_response(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        response = Mock()
        response.json.return_value = [{
            "id": 12,
            "name": "Best Movies",
            "slug": "best-movies",
            "user_name": "demo",
            "description": "desc",
            "mediatype": "movie",
            "items": 25,
            "likes": 7,
            "type": "user",
            "private": False,
        }]

        with patch.object(client, "_get", return_value=response):
            items = client.get_user_lists()

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], 12)
        self.assertEqual(items[0]["mediatype"], "movie")

    def test_get_list_items_paginates_and_normalizes(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        page_one = Mock()
        page_one.json.return_value = [{
            "title": "Movie One",
            "release_year": 2023,
            "mediatype": "movie",
            "ids": {"tmdb": 101, "imdb": "tt0101"},
        }]
        page_one.headers = {"X-Has-More": "true"}

        page_two = Mock()
        page_two.json.return_value = [{
            "title": "Show One",
            "release_year": 2024,
            "mediatype": "show",
            "ids": {"tmdb": 202, "tvdb": 303},
        }]
        page_two.headers = {"X-Has-More": "false"}

        with patch.object(client, "_get", side_effect=[page_one, page_two]):
            items = client.get_list_items(12)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["media_type"], "movie")
        self.assertEqual(items[1]["media_type"], "tv")

    def test_get_list_items_supports_wrapped_payload(self) -> None:
        client = MdbListClient(MdbListConfig(api_key="key"))
        response = Mock()
        response.json.return_value = {
            "items": [{
                "title": "Wrapped Movie",
                "release_year": 2022,
                "mediatype": "movie",
                "ids": {"tmdb": 404, "imdb": "tt0404"},
            }]
        }
        response.headers = {}

        with patch.object(client, "_get", return_value=response):
            items = client.get_list_items(44)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Wrapped Movie")
        self.assertEqual(items[0]["media_type"], "movie")


if __name__ == "__main__":
    unittest.main()
