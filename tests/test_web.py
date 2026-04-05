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
        self.assertIn("Sync SIMKL watched history", html)
        self.assertIn("Sync SIMKL resume progress", html)
        self.assertIn("Sync Trakt watched history", html)
        self.assertIn("Sync Trakt resume progress", html)
        self.assertIn("Activity Sync", html)
        self.assertIn('id="activity-cards"', html)
        self.assertIn("Sync Watch History", html)
        self.assertIn("Clear PMDB History", html)
        self.assertIn("Sync Resume Progress", html)
        self.assertIn("SIMKL and Trakt activity sync only run from the dashboard buttons", html)
        self.assertIn("Watched history imports only add items that are not already watched in PublicMetaDB", html)
        self.assertIn('id="btn-stop"', html)
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

    @patch("web.TraktClient.poll_device_token")
    def test_trakt_device_check_pending_is_not_reported_as_error(self, mock_poll_device_token) -> None:
        mock_poll_device_token.return_value = {
            "error": "authorization_pending",
            "error_description": "User has not finished authorizing yet.",
        }

        response = self.client.post("/api/trakt/device/check", json={
            "client_id": "client",
            "client_secret": "secret",
            "device_code": "device",
        })
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["status"], "pending")
        self.assertTrue(data["message"])

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

    def test_stop_sync_endpoint_marks_profile_as_stopping(self) -> None:
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
                "api_key": "pmdb-key",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 1800,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows"],
        })

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        web._profile_store.claim_profile_for_sync_by_id(profile["profile_id"])

        stop_response = self.client.post("/api/profile/sync/stop", json={})
        stop_data = stop_response.get_json()

        self.assertEqual(stop_response.status_code, 200)
        self.assertEqual(stop_data["status"], "stopping")
        self.assertTrue(stop_data["profile"]["sync_cancel_requested"])
        self.assertEqual(stop_data["profile"]["sync_status"], "Stopping...")
        private_profile = web._profile_store.get_private_profile_by_id(profile["profile_id"])
        self.assertTrue(private_profile["sync_cancel_requested"])

    @patch("web.PublicMetaDBClient.clear_watched_history")
    def test_clear_watch_history_endpoint_clears_pmdb_history(self, mock_clear_watched_history) -> None:
        mock_clear_watched_history.return_value = 7
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "selected_statuses": {"shows": [], "movies": [], "anime": []},
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
                "api_key": "pmdb-key",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 1800,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows", "movies", "anime"],
        })

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        clear_response = self.client.post("/api/profile/activity/history/clear", json={})
        clear_data = clear_response.get_json()

        self.assertEqual(clear_response.status_code, 200)
        self.assertEqual(clear_data["status"], "cleared")
        self.assertEqual(clear_data["deleted_count"], 7)
        mock_clear_watched_history.assert_called_once()

    def test_activity_sync_endpoint_starts_history_only_run(self) -> None:
        profile = web._profile_store.create_profile("secret", {
            "simkl": {
                "client_id": "",
                "client_secret": "",
                "access_token": "",
                "selected_statuses": {"shows": [], "movies": [], "anime": []},
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": [],
            },
            "trakt": {
                "client_id": "trakt-client",
                "client_secret": "trakt-secret",
                "access_token": "trakt-token",
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
                "api_key": "pmdb-key",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 1800,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows"],
            "trakt_sync_watched_history": True,
            "trakt_watched_history_interval_seconds": 43200,
            "trakt_sync_resume_progress": False,
        })

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        with patch("web.threading.Thread") as mock_thread:
            response = self.client.post("/api/profile/activity/sync", json={"mode": "history"})
            data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["status"], "started")
        self.assertEqual(data["mode"], "history")
        thread_args = mock_thread.call_args.kwargs["args"]
        self.assertEqual(thread_args[2], {"lists": False, "history": True, "resume": False})

    @patch("web.PublicMetaDBClient.delete_list")
    def test_delete_managed_list_endpoint_removes_mdblist_selection(self, mock_delete_list) -> None:
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
                "api_key": "mdbl-key",
                "selected_lists": [{
                    "id": 7,
                    "name": "Favorites",
                    "mediatype": "movie",
                }],
            },
            "pmdb": {
                "api_key": "pmdb-secret",
            },
        }, {
            "auto_sync": True,
            "interval_seconds": 600,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "media_types": ["shows", "movies"],
        })
        web._profile_store.record_sync_success(profile["profile_id"], [{
            "list_name": "Favorites",
            "display_name": "Favorites",
            "source_name": "MDBList",
        }], managed_lists=[{
            "list_name": "Favorites",
            "list_id": "pmdb-list-1",
            "display_name": "Favorites",
            "source_name": "MDBList",
            "selection": {
                "source": "mdblist",
                "id": 7,
                "mediatype": "movie",
            },
        }])

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        delete_response = self.client.post("/api/profile/list/delete", json={"list_name": "Favorites"})
        delete_data = delete_response.get_json()

        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_data["profile"]["last_results"], [])
        self.assertEqual(delete_data["profile"]["credentials"]["mdblist"]["selected_lists"], [])
        private_profile = web._profile_store.get_private_profile_by_id(profile["profile_id"])
        self.assertEqual(private_profile["managed_lists"], [])
        self.assertEqual(private_profile["credentials"]["mdblist"]["selected_lists"], [])
        mock_delete_list.assert_called_once_with("pmdb-list-1")

    @patch("web.PublicMetaDBClient.delete_list")
    def test_delete_managed_list_endpoint_removes_trakt_selection_by_list_name(self, mock_delete_list) -> None:
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
                "client_id": "trakt-client",
                "client_secret": "trakt-secret",
                "access_token": "trakt-token",
                "refresh_token": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [{
                    "name": "Recommended Movies",
                    "user": "trakt",
                    "slug": "recommended-movies",
                    "source": "default",
                    "catalog_key": "recommended-movies",
                }],
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
            "media_types": ["shows", "movies"],
        })
        web._profile_store.record_sync_success(profile["profile_id"], [{
            "list_name": "Recommended Movies",
            "display_name": "Recommended Movies",
            "source_name": "Trakt",
        }], managed_lists=[{
            "list_name": "Recommended Movies",
            "list_id": "pmdb-list-2",
            "display_name": "Recommended Movies",
            "source_name": "Trakt",
            "selection": {},
        }])

        login_response = self.client.post("/api/profile/login", json={
            "profile_id": profile["profile_id"],
            "password": "secret",
        })
        self.assertEqual(login_response.status_code, 200)

        delete_response = self.client.post("/api/profile/list/delete", json={"list_name": "Recommended Movies"})
        delete_data = delete_response.get_json()

        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_data["profile"]["credentials"]["trakt"]["selected_lists"], [])
        private_profile = web._profile_store.get_private_profile_by_id(profile["profile_id"])
        self.assertEqual(private_profile["credentials"]["trakt"]["selected_lists"], [])
        mock_delete_list.assert_called_once_with("pmdb-list-2")


if __name__ == "__main__":
    unittest.main()
