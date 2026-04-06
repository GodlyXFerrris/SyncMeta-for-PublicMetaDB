import unittest

import requests
from urllib3.util.retry import Retry

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

    def test_query_returns_none_on_http_error(self) -> None:
        client = AniListClient(AniListConfig(username="tester"))

        class _Resp:
            def raise_for_status(self) -> None:
                raise requests.HTTPError("404 Client Error: Not Found for url: https://graphql.anilist.co/")

        client._session.post = lambda *args, **kwargs: _Resp()

        result = client._query("query {}", {"id": 1})

        self.assertIsNone(result)

    def test_query_pauses_further_requests_after_429(self) -> None:
        client = AniListClient(AniListConfig(username="tester"))
        calls = {"count": 0}

        class _Resp:
            status_code = 429

            def raise_for_status(self) -> None:
                error = requests.HTTPError("429 Client Error")
                error.response = self
                raise error

        def _post(*args, **kwargs):
            calls["count"] += 1
            return _Resp()

        client._session.post = _post

        self.assertIsNone(client._query("query {}", {"id": 1}))
        self.assertIsNone(client._query("query {}", {"id": 2}))
        self.assertEqual(calls["count"], 1)

    def test_retry_policy_does_not_retry_429s(self) -> None:
        client = AniListClient(AniListConfig(username="tester"))
        adapter = client._session.get_adapter("https://")
        retry = adapter.max_retries

        self.assertIsInstance(retry, Retry)
        self.assertNotIn(429, retry.status_forcelist)


if __name__ == "__main__":
    unittest.main()
