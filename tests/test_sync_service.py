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
        self.created_lists: list[str] = []

    def get_or_create_list(self, name: str, description: str) -> dict:
        self.created_lists.append(name)
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
        self.assertEqual(pmdb.created_lists, ["Watching - Series"])
        self.assertEqual(
            [item["list_name"] for item in service.managed_lists],
            ["Watching - Series"],
        )

    def test_keeps_disabled_managed_lists_when_option_off(self) -> None:
        service, pmdb = self.build_service(delete_disabled_lists=False)

        service.run()

        self.assertEqual(pmdb.deleted_lists, [])
        self.assertEqual(pmdb.created_lists, ["Watching - Series"])
        self.assertEqual(
            [item["list_name"] for item in service.managed_lists],
            ["SIMKL - Series - Watching", "Trakt List - demo - old-list", "Watching - Series"],
        )


if __name__ == "__main__":
    unittest.main()
