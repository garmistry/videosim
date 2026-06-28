from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]

REQUIRED_DOCS = [
    "README.md",
    "CODEX_GOALS.md",
    "TEST_PLAN.md",
    "TEST_GAPS.md",
    "ACCEPTANCE_MATRIX.md",
    "KNOWN_LIMITATIONS.md",
    "RUNBOOK.md",
]

CRITICAL_REQUIREMENTS = [f"CR{i}" for i in range(1, 13)]

REQUIRED_MODES = {
    "normal": ("present", "moving/generated", "present", "present"),
    "audio_only": ("absent", "n/a", "present", "absent or explicitly unsupported"),
    "video_only": ("present", "moving/generated", "absent", "present"),
    "no_captions": ("present", "moving/generated", "present", "absent"),
    "black_video": ("present", "black frames", "present", "present"),
    "frozen_video": ("present", "static/repeated frame", "present", "present"),
}


def read_doc(path):
    return (ROOT / path).read_text(encoding="utf-8")


def markdown_rows(text):
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in text.splitlines()
        if line.startswith("|") and not re.fullmatch(r"[|\-: ]+", line)
    ]


class DocsContractTest(unittest.TestCase):
    def test_required_docs_exist(self):
        for doc in REQUIRED_DOCS:
            with self.subTest(doc=doc):
                self.assertTrue((ROOT / doc).is_file())

    def test_every_critical_requirement_maps_to_planned_tests(self):
        rows = markdown_rows(read_doc("TEST_PLAN.md"))
        by_id = {row[0]: row for row in rows if row and row[0] in CRITICAL_REQUIREMENTS}

        self.assertEqual(set(CRITICAL_REQUIREMENTS), set(by_id))
        for requirement_id, row in by_id.items():
            with self.subTest(requirement=requirement_id):
                self.assertRegex(row[-1], r"M\d+-P[01]-")

    def test_required_feed_modes_have_expected_behavior_and_tests(self):
        rows = markdown_rows(read_doc("TEST_PLAN.md"))
        by_mode = {row[0]: row for row in rows if row and row[0] in REQUIRED_MODES}

        self.assertEqual(set(REQUIRED_MODES), set(by_mode))
        for mode, expected in REQUIRED_MODES.items():
            with self.subTest(mode=mode):
                row = by_mode[mode]
                self.assertEqual(tuple(row[1:5]), expected)
                self.assertRegex(row[-1], r"M\d+-P0-")

    def test_runbook_has_install_and_verify_path(self):
        runbook = read_doc("RUNBOOK.md")

        self.assertIn("scripts/install-deps.sh", runbook)
        self.assertIn("python3 -m unittest tests.test_docs_contract", runbook)


if __name__ == "__main__":
    unittest.main()
