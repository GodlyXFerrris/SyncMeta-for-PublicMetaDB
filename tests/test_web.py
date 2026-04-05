import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["DISABLE_PROFILE_SCHEDULER"] = "1"

import web  # noqa: E402


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_site_access_password = web.SITE_ACCESS_PASSWORD
        web._profile_store = web.ProfileStore(Path(self.tmpdir.name) / "profiles.json")
        web._session_store = web.ServerSessionStore(ttl_seconds=3600)
        web._login_limiter = web.LoginAttemptLimiter(max_attempts=5, window_seconds=60)
        web._access_store = web.ServerSessionStore(ttl_seconds=3600)
        web._access_limiter = web.LoginAttemptLimiter(max_attempts=5, window_seconds=60)
        web.SITE_ACCESS_PASSWORD = ""
        self.client = web.app.test_client()

    def tearDown(self) -> None:
        web.SITE_ACCESS_PASSWORD = self.original_site_access_password
        self.tmpdir.cleanup()

    def test_index_contains_new_source_sections(self) -> None:
        response = self.client.get("/")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Sync Your Lists.<br>Keep Them Fresh.", html)
        self.assertIn("Connect SIMKL, AniList, Trakt, and MDBList", html)
        self.assertIn(">Trakt</div>", html)
        self.assertIn(">MDBList</div>", html)
        self.assertIn("SIMKL Lists", html)
        self.assertIn("AniList Lists", html)
        self.assertIn("MDBList Lists", html)
        self.assertIn("dot-mdblist", html)
        self.assertIn("If SIMKL asks for a redirect URL, use your SyncMeta HTTPS URL.", html)
        self.assertIn("If Trakt asks for a redirect URL, use your SyncMeta HTTPS URL.", html)
        self.assertIn("https://github.com/Febsho/SyncMeta-for-PublicMetaDB", html)
        self.assertIn("<h3>Options</h3>", html)
        self.assertIn("SIMKL Lists Visibility", html)
        self.assertIn("AniList Lists Visibility", html)
        self.assertIn("Trakt Personal Lists Visibility", html)
        self.assertIn("Trakt Public Lists Visibility", html)
        self.assertIn("MDBList Visibility", html)
        self.assertIn("Applies to Trakt watchlist and default personal-style catalogs like recommendations.", html)
        self.assertIn("Applies to liked Trakt lists and Discover/public Trakt lists.", html)
        self.assertIn("Delete User Records", html)
        self.assertIn("Danger Zone", html)
        self.assertIn("Stored securely for this profile. Leave blank to keep it.", html)
        self.assertIn("selected public Trakt lists", html)
        self.assertIn("personal or public-style catalog lists", html)
        self.assertIn("SyncMeta</div>", html)
        self.assertNotIn("cookie_notice_ack", html)
        self.assertIn("choose exactly which movie, show, and anime statuses should sync", html)
        self.assertIn("choose which anime lists should sync into PublicMetaDB", html)
        self.assertIn("Delete PublicMetaDB lists when disabled in SyncMeta", html)
        self.assertNotIn('href="/impressum"', html)
        self.assertNotIn('href="/datenschutz"', html)
        self.assertNotIn('href="/terms"', html)
        self.assertNotIn('href="/cookie-settings"', html)
        self.assertNotIn("Sync Series", html)
        self.assertNotIn("Sync Movies", html)
        self.assertNotIn("Sync Anime", html)

    def test_legal_pages_render(self) -> None:
        impressum = self.client.get("/impressum")
        privacy = self.client.get("/datenschutz")
        terms = self.client.get("/terms")
        cookies = self.client.get("/cookie-settings")

        self.assertEqual(impressum.status_code, 404)
        self.assertEqual(privacy.status_code, 404)
        self.assertEqual(terms.status_code, 404)
        self.assertEqual(cookies.status_code, 404)

    def test_site_access_password_gate(self) -> None:
        web.SITE_ACCESS_PASSWORD = "letmein"

        blocked = self.client.get("/")
        self.assertEqual(blocked.status_code, 401)
        self.assertIn("Private Access", blocked.get_data(as_text=True))

        api_blocked = self.client.post("/api/profile/status", json={})
        self.assertEqual(api_blocked.status_code, 401)
        self.assertEqual(api_blocked.get_json()["error"], "Site password required")

        wrong = self.client.post("/access", data={"password": "wrong"})
        self.assertEqual(wrong.status_code, 200)
        self.assertIn("Wrong site password.", wrong.get_data(as_text=True))

        unlocked = self.client.post("/access", data={"password": "letmein"}, follow_redirects=True)
        self.assertEqual(unlocked.status_code, 200)
        self.assertIn("Sync Your Lists.<br>Keep Them Fresh.", unlocked.get_data(as_text=True))

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

    def test_login_uses_session_and_masks_saved_secrets(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "simkl-client",
                "client_secret": "super-secret",
                "access_token": "simkl-token",
                "selected_statuses": {"shows": ["watching"], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [],
            },
            "mdblist": {
                "api_key": "",
                "selected_lists": [],
            },
            "pmdb": {
                "api_key": "pmdb-secret",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 600,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows"],
        })

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        login_data = login_response.get_json()

        self.assertEqual(login_response.status_code, 200)
        self.assertIn("Set-Cookie", login_response.headers)
        self.assertTrue(login_data["profile"]["credentials"]["simkl"]["client_secret_saved"])
        self.assertTrue(login_data["profile"]["credentials"]["pmdb"]["api_key_saved"])
        self.assertNotIn("api_key", login_data["profile"]["credentials"]["pmdb"])

        status_response = self.client.post("/api/profile/status", json={"include_credentials": True})
        status_data = status_response.get_json()

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_data["profile"]["profile_id"], profile["profile_id"])
        self.assertTrue(status_data["profile"]["credentials"]["simkl"]["access_token_saved"])
        self.assertNotIn("access_token", status_data["profile"]["credentials"]["simkl"])

    def test_delete_profile_endpoint_removes_signed_in_profile(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "simkl-client",
                "client_secret": "",
                "access_token": "simkl-token",
                "selected_statuses": {"shows": ["watching"], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [],
            },
            "mdblist": {
                "api_key": "",
                "selected_lists": [],
            },
            "pmdb": {
                "api_key": "pmdb-secret",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 600,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows"],
        })

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        delete_response = self.client.post("/api/profile/delete", json={"confirm_text": "DELETE"})
        delete_data = delete_response.get_json()

        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_data["status"], "deleted")

        with self.assertRaises(KeyError):
            web._profile_store.get_private_profile_by_id(profile["profile_id"])


if __name__ == "__main__":
    unittest.main()
