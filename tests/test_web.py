import os
import unittest
from unittest.mock import patch

os.environ["DISABLE_PROFILE_SCHEDULER"] = "1"

import web  # noqa: E402


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = web.app.test_client()

    def test_index_contains_new_source_sections(self) -> None:
        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("SIMKL Lists", html)
        self.assertIn("AniList Lists", html)
        self.assertIn("MDBList Lists", html)
        self.assertIn("dot-mdblist", html)
        self.assertIn("https://github.com/Febsho/SyncMeta-for-PublicMetaDB", html)
        self.assertIn("<h3>Options</h3>", html)
        self.assertNotIn("Sync Series", html)
        self.assertNotIn("Sync Movies", html)
        self.assertNotIn("Sync Anime", html)

    @patch("web.SimklClient.request_pin")
    def test_simkl_pin_start(self, mock_request_pin) -> None:
        mock_request_pin.return_value = {
            "user_code": "ABCD",
            "verification_url": "https://simkl.com/pin/",
            "interval": 5,
            "expires_in": 900,
            "result": "OK",
        }

        response = self.client.post("/api/simkl/pin/start", json={"client_id": "client"})
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["user_code"], "ABCD")
        self.assertEqual(data["verification_url"], "https://simkl.com/pin/")

    @patch("web.SimklClient.check_pin")
    def test_simkl_pin_check_approved(self, mock_check_pin) -> None:
        mock_check_pin.return_value = {
            "result": "OK",
            "access_token": "token-123",
        }

        response = self.client.post("/api/simkl/pin/check", json={
            "client_id": "client",
            "user_code": "ABCD",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["status"], "approved")
        self.assertEqual(data["access_token"], "token-123")

    @patch("web.TraktClient.request_device_code")
    def test_trakt_device_start(self, mock_request_device_code) -> None:
        mock_request_device_code.return_value = {
            "device_code": "device",
            "user_code": "TRAKT",
            "verification_url": "https://trakt.tv/activate",
            "interval": 5,
            "expires_in": 600,
        }

        response = self.client.post("/api/trakt/device/start", json={
            "client_id": "client",
            "client_secret": "secret",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["device_code"], "device")
        self.assertEqual(data["user_code"], "TRAKT")

    @patch("web.TraktClient.poll_device_token")
    def test_trakt_device_check_approved(self, mock_poll_device_token) -> None:
        mock_poll_device_token.return_value = {
            "access_token": "access",
            "refresh_token": "refresh",
        }

        response = self.client.post("/api/trakt/device/check", json={
            "client_id": "client",
            "client_secret": "secret",
            "device_code": "device",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["status"], "approved")
        self.assertEqual(data["refresh_token"], "refresh")

    @patch("web.TraktClient.get_liked_lists_metadata")
    def test_trakt_catalogs_liked_lists(self, mock_get_liked_lists_metadata) -> None:
        mock_get_liked_lists_metadata.return_value = [{
            "name": "Anime",
            "user": "demo",
            "slug": "anime",
            "source": "liked",
            "likes": 42,
            "item_count": 10,
        }]

        response = self.client.post("/api/trakt/catalogs", json={
            "client_id": "client",
            "access_token": "token",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["slug"], "anime")

    @patch("web.TraktClient.search_lists")
    def test_trakt_catalogs_search(self, mock_search_lists) -> None:
        mock_search_lists.return_value = [{
            "name": "Top Rated",
            "user": "demo",
            "slug": "top-rated",
            "source": "discover",
        }]

        response = self.client.post("/api/trakt/catalogs", json={
            "client_id": "client",
            "access_token": "token",
            "query": "top rated",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["query"], "top rated")
        self.assertEqual(data["items"][0]["source"], "discover")

    @patch("web.MdbListClient.get_user_lists")
    def test_mdblist_lists(self, mock_get_user_lists) -> None:
        mock_get_user_lists.return_value = [{
            "id": 7,
            "name": "Favorites",
            "mediatype": "movie",
        }]

        response = self.client.post("/api/mdblist/lists", json={
            "api_key": "mdb-key",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["id"], 7)


if __name__ == "__main__":
    unittest.main()
