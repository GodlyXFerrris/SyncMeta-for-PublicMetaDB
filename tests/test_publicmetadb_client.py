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


if __name__ == "__main__":
    unittest.main()
