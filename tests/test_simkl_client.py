import unittest

from src.config import SimklConfig
from src.simkl_client import SimklClient


class RecordingSimklClient(SimklClient):
    def __init__(self) -> None:
        super().__init__(SimklConfig(client_id="client", access_token="token"))
        self.paths: list[str] = []

    def _get(self, path: str, params: dict | None = None) -> dict | list | None:
        self.paths.append(path)
        if path == "/sync/all-items/tv/watching":
            return {
                "shows": [
                    {
                        "show": {
                            "title": "Demo Show",
                            "year": 2024,
                            "ids": {"tmdb": 1234},
                        },
                        "status": "watching",
                        "seasons": [
                            {
                                "number": 1,
                                "episodes": [
                                    {"number": 1, "watched_at": "2026-04-04T12:00:00Z"},
                                ],
                            }
                        ],
                    },
                    {
                        "show": {
                            "title": "Completed Leak",
                            "year": 2022,
                            "ids": {"tmdb": 9999},
                        },
                        "status": "completed",
                    },
                ]
            }
        if path == "/sync/all-items/anime/plan%20to%20watch":
            return {
                "anime": [
                    {
                        "show": {
                            "title": "Demo Anime",
                            "year": 2025,
                            "ids": {"tmdb": 5678, "anilist": 44},
                        },
                        "status": "plantowatch",
                    }
                ]
            }
        if path == "/sync/all-items/movie/completed":
            return {
                "movies": [
                    {
                        "movie": {
                            "title": "Completed Movie",
                            "ids": {"tmdb": 7001},
                        },
                        "last_watched_at": "2026-04-01T12:00:00Z",
                    }
                ]
            }
        if path == "/sync/all-items/tv/watching":
            return {
                "shows": [
                    {
                        "show": {
                            "title": "Watching Show",
                            "ids": {"tmdb": 7004},
                        },
                        "seasons": [
                            {
                                "number": 1,
                                "episodes": [
                                    {"number": 1, "watched_at": "2026-04-04T12:00:00Z"},
                                ],
                            }
                        ],
                    }
                ]
            }
        if path == "/sync/all-items/tv/completed":
            return {
                "shows": [
                    {
                        "show": {
                            "title": "Completed Show",
                            "ids": {"tmdb": 7002},
                        },
                        "seasons": [
                            {
                                "number": 1,
                                "episodes": [
                                    {"number": 2, "watched_at": "2026-04-02T12:00:00Z"},
                                ],
                            }
                        ],
                    }
                ]
            }
        if path == "/sync/all-items/tv/on%20hold":
            return {
                "shows": []
            }
        if path == "/sync/all-items/tv/dropped":
            return {
                "shows": []
            }
        if path == "/sync/all-items/tv/plan%20to%20watch":
            return {
                "shows": []
            }
        if path == "/sync/all-items/anime/watching":
            return {
                "anime": []
            }
        if path == "/sync/all-items/anime/completed":
            return {
                "anime": [
                    {
                        "show": {
                            "title": "Completed Anime",
                            "ids": {"tmdb": 7003},
                        },
                        "episodes": [
                            {"season": 1, "number": 3, "watched_at": "2026-04-03T12:00:00Z"},
                        ],
                    }
                ]
            }
        if path == "/sync/all-items/anime/on%20hold":
            return {
                "anime": []
            }
        if path == "/sync/all-items/anime/dropped":
            return {
                "anime": []
            }
        if path == "/sync/all-items/anime/plan%20to%20watch":
            return {
                "anime": []
            }
        if path == "/sync/playback":
            return {
                "movies": [
                    {
                        "movie": {
                            "title": "Paused Movie",
                            "runtime": 120,
                            "ids": {"tmdb": 8001},
                        },
                        "progress": 50,
                    }
                ],
                "shows": [
                    {
                        "show": {
                            "title": "Paused Show",
                            "ids": {"tmdb": 8002},
                        },
                        "episode": {
                            "season": 2,
                            "number": 4,
                            "runtime": 45,
                        },
                        "progress": 40,
                    }
                ],
            }
        return None


class SimklClientTests(unittest.TestCase):
    def test_fetch_list_uses_type_specific_endpoint(self) -> None:
        client = RecordingSimklClient()

        grouped = client.get_status("watching", ["shows"])

        self.assertEqual(client.paths, ["/sync/all-items/tv/watching"])
        self.assertEqual(len(grouped["shows"]), 1)
        self.assertEqual(grouped["shows"][0]["title"], "Demo Show")
        self.assertEqual(grouped["shows"][0]["media_type"], "tv")

    def test_fetch_list_maps_plan_to_watch_status_for_api(self) -> None:
        client = RecordingSimklClient()

        grouped = client.get_status("plantowatch", ["anime"])

        self.assertEqual(client.paths, ["/sync/all-items/anime/plan%20to%20watch"])
        self.assertEqual(grouped["anime"][0]["title"], "Demo Anime")
        self.assertEqual(grouped["anime"][0]["anilist_id"], "44")

    def test_get_watched_history_parses_movies_shows_and_anime(self) -> None:
        client = RecordingSimklClient()

        history = client.get_watched_history()

        self.assertEqual(len(history), 4)
        self.assertTrue(any(
            item["tmdb_id"] == 7001 and item["media_type"] == "movie" and item["watched_at"] == "2026-04-01T12:00:00Z" and item["title"] == "Completed Movie"
            for item in history
        ))
        self.assertTrue(any(
            item["tmdb_id"] == 1234 and item["media_type"] == "tv" and item["season"] == 1 and item["episode"] == 1 and item["watched_at"] == "2026-04-04T12:00:00Z" and item["title"] == "Demo Show"
            for item in history
        ))
        self.assertTrue(any(
            item["tmdb_id"] == 7002 and item["media_type"] == "tv" and item["season"] == 1 and item["episode"] == 2 and item["watched_at"] == "2026-04-02T12:00:00Z" and item["title"] == "Completed Show"
            for item in history
        ))
        self.assertTrue(any(
            item["tmdb_id"] == 7003 and item["media_type"] == "tv" and item["season"] == 1 and item["episode"] == 3 and item["watched_at"] == "2026-04-03T12:00:00Z" and item["title"] == "Completed Anime"
            for item in history
        ))

    def test_get_playback_progress_parses_movie_and_episode(self) -> None:
        client = RecordingSimklClient()

        progress = client.get_playback_progress()

        self.assertEqual(len(progress), 2)
        self.assertTrue(any(
            item["tmdb_id"] == 8001 and item["media_type"] == "movie" and item["position_ms"] == 3_600_000 and item["runtime_ms"] == 7_200_000 and item["progress"] == 50.0 and item["paused_at"] is None and item["title"] == "Paused Movie"
            for item in progress
        ))
        self.assertTrue(any(
            item["tmdb_id"] == 8002 and item["media_type"] == "tv" and item["season"] == 2 and item["episode"] == 4 and item["position_ms"] == 1_080_000 and item["runtime_ms"] == 2_700_000 and item["progress"] == 40.0 and item["paused_at"] is None and item["title"] == "Paused Show"
            for item in progress
        ))


if __name__ == "__main__":
    unittest.main()
