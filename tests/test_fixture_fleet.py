import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

from videosim.cli import main
from videosim.fixture_fleet import (
    build_dash_fixture_handler,
    fixture_state,
    load_fixture_manifest,
    srt_fixture_commands,
)


ROOT = Path(__file__).resolve().parents[1]


class FixtureFleetTest(unittest.TestCase):
    def test_repository_endpoint_manifests_build_exact_500_url_mix(self):
        for protocol in ("srt", "dash"):
            with self.subTest(protocol=protocol):
                manifest, digest = load_fixture_manifest(
                    ROOT / f"scale/fixtures/{protocol}-endpoints-500.json"
                )
                state = fixture_state(manifest, digest)

                self.assertEqual(len(state["streams"]), 500)
                self.assertEqual(
                    len({stream["endpoint"] for stream in state["streams"]}), 500
                )
                self.assertEqual(
                    state["behaviorEndpointCounts"],
                    {"healthy": 400, "slow": 50, "dead": 25, "malformed": 25},
                )

    def test_repository_dash_manifest_builds_four_benchmark_streams(self):
        manifest, digest = load_fixture_manifest(
            ROOT / "scale/fixtures/dash-matrix.json"
        )

        state = fixture_state(manifest, digest)

        self.assertEqual(state["fixtureManifestSha256"], digest)
        self.assertEqual(
            [stream["id"] for stream in state["streams"]],
            ["dash-healthy", "dash-slow", "dash-dead", "dash-malformed"],
        )
        self.assertTrue(
            all(stream["status"] == "running" for stream in state["streams"])
        )

    def test_dash_http_fixtures_serve_healthy_slow_dead_and_malformed(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "manifest.mpd").write_bytes(b"healthy-mpd")
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), build_dash_fixture_handler(directory, 30)
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                self.assertEqual(
                    urlopen(f"{base}/healthy/manifest.mpd", timeout=2).read(),
                    b"healthy-mpd",
                )
                self.assertEqual(
                    urlopen(
                        f"{base}/healthy/endpoint-00001/manifest.mpd", timeout=2
                    ).read(),
                    b"healthy-mpd",
                )
                with patch("videosim.fixture_fleet.time.sleep") as sleep:
                    self.assertEqual(
                        urlopen(f"{base}/slow/manifest.mpd", timeout=2).read(),
                        b"healthy-mpd",
                    )
                sleep.assert_called_once_with(30)
                self.assertEqual(
                    urlopen(f"{base}/malformed/manifest.mpd", timeout=2).read(),
                    b"<MPD><broken",
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(f"{base}/dead/manifest.mpd", timeout=2)
                self.assertEqual(raised.exception.code, 503)
                raised.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_repository_srt_manifest_builds_four_transport_streams(self):
        manifest, digest = load_fixture_manifest(
            ROOT / "scale/fixtures/srt-matrix.json"
        )

        state = fixture_state(manifest, digest)

        self.assertEqual(
            [stream["id"] for stream in state["streams"]],
            ["srt-healthy", "srt-slow", "srt-dead", "srt-malformed"],
        )
        self.assertEqual(
            [stream["endpoint"] for stream in state["streams"]],
            [
                f"srt://127.0.0.1:{port}?mode=caller"
                for port in range(19081, 19085)
            ],
        )

    @patch("videosim.fixture_fleet.require_gst_launch", return_value="gst-launch-1.0")
    def test_srt_commands_use_real_feed_stall_and_malformed_payload(self, _require):
        manifest, _digest = load_fixture_manifest(
            ROOT / "scale/fixtures/srt-matrix.json"
        )

        healthy, slow, malformed = srt_fixture_commands(manifest)

        self.assertIn("videosim", healthy)
        self.assertIn("19081", healthy)
        self.assertIn("sleep-time=30000000", slow)
        self.assertIn("uri=srt://:19082?mode=listener", slow)
        self.assertIn("filltype=pattern", malformed)
        self.assertIn("uri=srt://:19084?mode=listener", malformed)

    @patch("videosim.fixture_fleet.require_gst_launch", return_value="gst-launch-1.0")
    def test_manifest_expands_unique_behavior_endpoints_and_srt_commands(self, _require):
        with tempfile.TemporaryDirectory() as directory:
            source = json.loads(
                (ROOT / "scale/fixtures/srt-matrix.json").read_text(
                    encoding="utf-8"
                )
            )
            source["behaviorEndpointCounts"] = {
                "healthy": 2,
                "slow": 1,
                "dead": 1,
                "malformed": 2,
            }
            path = Path(directory, "manifest.json")
            path.write_text(json.dumps(source), encoding="utf-8")

            manifest, digest = load_fixture_manifest(path)
            state = fixture_state(manifest, digest)
            commands = srt_fixture_commands(manifest)

        self.assertEqual(len(state["streams"]), 6)
        self.assertEqual(len({stream["endpoint"] for stream in state["streams"]}), 6)
        self.assertEqual(len(commands), 5)
        self.assertIn("19081", commands[0])
        self.assertIn("19082", commands[1])
        self.assertIn("uri=srt://:19086?mode=listener", commands[-1])

    def test_fixture_fleet_rejects_unsupported_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            source = json.loads(
                (ROOT / "scale/fixtures/dash-matrix.json").read_text(
                    encoding="utf-8"
                )
            )
            source["protocol"] = "rist"
            path = Path(directory, "manifest.json")
            path.write_text(json.dumps(source), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must be dash or srt"):
                load_fixture_manifest(path)

    def test_fixture_fleet_rejects_srt_port_range_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            source = json.loads(
                (ROOT / "scale/fixtures/srt-matrix.json").read_text(
                    encoding="utf-8"
                )
            )
            source["basePort"] = 65533
            path = Path(directory, "manifest.json")
            path.write_text(json.dumps(source), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "basePort"):
                load_fixture_manifest(path)

            source["basePort"] = 65532
            source["behaviorEndpointCounts"] = {
                "healthy": 2,
                "slow": 1,
                "dead": 1,
                "malformed": 1,
            }
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "endpoint count"):
                load_fixture_manifest(path)

    def test_fixture_fleet_cli_delegates(self):
        with patch("videosim.cli.run_fixture_fleet", return_value=0) as run:
            code = main(
                [
                    "fixture-fleet",
                    "--manifest",
                    "fixtures.json",
                    "--state-path",
                    "state.json",
                ]
            )

        self.assertEqual(code, 0)
        run.assert_called_once_with("fixtures.json", "state.json", "")


if __name__ == "__main__":
    unittest.main()
