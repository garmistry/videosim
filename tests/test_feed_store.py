import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from videosim.feed_store import SqliteFeedStore, default_feed_store


class FeedStoreTest(unittest.TestCase):
    def feed(self, feed_id="stream-1"):
        return {
            "id": feed_id,
            "name": "External camera",
            "source": "external",
            "external_url": "srt://camera.local:9999?mode=caller",
            "protocol": "srt",
            "mode": "normal",
            "http_port": 8080,
            "feed_port": 9000,
            "width": 1280,
            "height": 720,
            "framerate": "59.94",
            "dash_dir": f"/tmp/videosim-dash/{feed_id}",
            "alert_enabled_ids": ["essence_video_present"],
            "alert_delay_seconds": 3,
        }

    def test_database_url_selects_bounded_postgres_store(self):
        with patch.dict(
            os.environ,
            {
                "VIDEOSIM_DATABASE_URL": "postgresql://db/videosim",
                "VIDEOSIM_DB_POOL_MIN": "2",
                "VIDEOSIM_DB_POOL_MAX": "7",
            },
        ), patch("videosim.postgres_store.PostgresControlPlaneStore") as store_class:
            selected = default_feed_store()

        self.assertIs(selected, store_class.return_value)
        store_class.assert_called_once_with(
            "postgresql://db/videosim", min_pool_size=2, max_pool_size=7
        )

    def test_sqlite_store_round_trips_feed_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feeds.sqlite3"
            store = SqliteFeedStore(path)
            store.upsert(self.feed())
            store.close()

            reloaded = SqliteFeedStore(path)
            feeds = reloaded.load()

        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]["source"], "external")
        self.assertEqual(feeds[0]["external_url"], "srt://camera.local:9999?mode=caller")
        self.assertEqual(feeds[0]["alert_enabled_ids"], ["essence_video_present"])
        self.assertEqual(feeds[0]["alert_delay_seconds"], 3)

    def test_sqlite_store_accepts_gui_request_thread_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteFeedStore(Path(directory) / "feeds.sqlite3")
            errors = []

            def write_from_request_thread():
                try:
                    store.upsert(self.feed("stream-2"))
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=write_from_request_thread)
            thread.start()
            thread.join()
            feeds = store.load()

        self.assertEqual(errors, [])
        self.assertEqual([feed["id"] for feed in feeds], ["stream-2"])


if __name__ == "__main__":
    unittest.main()
