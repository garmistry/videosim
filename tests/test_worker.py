import unittest
from unittest.mock import patch

from videosim.worker import run_worker


class WorkerTest(unittest.TestCase):
    def test_run_worker_once_monitors_assigned_streams_and_posts_report(self):
        assignments = {"streams": [{"id": "stream-1", "status": "running"}]}
        monitor_state = {"updatedAt": "now", "alarms": [], "events": [], "pending": []}

        with patch("videosim.worker.fetch_assignments", return_value=assignments) as fetch, patch(
            "videosim.worker.run_monitor_once", return_value=monitor_state
        ) as monitor, patch("videosim.worker.post_report", return_value={"ok": True}) as post, patch(
            "videosim.worker.time.time", return_value=123.0
        ):
            code = run_worker("http://master:8080", "worker-a", 5, 7, 20, "app", once=True)

        self.assertEqual(code, 0)
        fetch.assert_called_once_with("http://master:8080", "worker-a")
        monitor.assert_called_once()
        self.assertEqual(monitor.call_args.args[0], {"streams": assignments["streams"]})
        self.assertEqual(monitor.call_args.args[2], 123.0)
        self.assertEqual(monitor.call_args.args[3], 7)
        self.assertEqual(monitor.call_args.args[4], 20)
        self.assertEqual(monitor.call_args.args[5], "app")
        post.assert_called_once_with("http://master:8080", "worker-a", ["stream-1"], monitor_state)


if __name__ == "__main__":
    unittest.main()
