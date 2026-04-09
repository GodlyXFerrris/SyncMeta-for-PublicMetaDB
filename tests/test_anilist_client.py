import unittest

import requests

from src.anilist_client import AniListClient
from src.config import AniListConfig


class AniListClientTests(unittest.TestCase):
    def test_normalize_returns_direct_ids_without_root_walk(self) -> None:
        # Root IDs are no longer pre-populated during normalization; the matcher
        # resolves them lazily via anime_root_resolver when direct lookup fails.
        client = AniListClient(AniListConfig(username="tester"))

        normalized = client._normalize({
            "id": 177937,
            "idMal": 59027,
            "title": {"english": "SPY x FAMILY Season 3"},
            "seasonYear": 2025,
        })

        self.assertEqual(normalized["anilist_id"], "177937")
        self.assertEqual(normalized["mal_id"], "59027")
        self.assertIsNone(normalized["root_anilist_id"])
        self.assertIsNone(normalized["root_mal_id"])

    def test_normalize_single_episode_ona_as_movie(self) -> None:
        client = AniListClient(AniListConfig(username="tester"))

        normalized = client._normalize({
            "id": 21783,
            "idMal": 31387,
            "title": {"english": "Star Fox Zero: The Battle Begins"},
            "seasonYear": 2016,
            "format": "ONA",
            "episodes": 1,
        })

        self.assertEqual(normalized["media_type"], "movie")

    def test_normalize_multi_episode_ona_stays_tv(self) -> None:
        client = AniListClient(AniListConfig(username="tester"))

        normalized = client._normalize({
            "id": 99999,
            "idMal": 99999,
            "title": {"english": "Example Episodic ONA"},
            "seasonYear": 2026,
            "format": "ONA",
            "episodes": 6,
        })

        self.assertEqual(normalized["media_type"], "tv")

    def test_root_context_computes_episode_offset_from_prequels(self) -> None:
        client = AniListClient(AniListConfig(username="tester"))

        payloads = {
            177937: {
                "Media": {
                    "id": 177937,
                    "idMal": 59027,
                    "episodes": 12,
                    "format": "TV",
                    "seasonYear": 2025,
                    "startDate": {"year": 2025, "month": 1, "day": 1},
                    "title": {"english": "SPY x FAMILY Season 3"},
                    "relations": {
                        "edges": [
                            {
                                "relationType": "PREQUEL",
                                "node": {
                                    "id": 140960,
                                    "idMal": 48675,
                                    "episodes": 25,
                                    "format": "TV",
                                    "seasonYear": 2022,
                                    "startDate": {"year": 2022, "month": 4, "day": 1},
                                    "title": {"english": "SPY x FAMILY"},
                                },
                            }
                        ]
                    },
                }
            },
            140960: {
                "Media": {
                    "id": 140960,
                    "idMal": 48675,
                    "episodes": 25,
                    "format": "TV",
                    "seasonYear": 2022,
                    "startDate": {"year": 2022, "month": 4, "day": 1},
                    "title": {"english": "SPY x FAMILY"},
                    "relations": {"edges": []},
                }
            },
        }

        client._query = lambda query, variables: payloads.get(variables["id"])

        context = client._get_root_context(177937)

        self.assertEqual(context["root"]["id"], 140960)
        self.assertEqual(context["episode_offset"], 25)

    def test_get_statuses_reuses_completed_fetch_for_filtered_statuses(self) -> None:
        client = AniListClient(AniListConfig(username="tester"))
        calls: list[str] = []

        def fake_query(query, variables):
            calls.append(str(variables["status"]))
            return {
                "MediaListCollection": {
                    "lists": [
                        {
                            "entries": [
                                {
                                    "media": {
                                        "id": 1,
                                        "idMal": 11,
                                        "title": {"english": "Movie OVA"},
                                        "seasonYear": 2024,
                                        "format": "OVA",
                                        "episodes": 1,
                                    }
                                },
                                {
                                    "media": {
                                        "id": 2,
                                        "idMal": 22,
                                        "title": {"english": "Movie ONA"},
                                        "seasonYear": 2024,
                                        "format": "ONA",
                                        "episodes": 1,
                                    }
                                },
                            ]
                        }
                    ]
                }
            }

        client._query = fake_query

        results = client.get_statuses(["COMPLETED_OVA", "COMPLETED_ONA", "COMPLETED"])

        self.assertEqual(calls, ["COMPLETED"])
        self.assertEqual(len(results["COMPLETED"]), 2)
        self.assertEqual(len(results["COMPLETED_OVA"]), 1)
        self.assertEqual(results["COMPLETED_OVA"][0]["anilist_format"], "OVA")
        self.assertEqual(len(results["COMPLETED_ONA"]), 1)
        self.assertEqual(results["COMPLETED_ONA"][0]["anilist_format"], "ONA")

    def test_query_returns_none_on_http_error(self) -> None:
        client = AniListClient(AniListConfig(username="tester"))

        class _Resp:
            def raise_for_status(self) -> None:
                raise requests.HTTPError("404 Client Error: Not Found for url: https://graphql.anilist.co/")

        client._session.post = lambda *args, **kwargs: _Resp()

        result = client._query("query {}", {"id": 1})

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
