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


if __name__ == "__main__":
    unittest.main()
