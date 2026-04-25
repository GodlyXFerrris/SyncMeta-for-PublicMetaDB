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
    def test_non_anime_falls_back_to_root_series_ids(self) -> None:
        matcher = ItemMatcher(StubPMDBClient())

        tmdb_id = matcher.resolve_tmdb_id({
            "title": "Example Franchise Sequel",
            "year": 2025,
            "media_type": "tv",
            "root_mal_id": "48675",
            "root_anilist_id": "140960",
            "root_title": "Example Root",
            "ids": {
                "root_mal": 48675,
                "root_anilist": 140960,
            },
        })

        self.assertEqual(tmdb_id, 68028)

    def test_anime_does_not_fall_back_to_root_series_ids(self) -> None:
        matcher = ItemMatcher(StubPMDBClient())

        result = matcher.resolve_match({
            "title": "SPY x FAMILY Season 3",
            "year": 2025,
            "media_type": "tv",
            "simkl_type": "anime",
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

        self.assertIsNone(result.tmdb_id)
        self.assertEqual(result.unresolved_reason, "not_found")

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


    def test_cache_key_includes_anilist_id_so_stale_entries_are_invalidated(self) -> None:
        # Two items that differ only in anilist_id must produce different cache keys
        # so that a stale wrong resolution for one (e.g. Boruto cached as Naruto)
        # cannot collide with or mask the other.
        boruto = {
            "title": "Boruto: Naruto Next Generations",
            "year": 2017,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "97938",
            "mal_id": "34566",
            "ids": {"anilist": 97938, "mal": 34566},
        }
        naruto = {
            "title": "Naruto",
            "year": 2002,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "20",
            "mal_id": "1735",
            "ids": {"anilist": 20, "mal": 1735},
        }
        # Also verify that an item with the same MAL/title/year but different
        # anilist_id gets a different key (guards against franchise-root collision).
        fake_boruto_no_anilist = {
            "title": "Boruto: Naruto Next Generations",
            "year": 2017,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "99999",
            "mal_id": "34566",
            "ids": {"anilist": 99999, "mal": 34566},
        }
        key_boruto = ItemMatcher._cache_key(boruto)
        key_naruto = ItemMatcher._cache_key(naruto)
        key_fake = ItemMatcher._cache_key(fake_boruto_no_anilist)

        self.assertNotEqual(key_boruto, key_naruto)
        self.assertNotEqual(key_boruto, key_fake)
        self.assertIn("97938", key_boruto)

    def test_highest_voted_pmdb_result_resolves_sequel_correctly(self) -> None:
        # Simulate PMDB returning two results for Boruto's AniList ID: the
        # wrong franchise-root mapping (Naruto, low votes) first, and the
        # correct per-show mapping (Boruto, high votes) second.
        client = StubPMDBClient()

        def fake_lookup_detailed(id_type: str, id_value: str, media_type: str) -> dict:
            client.calls.append((id_type, id_value, media_type))
            if (id_type, id_value, media_type) == ("anilist", "97938", "tv"):
                from src.publicmetadb_client import PublicMetaDBClient, PublicMetaDBConfig
                pmdb = PublicMetaDBClient(PublicMetaDBConfig(api_key="x"))
                import types

                def fake_get(path, params=None):
                    return {
                        "results": [
                            {"tmdb_id": 46260, "votes": 1},   # Naruto
                            {"tmdb_id": 65930, "votes": 10},  # Boruto
                        ]
                    }

                pmdb._get = fake_get  # type: ignore[method-assign]
                return pmdb.lookup_by_external_id_detailed(id_type, id_value, media_type)
            return {"tmdb_id": None, "status": "miss"}

        client.lookup_by_external_id_detailed = fake_lookup_detailed  # type: ignore[method-assign]
        matcher = ItemMatcher(client)

        result = matcher.resolve_match({
            "title": "Boruto: Naruto Next Generations",
            "year": 2017,
            "media_type": "tv",
            "simkl_type": "anime",
            "anilist_id": "97938",
            "mal_id": "34566",
            "ids": {"anilist": 97938, "mal": 34566},
        })

        self.assertEqual(result.tmdb_id, 65930)
        self.assertEqual(result.resolution_kind, "external_mapping")

    def test_prefer_root_series_uses_root_before_direct_tmdb(self) -> None:
        matcher = ItemMatcher(StubPMDBClient())

        tmdb_id = matcher.resolve_tmdb_id({
            "title": "SPY x FAMILY Season 3",
            "year": 2025,
            "media_type": "tv",
            "tmdb_id": "999999",
            "mal_id": "59027",
            "anilist_id": "177937",
            "root_mal_id": "48675",
            "root_anilist_id": "140960",
            "root_title": "SPY x FAMILY",
            "prefer_root_series": True,
            "ids": {
                "tmdb": 999999,
                "mal": 59027,
                "anilist": 177937,
                "root_mal": 48675,
                "root_anilist": 140960,
            },
        })

        self.assertEqual(tmdb_id, 68028)

    def test_anime_sequel_can_keep_direct_mapping_even_with_root_ids(self) -> None:
        client = StubPMDBClient()
        matcher = ItemMatcher(client)

        def fake_lookup_detailed(id_type: str, ext_id: str, media_type: str):
            if id_type == "anilist" and ext_id == "177937" and media_type == "tv":
                return {"tmdb_id": 999999, "status": "hit"}
            if id_type == "mal" and ext_id == "48675" and media_type == "tv":
                return {"tmdb_id": 68028, "status": "hit"}
            if id_type == "anilist" and ext_id == "140960" and media_type == "tv":
                return {"tmdb_id": 68028, "status": "hit"}
            return {"tmdb_id": None, "status": "miss"}

        client.lookup_by_external_id_detailed = fake_lookup_detailed  # type: ignore[method-assign]

        result = matcher.resolve_match({
            "title": "SPY x FAMILY Season 3",
            "year": 2025,
            "media_type": "tv",
            "simkl_type": "anime",
            "tmdb_id": "999999",
            "anilist_id": "177937",
            "mal_id": "59027",
            "root_mal_id": "48675",
            "root_anilist_id": "140960",
            "root_title": "SPY x FAMILY",
            "prefer_root_series": False,
            "ids": {
                "tmdb": 999999,
                "anilist": 177937,
                "mal": 59027,
                "root_mal": 48675,
                "root_anilist": 140960,
            },
        })

        self.assertEqual(result.tmdb_id, 999999)
        self.assertEqual(result.resolution_kind, "external_mapping")

    def test_direct_tmdb_still_wins_without_root_preference(self) -> None:
        matcher = ItemMatcher(StubPMDBClient())

        tmdb_id = matcher.resolve_tmdb_id({
            "title": "Anime Movie",
            "media_type": "movie",
            "tmdb_id": "1575337",
            "root_mal_id": "48675",
            "ids": {"tmdb": 1575337, "root_mal": 48675},
        })

        self.assertEqual(tmdb_id, 1575337)


if __name__ == "__main__":
    unittest.main()
