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
- Added Docker Linux test harness and live SRT integration test at `tests/test_live_srt.py`.
- Added stop fallback from SIGINT to terminate/kill for stuck media child processes.
- Validation run: `python3 -m unittest tests.test_docs_contract tests.test_cli_video_feed tests.test_live_srt` passed, 12 tests with the live test skipped locally by default.
- Docker validation run: `docker compose run --build --rm test` passed, 11 tests.
- Docker live validation run: `docker compose run --build --rm live-srt` passed, 1 test proving SRT receiver video detection and restart on the same port.
- Completed Milestone 1 CLI SRT video feed gate at 100% weighted coverage.
- Added generated AAC audio to the default SRT feed with `audiotestsrc` and `--audio-frequency` validation; kept `--no-audio` for video-only operation.
- Updated Docker live SRT test to prove one receiver consumes both H.264 video and AAC audio, then restarts on the same port.
- Validation run: `python3 -m unittest tests.test_docs_contract tests.test_cli_video_feed tests.test_live_srt` passed, 14 tests with the live test skipped locally by default.
- Docker validation run: `docker compose run --build --rm test` passed, 13 tests.
- Docker live validation run: `docker compose run --build --rm live-srt` passed, 1 test proving audio/video receiver consumption.
- Completed Milestone 2 add-audio gate at 85.7% weighted coverage; skipped P1 30-minute continuity test is recorded in `TEST_GAPS.md`.
- Added generated CEA-608 caption insertion using `fdsrc`, `cccombiner`, and `h264ccinserter`; added `--no-captions`.
- Updated live Docker receiver test to extract captions with `h264ccextractor` and added a no-caption negative check.
- Validation run: `python3 -m unittest tests.test_docs_contract tests.test_cli_video_feed tests.test_live_srt` passed, 17 tests with 2 live tests skipped locally by default.
- Docker validation run: `docker compose run --build --rm test` passed, 15 tests.
- Docker live validation run: `docker compose run --build --rm live-srt` passed, 2 tests proving captions present and absent when disabled.
- Completed Milestone 3 add-closed-captions gate at 85.7% weighted coverage; skipped P1 caption update-over-time verification is recorded in `TEST_GAPS.md`.
- Added flat YAML profile loader, `profiles/srt-normal.yaml`, and `python3 -m videosim start --profile`.
- Added profile tests for valid normal profile loading, CLI command generation, invalid mode failure, missing required fields, schema version checks, and CLI profile error reporting.
- Validation run: `python3 -m unittest discover -s tests` passed, 23 tests with 2 live tests skipped locally by default.
- Docker validation run: `docker compose run --build --rm test` passed, 23 tests.
- Docker live regression run: `docker compose run --build --rm live-srt` passed, 2 tests.
- Completed Milestone 4 feed-profile gate at 100% weighted coverage.
