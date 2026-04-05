import unittest

from src.config import AppConfig, PublicMetaDBConfig, SimklConfig, SyncConfig
from src.sync_service import SyncService


class StubSimklClient:
    def get_status(self, status_key: str, media_types: list[str]) -> dict[str, list[dict]]:
        if status_key == "watching" and media_types == ["shows"]:
            return {
                "shows": [{
                    "title": "Demo Show",
                    "media_type": "tv",
                }],
            }
        return {media_type: [] for media_type in media_types}


class StubMatcher:
    def resolve_tmdb_id(self, item: dict) -> int | None:
        return 101


class StubPMDBClient:
    def __init__(self) -> None:
        self.deleted_lists: list[str] = []
        self.created_lists: list[dict] = []
        self.watched: list[dict] = []
        self.resume_batches: list[list[dict]] = []

    def get_or_create_list(self, name: str, description: str, is_public: bool = False) -> dict:
        self.created_lists.append({
            "name": name,
            "is_public": is_public,
        })
        return {"id": "pmdb-active", "name": name}

    def get_list_items(self, list_id: str) -> list[dict]:
        return []

    def add_item_to_list(self, list_id: str, tmdb_id: int, media_type: str) -> None:
        return None

    def delete_list(self, list_id: str) -> bool:
        self.deleted_lists.append(list_id)
        return True

    def find_list_by_name(self, name: str) -> dict | None:
        return None

    def get_watched_history(self) -> list[dict]:
        return list(self.watched)

    def mark_watched(
        self,
        tmdb_id: int,
        media_type: str,
        season: int | None = None,
        episode: int | None = None,
        watched_at: str | None = None,
        dedupe: bool = False,
    ) -> dict:
        item = {
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "watched_at": watched_at,
        }
        if season is not None:
            item["season"] = season
        if episode is not None:
            item["episode"] = episode
        self.watched.append(item)
        return {"success": True, "item": item}

    def get_resume_points(self) -> list[dict]:
        return []

    def save_resume_points_batch(self, items: list[dict]) -> dict:
        self.resume_batches.append(list(items))
        return {
            "results": [
                {**item, "action": "completed" if item["position_ms"] >= int(item["runtime_ms"] * 0.8) else "saved"}
                for item in items
            ]
        }


class StubTraktClient:
    def get_watchlist(self) -> list[dict]:
        return [
            {"title": "Watchlist Movie", "media_type": "movie"},
            {"title": "Watchlist Show", "media_type": "tv"},
        ]

    def get_default_catalog(self, catalog_key: str) -> list[dict]:
        return [{"title": f"Default {catalog_key}", "media_type": "movie"}]

    def get_list_items(self, user: str, slug: str) -> list[dict]:
        return [{"title": f"{user}-{slug}", "media_type": "movie"}]

    def get_liked_lists(self) -> list[dict]:
        return []

    def get_watched_history(self) -> list[dict]:
        return [
            {"tmdb_id": 901, "media_type": "movie", "watched_at": "2026-04-01T12:00:00Z", "title": "Watched Movie"},
            {"tmdb_id": 902, "media_type": "tv", "season": 1, "episode": 2, "watched_at": "2026-04-01T13:00:00Z", "title": "Watched Episode"},
        ]

    def get_playback_progress(self) -> list[dict]:
        return [
            {"tmdb_id": 903, "media_type": "movie", "position_ms": 1_800_000, "runtime_ms": 3_600_000, "progress": 50, "title": "Resume Movie"},
            {"tmdb_id": 904, "media_type": "tv", "season": 2, "episode": 5, "position_ms": 3_000_000, "runtime_ms": 3_600_000, "progress": 83.3, "title": "Completed Episode"},
        ]


class StubMdbListClient:
    def get_list_items(self, list_id: int) -> list[dict]:
        return [{"title": f"MDB-{list_id}", "media_type": "movie"}]


class SyncServiceTests(unittest.TestCase):
    def build_service(self, delete_disabled_lists: bool) -> tuple[SyncService, StubPMDBClient]:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={
                    "shows": ["watching"],
                    "movies": [],
                    "anime": [],
                },
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=delete_disabled_lists,
                dry_run=False,
                media_types=["shows"],
            ),
        )
        service = SyncService(config, managed_lists=[
            {
                "list_name": "SIMKL - Series - Watching",
                "list_id": "pmdb-active",
                "display_name": "Watching - Series",
                "source_name": "SIMKL",
            },
            {
                "list_name": "Trakt List - demo - old-list",
                "list_id": "pmdb-disabled",
                "display_name": "old-list",
                "source_name": "Trakt by demo",
            },
        ])
        pmdb = StubPMDBClient()
        service._simkl = StubSimklClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb
        return service, pmdb

    def test_deletes_disabled_managed_lists_when_enabled(self) -> None:
        service, pmdb = self.build_service(delete_disabled_lists=True)

        results = service.run()

        self.assertEqual(len(results), 1)
        self.assertEqual(pmdb.deleted_lists, ["pmdb-active", "pmdb-disabled"])
        self.assertEqual([item["name"] for item in pmdb.created_lists], ["Watching - Series"])
        self.assertEqual(
            [item["list_name"] for item in service.managed_lists],
            ["Watching - Series"],
        )

    def test_keeps_disabled_managed_lists_when_option_off(self) -> None:
        service, pmdb = self.build_service(delete_disabled_lists=False)

        service.run()

        self.assertEqual(pmdb.deleted_lists, [])
        self.assertEqual([item["name"] for item in pmdb.created_lists], ["Watching - Series"])
        self.assertEqual(
            [item["list_name"] for item in service.managed_lists],
            ["SIMKL - Series - Watching", "Trakt List - demo - old-list", "Watching - Series"],
        )

    def test_uses_expected_public_private_visibility_per_source_group(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="simkl-client",
                access_token="simkl-token",
                selected_statuses={
                    "shows": ["watching"],
                    "movies": [],
                    "anime": [],
                },
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies"],
                simkl_visibility="private",
                trakt_personal_visibility="private",
                trakt_public_visibility="public",
                mdblist_visibility="public",
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True
        config.trakt.sync_watchlist = True
        config.trakt.sync_liked_lists = False
        config.trakt.selected_lists = [
            {"name": "Recommended Movies", "user": "me", "slug": "recommended-movies", "source": "default", "catalog_key": "recommended-movies"},
            {"name": "Public Liked", "user": "demo", "slug": "public-liked", "source": "liked"},
            {"name": "Discover Picks", "user": "demo", "slug": "discover-picks", "source": "discover"},
        ]
        config.mdblist.api_key = "mdbl-key"
        config.mdblist.enabled = True
        config.mdblist.selected_lists = [
            {"id": 7, "name": "Popular Netflix Movies", "mediatype": "movie"},
        ]

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._simkl = StubSimklClient()
        service._trakt = StubTraktClient()
        service._mdblist = StubMdbListClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        service.run()

        visibility_by_name = {item["name"]: item["is_public"] for item in pmdb.created_lists}
        self.assertFalse(visibility_by_name["Watching - Series"])
        self.assertFalse(visibility_by_name["Watchlist - Movies"])
        self.assertFalse(visibility_by_name["Watchlist - Series"])
        self.assertFalse(visibility_by_name["Recommended Movies"])
        self.assertTrue(visibility_by_name["Public Liked"])
        self.assertTrue(visibility_by_name["Discover Picks"])
        self.assertTrue(visibility_by_name["Popular Netflix Movies"])

    def test_syncs_trakt_watched_history_and_resume_when_enabled(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="",
                access_token="",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies"],
                trakt_sync_watched_history=True,
                trakt_sync_resume_progress=True,
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.client_secret = "trakt-secret"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True

        service = SyncService(config)
        pmdb = StubPMDBClient()
        service._trakt = StubTraktClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Trakt Watch History")
        resume_stats = next(item for item in results if item.display_name == "Trakt Resume Progress")

        self.assertEqual(watched_stats.items_fetched, 2)
        self.assertEqual(watched_stats.items_added, 2)
        self.assertEqual(len(pmdb.watched), 2)
        self.assertEqual(resume_stats.items_fetched, 2)
        self.assertEqual(resume_stats.items_added, 1)
        self.assertEqual(resume_stats.items_removed, 1)
        self.assertEqual(len(pmdb.resume_batches), 1)

    def test_trakt_watched_history_skips_already_watched_title_even_with_new_timestamp(self) -> None:
        config = AppConfig(
            simkl=SimklConfig(
                client_id="",
                access_token="",
                selected_statuses={"shows": [], "movies": [], "anime": []},
            ),
            pmdb=PublicMetaDBConfig(api_key="pmdb-key"),
            sync=SyncConfig(
                remove_missing=False,
                delete_disabled_lists=False,
                dry_run=False,
                media_types=["shows", "movies"],
                trakt_sync_watched_history=True,
            ),
        )
        config.trakt.client_id = "trakt-client"
        config.trakt.client_secret = "trakt-secret"
        config.trakt.access_token = "trakt-token"
        config.trakt.enabled = True

        service = SyncService(config)
        pmdb = StubPMDBClient()
        pmdb.watched = [
            {"tmdb_id": 901, "media_type": "movie", "watched_at": "2026-03-31T12:00:00Z"},
            {"tmdb_id": 902, "media_type": "tv", "season": 1, "episode": 2, "watched_at": "2026-03-31T13:00:00Z"},
        ]
        service._trakt = StubTraktClient()
        service._matcher = StubMatcher()
        service._pmdb = pmdb

        results = service.run()

        watched_stats = next(item for item in results if item.display_name == "Trakt Watch History")

        self.assertEqual(watched_stats.items_fetched, 2)
        self.assertEqual(watched_stats.items_resolved, 2)
        self.assertEqual(watched_stats.items_added, 0)
        self.assertEqual(watched_stats.items_skipped_duplicate, 2)


if __name__ == "__main__":
    unittest.main()
