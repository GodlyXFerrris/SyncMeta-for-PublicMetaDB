import tempfile
import unittest
from pathlib import Path

from src.profile_store import MIN_SYNC_INTERVAL_SECONDS, ProfileStore


class ProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = ProfileStore(Path(self.tmpdir.name) / "profiles.json")
        self.credentials = {
            "simkl": {
                "client_id": "simkl-client",
                "client_secret": "",
                "access_token": "simkl-token",
            },
            "anilist": {
                "username": "",
                "access_token": "",
            },
            "pmdb": {
                "api_key": "pm-key",
            },
        }
        self.options = {
            "auto_sync": True,
            "interval_seconds": 600,
            "remove_missing": False,
            "media_types": ["shows", "movies"],
        }

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_create_profile_persists_and_authenticates(self) -> None:
        created = self.store.create_profile("secret", self.credentials, self.options)

        loaded = self.store.get_profile(created["profile_id"], "secret", include_credentials=True)

        self.assertEqual(loaded["profile_id"], created["profile_id"])
        self.assertEqual(loaded["credentials"]["pmdb"]["api_key"], "pm-key")
        self.assertEqual(loaded["options"]["interval_seconds"], 600)
        self.assertIsNotNone(loaded["next_sync_at"])

    def test_rejects_interval_below_minimum(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create_profile("secret", self.credentials, {
                **self.options,
                "interval_seconds": MIN_SYNC_INTERVAL_SECONDS - 1,
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


if __name__ == "__main__":
    unittest.main()
