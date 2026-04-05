import unittest

from src.anilist_client import AniListClient
from src.config import AniListConfig


class AniListClientTests(unittest.TestCase):
    def test_normalize_adds_root_series_ids(self) -> None:
        client = AniListClient(AniListConfig(username="tester"))
        client._get_root_media = lambda media_id: {
            "id": 140960,
            "idMal": 48675,
            "title": {"english": "SPY x FAMILY"},
        }

        normalized = client._normalize({
            "id": 177937,
            "idMal": 59027,
            "title": {"english": "SPY x FAMILY Season 3"},
            "seasonYear": 2025,
        })

        self.assertEqual(normalized["root_anilist_id"], "140960")
        self.assertEqual(normalized["root_mal_id"], "48675")
        self.assertEqual(normalized["root_title"], "SPY x FAMILY")
        self.assertEqual(normalized["ids"]["root_anilist"], 140960)
        self.assertEqual(normalized["ids"]["root_mal"], 48675)


if __name__ == "__main__":
    unittest.main()
