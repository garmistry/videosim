# TEST_PLAN.md

# Video Feed Simulator Test Plan

## Test Weights

| Priority | Weight | Meaning |
|---|---:|---|
| P0 | 5 | Critical MVP behavior |
| P1 | 3 | Important reliability or UX behavior |
| P2 | 2 | Useful but non-blocking behavior |
| P3 | 1 | Polish or edge behavior |

Weighted coverage is `covered test weight / total expected test weight`.

## Milestone Gates

Each milestone may advance only when all P0 tests for that milestone are
implemented and passing, weighted coverage meets the milestone threshold, blocker
and critical defects are closed, gaps are recorded in [TEST_GAPS.md](TEST_GAPS.md),
and a human-visible demo path exists.

Core outage milestones require stricter coverage:

- Static outage profiles: 95%.
- GUI outage selection: 95%.
- Runtime fault toggles: 95%.
- Version 1.0: 98%.

## Critical Requirement Test Mapping

| Requirement ID | Critical requirement | Planned acceptance tests |
|---|---|---|
| CR1 | Run on Linux. | M1-P0-start-srt-video-feed, M7-P0-gui-launches, M11-P0-gstreamer-receiver-consumes-required-modes |
| CR2 | Provide a visual GUI. | M7-P0-gui-launches, M7-P0-gui-starts-normal-feed, M8-P0-gui-starts-all-required-modes |
| CR3 | Generate local SRT feeds. | M1-P0-start-srt-video-feed, M1-P0-receiver-detects-video, M5-P0-all-static-profiles-validate |
| CR4 | Generate a normal feed with video, audio, and closed captions. | M2-P0-normal-feed-contains-video, M2-P0-normal-feed-contains-audio, M3-P0-captions-present-in-normal-feed |
| CR5 | Support all required fault modes. | M5-P0-normal-profile-validates, M5-P0-audio-only-profile-validates, M5-P0-video-only-profile-validates, M5-P0-no-caption-profile-validates, M5-P0-black-video-profile-validates, M5-P0-frozen-video-profile-validates |
| CR6 | Show a copyable SRT endpoint URL. | M7-P0-gui-shows-copyable-srt-url |
| CR7 | Provide start/stop controls. | M1-P0-stop-feed, M1-P0-restart-feed, M7-P0-gui-stops-feed |
| CR8 | Provide basic logs. | M1-P1-log-output-includes-start-stop-failure, M10-P0-logs-visible |
| CR9 | Provide clear error states. | M1-P1-invalid-port-handling, M10-P0-failed-feed-shows-error |
| CR10 | Provide validation that proves actual stream state. | M6-P0-validate-normal-feed, M6-P0-validate-audio-only-feed, M6-P0-validate-video-only-feed, M6-P0-validate-no-caption-feed, M6-P0-validate-black-video-feed, M6-P0-validate-frozen-video-feed, M6-P0-validate-stopped-feed |
| CR11 | Include install/run documentation. | M0-P0-required-docs-exist, M0-P0-runbook-has-install-and-verify-path |
| CR12 | Include tests for all critical behavior. | M0-P0-critical-requirements-map-to-tests, M0-P0-required-feed-modes-map-to-tests |

## Required Feed Mode Matrix

| Mode | Video track | Video content | Audio track | Captions | Planned acceptance tests |
|---|---|---|---|---|---|
| normal | present | moving/generated | present | present | M5-P0-normal-profile-validates, M6-P0-validate-normal-feed, M8-P0-gui-starts-normal-mode |
| audio_only | absent | n/a | present | absent or explicitly unsupported | M5-P0-audio-only-profile-validates, M6-P0-validate-audio-only-feed, M8-P0-gui-starts-audio-only-mode |
| video_only | present | moving/generated | absent | present | M5-P0-video-only-profile-validates, M6-P0-validate-video-only-feed, M8-P0-gui-starts-video-only-mode |
| no_captions | present | moving/generated | present | absent | M5-P0-no-caption-profile-validates, M6-P0-validate-no-caption-feed, M8-P0-gui-starts-no-caption-mode |
| black_video | present | black frames | present | present | M5-P0-black-video-profile-validates, M6-P0-validate-black-video-feed, M8-P0-gui-starts-black-video-mode |
| frozen_video | present | static/repeated frame | present | present | M5-P0-frozen-video-profile-validates, M6-P0-validate-frozen-video-feed, M8-P0-gui-starts-frozen-video-mode |

## Milestone 0 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M0-P0-critical-requirements-map-to-tests | P0 | 5 | implemented | `python3 -m unittest tests.test_docs_contract` |
| M0-P0-required-feed-modes-map-to-tests | P0 | 5 | implemented | `python3 -m unittest tests.test_docs_contract` |
| M0-P0-required-docs-exist | P0 | 5 | implemented | `python3 -m unittest tests.test_docs_contract` |
| M0-P0-runbook-has-install-and-verify-path | P0 | 5 | implemented | `python3 -m unittest tests.test_docs_contract` |

Milestone 0 weighted coverage: 20 / 20 = 100%.

## Milestone 1 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M1-P0-start-srt-video-feed | P0 | 5 | implemented | `python3 -m unittest tests.test_cli_video_feed`; `docker compose run --build --rm live-srt` |
| M1-P0-receiver-detects-video | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M1-P0-stop-feed | P0 | 5 | implemented | `python3 -m unittest tests.test_cli_video_feed`; `docker compose run --build --rm live-srt` |
| M1-P0-restart-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M1-P1-invalid-port-handling | P1 | 3 | implemented | `python3 -m unittest tests.test_cli_video_feed` |
| M1-P1-log-output-includes-start-stop-failure | P1 | 3 | implemented | `python3 -m unittest tests.test_cli_video_feed` |

Milestone 1 weighted coverage: 26 / 26 = 100%.

## Milestone 2 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M2-P0-normal-feed-contains-video | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M2-P0-normal-feed-contains-audio | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M2-P0-receiver-consumes-audio-video-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M2-P1-audio-continuity-30-minutes | P1 | 3 | not implemented | tracked in `TEST_GAPS.md` |
| M2-P1-audio-config-error-handling | P1 | 3 | implemented | `python3 -m unittest tests.test_cli_video_feed` |

Milestone 2 weighted coverage: 18 / 21 = 85.7%.

## Milestone 3 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M3-P0-captions-present-in-normal-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M3-P0-captions-can-be-detected | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M3-P0-video-audio-still-present-when-captions-enabled | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M3-P1-caption-text-updates-over-time | P1 | 3 | not implemented | tracked in `TEST_GAPS.md` |
| M3-P1-caption-disabled-negative-test | P1 | 3 | implemented | `docker compose run --build --rm live-srt` |

Milestone 3 weighted coverage: 18 / 21 = 85.7%.

## Milestone 4 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M4-P0-valid-normal-profile-runs | P0 | 5 | implemented | `python3 -m unittest tests.test_profiles` |
| M4-P0-invalid-profile-fails-safely | P0 | 5 | implemented | `python3 -m unittest tests.test_profiles` |
| M4-P0-profile-maps-to-expected-video-audio-caption-state | P0 | 5 | implemented | `python3 -m unittest tests.test_profiles` |
| M4-P1-missing-fields-reported-clearly | P1 | 3 | implemented | `python3 -m unittest tests.test_profiles` |
| M4-P1-schema-version-checked | P1 | 3 | implemented | `python3 -m unittest tests.test_profiles` |

Milestone 4 weighted coverage: 21 / 21 = 100%.

## Milestone 5 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M5-P0-normal-profile-validates | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M5-P0-audio-only-profile-validates | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M5-P0-video-only-profile-validates | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M5-P0-no-caption-profile-validates | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M5-P0-black-video-profile-validates | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M5-P0-frozen-video-profile-validates | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M5-P1-unknown-outage-mode-fails-clearly | P1 | 3 | implemented | `python3 -m unittest tests.test_profiles` |
| M5-P1-receiver-does-not-crash-on-any-outage-scenario | P1 | 3 | implemented | `docker compose run --build --rm live-srt` |

Milestone 5 weighted coverage: 36 / 36 = 100%.

## Milestone 6 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M6-P0-validate-normal-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M6-P0-validate-audio-only-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M6-P0-validate-video-only-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M6-P0-validate-no-caption-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M6-P0-validate-black-video-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M6-P0-validate-frozen-video-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M6-P0-validate-stopped-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M6-P1-false-positive-check | P1 | 3 | implemented | absent-track, no-caption, and stopped-feed live checks |
| M6-P1-false-negative-check | P1 | 3 | implemented | all expected-present live profile checks |
| M6-P1-clear-validation-failure-messages | P1 | 3 | implemented | `python3 -m unittest tests.test_validator` |

Milestone 6 weighted coverage: 44 / 44 = 100%.

## Milestone 7 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M7-P0-gui-launches | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M7-P0-gui-starts-normal-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M7-P0-receiver-consumes-gui-started-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M7-P0-gui-stops-feed | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M7-P1-logs-shown-in-gui | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |
| M7-P1-backend-start-failure-shown-clearly | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |

Milestone 7 weighted coverage: 26 / 26 = 100%.

## Milestone 8 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M8-P0-gui-starts-normal-mode | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M8-P0-gui-starts-audio-only-mode | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M8-P0-gui-starts-video-only-mode | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M8-P0-gui-starts-no-caption-mode | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M8-P0-gui-starts-black-video-mode | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M8-P0-gui-starts-frozen-video-mode | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M8-P0-gui-state-matches-validation-output | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M8-P1-mode-labels-understandable | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |
| M8-P1-invalid-combinations-blocked | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |

Milestone 8 weighted coverage: 41 / 41 = 100%.

## Milestone 9 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M9-P0-toggle-video-off-on | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M9-P0-toggle-audio-off-on | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M9-P0-toggle-captions-off-on | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M9-P0-toggle-black-video-off-on | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M9-P0-toggle-frozen-video-off-on | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M9-P0-gui-state-equals-validation-result | P0 | 5 | implemented | `docker compose run --build --rm live-srt` |
| M9-P1-toggle-event-logging | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |
| M9-P1-contradictory-states-prevented | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |

Milestone 9 weighted coverage: 36 / 36 = 100%.

## Milestone 10 Tests

| Test ID | Priority | Weight | Status | Command |
|---|---:|---:|---|---|
| M10-P0-failed-feed-shows-error | P0 | 5 | implemented | `python3 -m unittest tests.test_gui` |
| M10-P0-intentional-outage-shown-as-intentional | P0 | 5 | implemented | `python3 -m unittest tests.test_gui` |
| M10-P0-logs-visible | P0 | 5 | implemented | `python3 -m unittest tests.test_gui` |
| M10-P0-logs-exportable | P0 | 5 | implemented | `python3 -m unittest tests.test_gui` |
| M10-P1-validation-output-visible-in-gui | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |
| M10-P1-error-messages-actionable | P1 | 3 | implemented | `python3 -m unittest tests.test_gui` |

Milestone 10 weighted coverage: 26 / 26 = 100%.
