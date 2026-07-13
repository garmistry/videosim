import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from videosim.cli import main
from videosim.fixture_fleet import fixture_state, load_fixture_manifest
from videosim.fixture_scenario import run_fixture_scenario


ROOT = Path(__file__).resolve().parents[1]


class FixtureScenarioTest(unittest.TestCase):
    def write_fixture_state(self, directory: str, protocol: str) -> Path:
        manifest, digest = load_fixture_manifest(
            ROOT / f"scale/fixtures/{protocol}-matrix.json"
        )
        path = Path(directory) / f"{protocol}.json"
        path.write_text(json.dumps(fixture_state(manifest, digest)), encoding="utf-8")
        return path

    def test_repository_manifest_expands_exact_deterministic_mix(self):
        with tempfile.TemporaryDirectory() as directory:
            srt = self.write_fixture_state(directory, "srt")
            dash = self.write_fixture_state(directory, "dash")
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"

            with patch("builtins.print"):
                run_fixture_scenario(
                    str(ROOT / "scale/fixtures/mixed-1000.json"),
                    str(srt),
                    str(dash),
                    str(first),
                )
                run_fixture_scenario(
                    str(ROOT / "scale/fixtures/mixed-1000.json"),
                    str(srt),
                    str(dash),
                    str(second),
                )
            state = json.loads(first.read_text(encoding="utf-8"))
            first_bytes = first.read_bytes()
            second_bytes = second.read_bytes()

        self.assertEqual(first_bytes, second_bytes)
        self.assertTrue(state["logicalStreamsShareEndpoints"])
        self.assertEqual(state["protocolCounts"], {"srt": 500, "dash": 500})
        self.assertEqual(
            state["behaviorCounts"],
            {"healthy": 800, "slow": 100, "dead": 50, "malformed": 50},
        )
        self.assertEqual(len(state["streams"]), 1000)
        self.assertEqual(len({stream["id"] for stream in state["streams"]}), 1000)
        self.assertEqual(
            Counter(stream["protocol"] for stream in state["streams"]),
            {"srt": 500, "dash": 500},
        )

    def test_rejects_invalid_percentages_and_missing_fixture_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            srt = self.write_fixture_state(directory, "srt")
            dash = self.write_fixture_state(directory, "dash")
            manifest = json.loads(
                (ROOT / "scale/fixtures/mixed-1000.json").read_text(
                    encoding="utf-8"
                )
            )
            manifest["protocolPercent"]["srt"] = 40
            invalid = Path(directory) / "invalid.json"
            invalid.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "percentages must total 100"):
                run_fixture_scenario(
                    str(invalid), str(srt), str(dash), str(Path(directory) / "out.json")
                )

            state = json.loads(srt.read_text(encoding="utf-8"))
            state["streams"] = [
                stream for stream in state["streams"] if stream["id"] != "srt-dead"
            ]
            srt.write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "running external dead"):
                run_fixture_scenario(
                    str(ROOT / "scale/fixtures/mixed-1000.json"),
                    str(srt),
                    str(dash),
                    str(Path(directory) / "out.json"),
                )

    def test_multiple_behavior_endpoints_compose_an_independent_url_scenario(self):
        with tempfile.TemporaryDirectory() as directory:
            states = {}
            for protocol in ("srt", "dash"):
                path = self.write_fixture_state(directory, protocol)
                state = json.loads(path.read_text(encoding="utf-8"))
                expanded = []
                for behavior_index, stream in enumerate(state["streams"]):
                    behavior = stream["id"].removeprefix(f"{protocol}-")
                    for instance in range(2):
                        item = dict(stream)
                        item["id"] = f"{stream['id']}-{instance}"
                        item["fixtureBehavior"] = behavior
                        item["endpoint"] = (
                            f"srt://127.0.0.1:{21000 + behavior_index * 2 + instance}?mode=caller"
                            if protocol == "srt"
                            else f"http://127.0.0.1:18081/{behavior}/{instance}/manifest.mpd"
                        )
                        expanded.append(item)
                state["streams"] = expanded
                path.write_text(json.dumps(state), encoding="utf-8")
                states[protocol] = path

            manifest = json.loads(
                (ROOT / "scale/fixtures/mixed-1000.json").read_text(
                    encoding="utf-8"
                )
            )
            manifest["streamCount"] = 16
            manifest["behaviorPercent"] = {
                behavior: 25
                for behavior in ("healthy", "slow", "dead", "malformed")
            }
            manifest_path = Path(directory) / "scenario.json"
            output = Path(directory) / "output.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with patch("builtins.print"):
                run_fixture_scenario(
                    str(manifest_path),
                    str(states["srt"]),
                    str(states["dash"]),
                    str(output),
                )
            scenario = json.loads(output.read_text(encoding="utf-8"))

        self.assertFalse(scenario["logicalStreamsShareEndpoints"])
        self.assertEqual(
            len({stream["endpoint"] for stream in scenario["streams"]}), 16
        )
        self.assertFalse(
            any(stream["fixtureEndpointShared"] for stream in scenario["streams"])
        )
        self.assertIn(
            "distinct protocol endpoint", scenario["scenarioLimitations"][0]
        )

    def test_cli_delegates_fixture_scenario(self):
        with patch("videosim.cli.run_fixture_scenario", return_value=0) as run:
            code = main(
                [
                    "fixture-scenario",
                    "--manifest",
                    "scenario.json",
                    "--srt-state",
                    "srt.json",
                    "--dash-state",
                    "dash.json",
                    "--state-path",
                    "state.json",
                ]
            )

        self.assertEqual(code, 0)
        run.assert_called_once_with(
            "scenario.json", "srt.json", "dash.json", "state.json"
        )


if __name__ == "__main__":
    unittest.main()
