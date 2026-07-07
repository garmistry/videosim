import tempfile
import unittest
from pathlib import Path

from videosim.feed_store import SqliteFeedStore


class FeedStoreTest(unittest.TestCase):
    def test_sqlite_store_round_trips_feed_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feeds.sqlite3"
            store = SqliteFeedStore(path)
            store.upsert(
                {
                    "id": "stream-1",
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
                    "dash_dir": "/tmp/videosim-dash/stream-1",
                    "alert_enabled_ids": ["essence_video_present"],
                    "alert_delay_seconds": 3,
                }
            )
            store.close()

            reloaded = SqliteFeedStore(path)
            feeds = reloaded.load()

        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]["source"], "external")
        self.assertEqual(feeds[0]["external_url"], "srt://camera.local:9999?mode=caller")
        self.assertEqual(feeds[0]["alert_enabled_ids"], ["essence_video_present"])
        self.assertEqual(feeds[0]["alert_delay_seconds"], 3)


if __name__ == "__main__":
    unittest.main()
