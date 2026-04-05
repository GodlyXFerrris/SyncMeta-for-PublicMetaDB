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
                    }
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

        self.assertEqual(len(history), 3)
        self.assertIn(
            {"tmdb_id": 7001, "media_type": "movie", "watched_at": "2026-04-01T12:00:00Z", "title": "Completed Movie"},
            history,
        )
        self.assertIn(
            {"tmdb_id": 7002, "media_type": "tv", "season": 1, "episode": 2, "watched_at": "2026-04-02T12:00:00Z", "title": "Completed Show"},
            history,
        )
        self.assertIn(
            {"tmdb_id": 7003, "media_type": "tv", "season": 1, "episode": 3, "watched_at": "2026-04-03T12:00:00Z", "title": "Completed Anime"},
            history,
        )

    def test_get_playback_progress_parses_movie_and_episode(self) -> None:
        client = RecordingSimklClient()

        progress = client.get_playback_progress()

        self.assertEqual(len(progress), 2)
        self.assertIn(
            {"tmdb_id": 8001, "media_type": "movie", "position_ms": 3_600_000, "runtime_ms": 7_200_000, "progress": 50.0, "paused_at": None, "title": "Paused Movie"},
            progress,
        )
        self.assertIn(
            {"tmdb_id": 8002, "media_type": "tv", "season": 2, "episode": 4, "position_ms": 1_080_000, "runtime_ms": 2_700_000, "progress": 40.0, "paused_at": None, "title": "Paused Show"},
            progress,
        )


if __name__ == "__main__":
    unittest.main()
