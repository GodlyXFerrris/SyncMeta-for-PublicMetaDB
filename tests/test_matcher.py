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


class DetailedStubPMDBClient(StubPMDBClient):
    def lookup_by_external_id_detailed(self, id_type: str, id_value: str, media_type: str) -> dict:
        self.calls.append((id_type, id_value, media_type))
        return {"tmdb_id": None, "status": "lookup_unavailable"}


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

    def test_resolve_match_reports_missing_ids(self) -> None:
        matcher = ItemMatcher(StubPMDBClient())

        result = matcher.resolve_match({
            "title": "No IDs Anime",
            "year": 2026,
            "media_type": "tv",
            "simkl_type": "anime",
            "ids": {},
        })

        self.assertIsNone(result.tmdb_id)
        self.assertEqual(result.unresolved_reason, "missing_ids")

    def test_resolve_match_reports_lookup_unavailable(self) -> None:
        matcher = ItemMatcher(DetailedStubPMDBClient())

        result = matcher.resolve_match({
            "title": "Unavailable Mapping Anime",
            "year": 2026,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "12345",
            "ids": {"anilist": "12345"},
        })

        self.assertIsNone(result.tmdb_id)
        self.assertEqual(result.unresolved_reason, "lookup_unavailable")

    def test_simkl_anime_prefers_external_mapping_before_direct_tmdb(self) -> None:
        client = StubPMDBClient()

        def fake_lookup(id_type: str, id_value: str, media_type: str) -> int | None:
            client.calls.append((id_type, id_value, media_type))
            if (id_type, id_value, media_type) == ("anilist", "12345", "tv"):
                return 777
            return None

        client.lookup_by_external_id = fake_lookup  # type: ignore[method-assign]
        matcher = ItemMatcher(
            client,
            anime_root_resolver=lambda anilist_id, mal_id: {
                "root": {"id": anilist_id or 999, "idMal": mal_id},
                "episode_offset": 0,
            },
        )

        result = matcher.resolve_match({
            "title": "Verified Anime",
            "year": 2026,
            "media_type": "tv",
            "simkl_type": "anime",
            "tmdb_id": "999999",
            "anilist_id": "12345",
            "ids": {"anilist": "12345"},
        })

        self.assertEqual(result.tmdb_id, 777)
        self.assertEqual(result.resolution_kind, "external_mapping")

    def test_simkl_anime_rejects_unverified_direct_tmdb(self) -> None:
        matcher = ItemMatcher(
            StubPMDBClient(),
            anime_root_resolver=lambda anilist_id, mal_id: None,
        )

        result = matcher.resolve_match({
            "title": "Suspicious Anime Entry",
            "year": 2026,
            "media_type": "tv",
            "simkl_type": "anime",
            "tmdb_id": "1575337",
            "anilist_id": "12345",
            "ids": {"anilist": "12345"},
        })

        self.assertIsNone(result.tmdb_id)
        self.assertEqual(result.unresolved_reason, "not_found")

    def test_simkl_anime_allows_verified_direct_tmdb_after_mapping_miss(self) -> None:
        matcher = ItemMatcher(
            StubPMDBClient(),
            anime_root_resolver=lambda anilist_id, mal_id: {
                "root": {"id": anilist_id or 999, "idMal": mal_id},
                "episode_offset": 0,
            },
        )

        result = matcher.resolve_match({
            "title": "Verified Direct TMDB Anime",
            "year": 2026,
            "media_type": "movie",
            "simkl_type": "anime",
            "tmdb_id": "1575337",
            "anilist_id": "999",
            "ids": {"anilist": "999"},
        })

        self.assertEqual(result.tmdb_id, 1575337)
        self.assertEqual(result.resolution_kind, "direct_tmdb")


if __name__ == "__main__":
    unittest.main()
