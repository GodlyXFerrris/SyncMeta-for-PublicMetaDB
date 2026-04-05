import json
import tempfile
import unittest
from pathlib import Path

from src.profile_store import MIN_SYNC_INTERVAL_SECONDS, MIN_WATCHED_HISTORY_INTERVAL_SECONDS, ProfileStore


class ProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = ProfileStore(Path(self.tmpdir.name) / "profiles.json")
        self.credentials = {
            "simkl": {
                "client_id": "simkl-client",
                "client_secret": "",
                "access_token": "simkl-token",
                "selected_statuses": {
                    "shows": ["watching", "completed"],
                    "movies": ["plantowatch"],
                    "anime": [],
                },
            },
            "anilist": {
                "username": "",
                "access_token": "",
                "selected_statuses": ["CURRENT", "COMPLETED"],
            },
            "mdblist": {
                "api_key": "mdbl-key",
                "selected_lists": [{
                    "id": 11,
                    "name": "Top Movies",
                    "slug": "top-movies",
                    "user_name": "demo",
                    "description": "desc",
                    "mediatype": "movie",
                    "items": 10,
                    "likes": 5,
                    "type": "user",
                    "private": False,
                }],
            },
            "pmdb": {
                "api_key": "pm-key",
            },
        }
        self.options = {
            "auto_sync": True,
            "interval_seconds": 600,
            "remove_missing": False,
            "delete_disabled_lists": False,
            "simkl_visibility": "private",
            "anilist_visibility": "private",
            "trakt_personal_visibility": "private",
            "trakt_public_visibility": "public",
            "mdblist_visibility": "public",
            "media_types": ["shows", "movies"],
        }

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_create_profile_persists_and_authenticates(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)
        private_loaded = self.store.get_private_profile_by_id(created["profile_id"])
        payload = json.loads((Path(self.tmpdir.name) / "profiles.json").read_text(encoding="utf-8"))

        self.assertEqual(loaded["profile_id"], created["profile_id"])
        self.assertTrue(loaded["credentials"]["pmdb"]["api_key_saved"])
        self.assertNotIn("credentials", payload["profiles"][created["profile_id"]])
        self.assertIn("credentials_encrypted", payload["profiles"][created["profile_id"]])
        self.assertEqual(private_loaded["credentials"]["pmdb"]["api_key"], "pm-key")
        self.assertEqual(loaded["options"]["interval_seconds"], 600)
        self.assertEqual(loaded["options"]["trakt_watched_history_interval_seconds"], 43200)
        self.assertFalse(loaded["options"]["delete_disabled_lists"])
        self.assertEqual(loaded["options"]["simkl_visibility"], "private")
        self.assertEqual(loaded["options"]["trakt_public_visibility"], "public")
        self.assertEqual(loaded["credentials"]["simkl"]["selected_statuses"]["shows"], ["watching", "completed"])
        self.assertEqual(loaded["credentials"]["anilist"]["selected_statuses"], ["CURRENT", "COMPLETED"])
        self.assertEqual(loaded["credentials"]["mdblist"]["selected_lists"][0]["id"], 11)
        self.assertIsNotNone(loaded["next_sync_at"])

    def test_rejects_interval_below_minimum(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_profile("secret", self.credentials, {
                **self.options,
                "interval_seconds": MIN_SYNC_INTERVAL_SECONDS - 1,
            })

    def test_rejects_watched_history_interval_below_minimum(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_profile("secret", self.credentials, {
                **self.options,
                "trakt_watched_history_interval_seconds": MIN_WATCHED_HISTORY_INTERVAL_SECONDS - 1,
            })

    def test_manual_dry_run_does_not_advance_schedule(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        claimed = self.store.claim_profile_for_sync(created["profile_id"], "secret")
        next_sync_before = claimed["next_sync_at"]

        self.store.record_sync_success(created["profile_id"], [{"list_name": "demo"}], dry_run=True)
        loaded = self.store.get_profile(created["profile_id"], "secret")

        self.assertEqual(loaded["next_sync_at"], next_sync_before)
        self.assertEqual(len(loaded["history"]), 1)
        self.assertTrue(loaded["history"][0]["dry_run"])

    def test_request_sync_cancel_and_record_cancelled(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        self.store.claim_profile_for_sync(created["profile_id"], "secret")

        stopping = self.store.request_sync_cancel(created["profile_id"])
        self.assertTrue(stopping["sync_cancel_requested"])
        self.assertEqual(stopping["sync_status"], "Stopping...")
        self.assertTrue(self.store.is_sync_cancel_requested(created["profile_id"]))

        stopped = self.store.record_sync_cancelled(created["profile_id"])
        self.assertFalse(stopped["sync_running"])
        self.assertFalse(stopped["sync_cancel_requested"])
        self.assertEqual(stopped["sync_status"], "Stopped")

    def test_sync_success_persists_managed_lists(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)

        self.store.record_sync_success(created["profile_id"], [{
            "list_name": "Watching - Series",
        }], managed_lists=[{
            "list_name": "SIMKL - Series - Watching",
            "list_id": "pmdb-1",
            "display_name": "Watching - Series",
            "source_name": "SIMKL",
            "selection": {
                "source": "simkl",
                "media_type": "shows",
                "status": "watching",
            },
        }])

        reloaded_store = ProfileStore(Path(self.tmpdir.name) / "profiles.json")
        loaded = reloaded_store.get_profile(created["profile_id"], "secret", include_credentials=True)

        self.assertEqual(loaded["last_results"][0]["list_name"], "Watching - Series")
        self.assertFalse(loaded["options"]["delete_disabled_lists"])
        self.assertEqual(reloaded_store._profiles[created["profile_id"]]["managed_lists"][0]["list_id"], "pmdb-1")
        self.assertEqual(
            reloaded_store._profiles[created["profile_id"]]["managed_lists"][0]["selection"]["status"],
            "watching",
        )

    def test_delete_managed_list_by_id_removes_selection_and_results(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        self.store.record_sync_success(created["profile_id"], [{
            "list_name": "Top Movies",
        }], managed_lists=[{
            "list_name": "Top Movies",
            "list_id": "pmdb-2",
            "display_name": "Top Movies",
            "source_name": "MDBList",
            "selection": {
                "source": "mdblist",
                "id": 11,
                "mediatype": "movie",
            },
        }])

        updated_credentials = dict(self.credentials)
        updated_credentials["mdblist"] = {
            **self.credentials["mdblist"],
            "selected_lists": [],
        }

        updated = self.store.delete_managed_list_by_id(created["profile_id"], "Top Movies", updated_credentials)

        self.assertEqual(updated["sync_status"], "Managed list deleted")
        self.assertEqual(updated["last_results"], [])
        self.assertEqual(self.store._profiles[created["profile_id"]]["managed_lists"], [])
        self.assertEqual(self.store._profiles[created["profile_id"]]["credentials"]["mdblist"]["selected_lists"], [])

    def test_delete_profile_by_id_removes_records(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)

        self.store.delete_profile_by_id(created["profile_id"])

        with self.assertRaises(KeyError):
            self.store.get_private_profile_by_id(created["profile_id"])

    def test_update_by_id_keeps_saved_secrets_when_fields_are_blank(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)

        updated = self.store.update_profile_by_id(created["profile_id"], {
            "simkl": {
                "client_id": "simkl-client",
                "client_secret": "",
                "access_token": "",
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
                "api_key": "",
            },
        }, self.options)

        private_loaded = self.store.get_private_profile_by_id(created["profile_id"])

        self.assertTrue(updated["credentials"]["pmdb"]["api_key_saved"])
        self.assertEqual(private_loaded["credentials"]["simkl"]["access_token"], "simkl-token")
        self.assertEqual(private_loaded["credentials"]["mdblist"]["api_key"], "mdbl-key")
        self.assertEqual(private_loaded["credentials"]["pmdb"]["api_key"], "pm-key")

    def test_empty_source_statuses_stay_empty_until_user_selects_them(self) -> None:
        created = self.store.create_profile("secret", {
            **self.credentials,
            "simkl": {
                **self.credentials["simkl"],
                "selected_statuses": {},
            },
            "anilist": {
                **self.credentials["anilist"],
                "selected_statuses": [],
            },
        }, self.options)

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)

        self.assertEqual(loaded["credentials"]["simkl"]["selected_statuses"], {
            "shows": [],
            "movies": [],
            "anime": [],
        })
        self.assertEqual(loaded["credentials"]["anilist"]["selected_statuses"], [])

    def test_legacy_default_trakt_catalogs_are_cleared_until_user_opts_in(self) -> None:
        created = self.store.create_profile("secret", {
            **self.credentials,
            "trakt": {
                "client_id": "trakt-client",
                "client_secret": "",
                "access_token": "trakt-token",
                "refresh_token": "",
                "username": "",
                "sync_watchlist": False,
                "sync_liked_lists": False,
                "selected_lists": [{
                    "name": "Trending Movies",
                    "user": "default",
                    "slug": "trending-movies",
                    "source": "default",
                    "catalog_key": "trending-movies",
                }],
            },
        }, self.options)

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)

        self.assertFalse(loaded["credentials"]["trakt"]["default_catalogs_initialized"])
        self.assertEqual(loaded["credentials"]["trakt"]["selected_lists"], [])

    def test_activity_sync_is_manual_only_even_when_enabled(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "trakt_sync_watched_history": True,
            "trakt_sync_resume_progress": True,
        })

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)
        due_profiles = self.store.claim_due_profiles()

        self.assertIsNone(loaded["next_history_sync_at"])
        self.assertIsNone(loaded["next_resume_sync_at"])
        self.assertEqual(len(due_profiles), 1)
        self.assertTrue(due_profiles[0]["pending_sync_modes"]["lists"])
        self.assertFalse(due_profiles[0]["pending_sync_modes"]["history"])
        self.assertFalse(due_profiles[0]["pending_sync_modes"]["resume"])

    def test_update_profile_by_id_preserves_existing_next_list_sync_time(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)
        self.store.claim_profile_for_sync(created["profile_id"], "secret")
        after_sync = self.store.record_sync_success(created["profile_id"], [{"list_name": "demo"}], dry_run=False)
        next_sync_before = after_sync["next_sync_at"]

        updated = self.store.update_profile_by_id(created["profile_id"], self.credentials, {
            **self.options,
            "trakt_sync_watched_history": True,
            "trakt_sync_resume_progress": True,
        })

        due_profiles = self.store.claim_due_profiles()

        self.assertEqual(updated["next_sync_at"], next_sync_before)
        self.assertEqual(due_profiles, [])

    def test_activity_only_sync_keeps_existing_last_list_results(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "trakt_sync_resume_progress": True,
        })
        self.store.claim_profile_for_sync(created["profile_id"], "secret")
        self.store.record_sync_success(created["profile_id"], [{
            "list_name": "Watching - Series",
            "display_name": "Watching - Series",
            "source_name": "SIMKL",
        }], dry_run=False, sync_modes={"lists": True, "history": False, "resume": False})

        self.store.claim_profile_for_sync(created["profile_id"], "secret", sync_modes={"lists": False, "history": False, "resume": True})
        updated = self.store.record_sync_success(created["profile_id"], [{
            "list_name": "",
            "display_name": "Trakt Resume Progress",
            "source_name": "Trakt",
            "items_fetched": 6,
        }], dry_run=False, sync_modes={"lists": False, "history": False, "resume": True})

        self.assertEqual(len(updated["last_results"]), 1)
        self.assertEqual(updated["last_results"][0]["display_name"], "Watching - Series")
        self.assertIn("resume_progress", updated["activity_results"])
        self.assertEqual(
            updated["activity_results"]["resume_progress"]["row"]["display_name"],
            "Trakt Resume Progress",
        )

    def test_history_sync_persists_latest_source_cursor(self) -> None:
        created = self.store.create_profile("secret", self.credentials, {
            **self.options,
            "activity_history_source": "simkl",
        })

        self.store.claim_profile_for_sync(created["profile_id"], "secret", sync_modes={"lists": False, "history": True, "resume": False})
        updated = self.store.record_sync_success(created["profile_id"], [{
            "list_name": "",
            "display_name": "SIMKL Watch History",
            "source_name": "SIMKL",
            "items_fetched": 5,
            "history_cursor": "2026-04-05T12:00:00Z",
        }], dry_run=False, sync_modes={"lists": False, "history": True, "resume": False})

        private_loaded = self.store.get_private_profile_by_id(created["profile_id"])

        self.assertEqual(updated["activity_results"]["watch_history"]["row"]["history_cursor"], "2026-04-05T12:00:00Z")
        self.assertEqual(private_loaded["activity_state"]["simkl_history_cursor"], "2026-04-05T12:00:00Z")


if __name__ == "__main__":
    unittest.main()
