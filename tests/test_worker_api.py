import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from videosim.gui import GuiHandler, GuiState


class WorkerApiTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = GuiState(
            monitor_state_path=str(Path(self.directory.name) / "monitor.json"),
            control_plane_instance_id="master-api-test",
        )
        self.state.create_stream(
            name="Camera A",
            source="external",
            external_url="srt://camera-a.local:9000?mode=caller",
        )
        handler = type("WorkerApiHandler", (GuiHandler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def assignment(self):
        with urlopen(f"{self.base_url}/api/workers/assignments?worker_id=worker-a", timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, payload):
        request = Request(
            f"{self.base_url}/api/workers/report",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urlopen(request, timeout=2)

    def post_path(self, path, payload):
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urlopen(request, timeout=2)

    def report_payload(self, assignment):
        return {
            "workerId": "worker-a",
            "streamIds": ["stream-1"],
            "state": {"updatedAt": "now", "alarms": [], "events": [], "pending": []},
            **{field: assignment[field] for field in (
                "apiVersion",
                "controlPlaneInstanceId",
                "assignmentGeneration",
                "assignmentToken",
            )},
        }

    def test_valid_versioned_report_returns_200(self):
        assignment = self.assignment()

        with self.post(self.report_payload(assignment)) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["legacyContract"])

    def test_worker_can_drain_after_registration(self):
        self.assignment()

        with self.post_path(
            "/api/workers/drain", {"workerId": "worker-a"}
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertNotIn("worker-a", self.state.worker_seen)

    def test_stale_assignment_returns_409_without_mutation(self):
        assignment = self.assignment()
        assignment["assignmentToken"] = "stale"

        with self.assertRaises(HTTPError) as raised:
            self.post(self.report_payload(assignment))

        self.assertEqual(raised.exception.code, 409)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        raised.exception.close()
        self.assertTrue(payload["retryAssignment"])
        self.assertFalse(Path(self.state.monitor_state_path).exists())

    def test_malformed_report_item_returns_422(self):
        assignment = self.assignment()
        payload = self.report_payload(assignment)
        payload["state"]["alarms"] = [{"id": "missing-stream"}]

        with self.assertRaises(HTTPError) as raised:
            self.post(payload)

        self.assertEqual(raised.exception.code, 422)
        raised.exception.close()

    def test_non_string_worker_id_returns_400(self):
        request = Request(
            f"{self.base_url}/api/workers/report",
            data=b'{"workerId": 123, "streamIds": [], "state": {}}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=2)

        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_invalid_utf8_worker_json_returns_400(self):
        request = Request(
            f"{self.base_url}/api/workers/report",
            data=b"\xff\xfe",
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=2)

        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()

    def test_persistence_failure_returns_retryable_503(self):
        assignment = self.assignment()

        with patch("videosim.gui.write_monitor_payload", return_value=False), self.assertRaises(HTTPError) as raised:
            self.post(self.report_payload(assignment))

        self.assertEqual(raised.exception.code, 503)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        raised.exception.close()
        self.assertTrue(payload["retryReport"])


if __name__ == "__main__":
    unittest.main()
