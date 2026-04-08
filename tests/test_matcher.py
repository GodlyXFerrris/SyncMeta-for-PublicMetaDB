import unittest

from src.matcher import ItemMatcher


class StubPMDBClient:
    def __init__(self):
        self.calls = []

    def lookup_by_external_id(self, id_type: str, id_value: str, media_type: str) -> int | None:
        self.calls.append((id_type, id_value, media_type))
        if (id_type, id_value, media_type) == ("mal", "48675", "tv"):
            return 68028
        return None


class ItemMatcherTests(unittest.TestCase):
    def test_falls_back_to_root_series_ids(self) -> None:
        matcher = ItemMatcher(StubPMDBClient())

        tmdb_id = matcher.resolve_tmdb_id({
            "title": "SPY x FAMILY Season 3",
            "year": 2025,
            "media_type": "tv",
            "mal_id": "59027",
            "anilist_id": "177937",
            "root_mal_id": "48675",
            "root_anilist_id": "140960",
            "root_title": "SPY x FAMILY",
            "ids": {
                "mal": 59027,
                "anilist": 177937,
                "root_mal": 48675,
                "root_anilist": 140960,
            },
        })

        self.assertEqual(tmdb_id, 68028)

    def test_anime_prefers_anilist_before_imdb(self) -> None:
        client = StubPMDBClient()

        def fake_lookup(id_type: str, id_value: str, media_type: str) -> int | None:
            client.calls.append((id_type, id_value, media_type))
            if (id_type, id_value, media_type) == ("anilist", "12345", "tv"):
                return 777
            if (id_type, id_value, media_type) == ("imdb", "tt123", "tv"):
                return 888
            return None

        client.lookup_by_external_id = fake_lookup  # type: ignore[method-assign]
        matcher = ItemMatcher(client)

        tmdb_id = matcher.resolve_tmdb_id({
            "title": "Example Anime",
            "year": 2026,
            "media_type": "tv",
            "simkl_type": "anime",
            "imdb_id": "tt123",
            "anilist_id": "12345",
        })

        self.assertEqual(tmdb_id, 777)
        self.assertEqual(client.calls[0], ("anilist", "12345", "tv"))


if __name__ == "__main__":
    unittest.main()
