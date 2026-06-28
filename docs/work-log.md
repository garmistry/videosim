# Work Log

## 2026-06-28

- Added dependency installer for Linux and macOS at `scripts/install-deps.sh`.
- Added repository rule to commit successful completions and keep this work log updated.
- Completed Milestone 0 product-contract docs: `README.md`, `TEST_PLAN.md`, `ACCEPTANCE_MATRIX.md`, `TEST_GAPS.md`, `KNOWN_LIMITATIONS.md`, and `RUNBOOK.md`.
- Added `tests/test_docs_contract.py` to enforce critical requirement/test mapping and required feed mode behavior mapping.
- Added Python cache ignores for repeatable local test runs.
- Validation run: `python3 -m unittest tests.test_docs_contract` passed, 4 tests.
- Skipped checks: no live SRT, media, GUI, or Docker checks exist yet; those are blocked by later milestone implementation.
- Added minimal `python3 -m videosim start` CLI for a synthetic video-only SRT listener feed using GStreamer.
- Added CLI tests for endpoint generation, GStreamer command construction, invalid port errors, print-command mode, and Ctrl-C stop handling.
- Validation run: `python3 -m unittest tests.test_docs_contract tests.test_cli_video_feed` passed, 9 tests.
- Manual smoke: `python3 -m videosim start --port 9911 --width 320 --height 180 --framerate 10` started, printed `srt://127.0.0.1:9911?mode=caller`, and stopped with Ctrl-C exit code 0.
- Skipped checks: automated receiver video detection and restart proof are still missing, so Milestone 1 remains open.
