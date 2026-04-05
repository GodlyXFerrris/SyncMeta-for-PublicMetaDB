import unittest

from src.config import PublicMetaDBConfig
from src.publicmetadb_client import PublicMetaDBClient


class PublicMetaDBClientTests(unittest.TestCase):
    def test_get_watched_history_paginates_all_pages(self) -> None:
        client = PublicMetaDBClient(PublicMetaDBConfig(api_key="pmdb-key"))
        calls: list[tuple[str, dict | None]] = []

        def fake_get(path: str, params: dict | None = None):
            calls.append((path, params))
            page = int((params or {}).get("page", 1))
            if page == 1:
                return {
                    "items": [{"id": "w1"}, {"id": "w2"}],
                    "totalPages": 2,
                }
            if page == 2:
                return {
                    "items": [{"id": "w3"}],
                    "totalPages": 2,
                }
            return {"items": [], "totalPages": 2}

        client._get = fake_get  # type: ignore[method-assign]

        items = client.get_watched_history()

        self.assertEqual([item["id"] for item in items], ["w1", "w2", "w3"])
        self.assertEqual(calls, [
            ("/api/external/watched", {"page": 1, "perPage": 100}),
            ("/api/external/watched", {"page": 2, "perPage": 100}),
        ])

    def test_clear_watched_history_deletes_all_entries_with_ids(self) -> None:
        client = PublicMetaDBClient(PublicMetaDBConfig(api_key="pmdb-key"))
        deleted_ids: list[str] = []

        client.get_watched_history = lambda: [  # type: ignore[method-assign]
            {"id": "w1"},
            {"id": "w2"},
            {"tmdb_id": 5},
            {"id": "w3"},
        ]

        def fake_delete(watched_id: str) -> bool:
            deleted_ids.append(watched_id)
            return True

        client.delete_watched_entry = fake_delete  # type: ignore[method-assign]

        deleted_count = client.clear_watched_history()

        self.assertEqual(deleted_count, 3)
        self.assertEqual(deleted_ids, ["w1", "w2", "w3"])


if __name__ == "__main__":
    unittest.main()
