import unittest
from unittest.mock import patch

from videosim.control_plane import (
    AssignmentContract,
    WorkerReportValidationError,
    validate_monitor_items,
    validate_report_contract,
)


class ControlPlaneContractTest(unittest.TestCase):
    def contract(self):
        return AssignmentContract(
            api_version="videosim.worker/v1",
            control_plane_instance_id="master-a",
            assignment_generation=3,
            assignment_token="token",
            worker_id="worker-a",
            stream_ids=("stream-1",),
        )

    def test_partial_contract_metadata_is_rejected(self):
        with self.assertRaises(WorkerReportValidationError):
            validate_report_contract(
                self.contract(),
                {"apiVersion": "videosim.worker/v1"},
                allow_legacy=False,
            )

    def test_collection_limit_is_enforced(self):
        state = {
            "alarms": [
                {"id": "one", "streamId": "stream-1"},
                {"id": "two", "streamId": "stream-1"},
            ],
            "events": [],
            "pending": [],
        }

        with patch("videosim.control_plane.MAX_REPORT_ITEMS_PER_COLLECTION", 1), self.assertRaises(
            WorkerReportValidationError
        ):
            validate_monitor_items(state, {"stream-1"})

    def test_identifier_limit_is_enforced(self):
        state = {
            "alarms": [{"id": "x" * 513, "streamId": "stream-1"}],
            "events": [],
            "pending": [],
        }

        with self.assertRaises(WorkerReportValidationError):
            validate_monitor_items(state, {"stream-1"})


if __name__ == "__main__":
    unittest.main()
