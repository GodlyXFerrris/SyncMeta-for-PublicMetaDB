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


if __name__ == "__main__":
    unittest.main()
