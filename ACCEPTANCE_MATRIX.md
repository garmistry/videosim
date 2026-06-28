# ACCEPTANCE_MATRIX.md

# Acceptance Matrix

## Critical MVP Requirements

| ID | Requirement | Acceptance evidence |
|---|---|---|
| CR1 | Run on Linux. | Linux install path works and Linux GUI/CLI tests pass. |
| CR2 | Provide a visual GUI. | GUI launches and controls a feed in P0 GUI tests. |
| CR3 | Generate local SRT feeds. | Receiver connects to local SRT endpoint and detects expected streams. |
| CR4 | Generate normal feed with video, audio, and closed captions. | Validator detects all three in normal mode. |
| CR5 | Support required fault modes. | Validator confirms every required mode matches the truth table below. |
| CR6 | Show copyable SRT endpoint URL. | GUI exposes an SRT URL that can be copied and consumed. |
| CR7 | Provide start/stop controls. | CLI and GUI start/stop/restart tests pass. |
| CR8 | Provide basic logs. | Start, stop, and failure events are visible. |
| CR9 | Provide clear error states. | Invalid config, dependency, port, and pipeline errors are shown clearly. |
| CR10 | Provide validation that proves actual stream state. | Validator detects track presence/absence, black video, frozen video, and stopped feed. |
| CR11 | Include install/run documentation. | README and RUNBOOK include setup, run, receive, validate, and troubleshooting steps. |
| CR12 | Include tests for all critical behavior. | TEST_PLAN maps each critical requirement to P0/P1 acceptance tests. |

## Required Feed Modes

| Mode | Video track | Video content | Audio track | Captions | Acceptance evidence |
|---|---|---|---|---|---|
| normal | present | moving/generated | present | present | Validator reports video, audio, captions, and non-static non-black video. |
| audio_only | absent | n/a | present | absent or explicitly unsupported | Validator reports audio present, video absent, captions absent/unsupported. |
| video_only | present | moving/generated | absent | present | Validator reports video and captions present, audio absent. |
| no_captions | present | moving/generated | present | absent | Validator reports video/audio present and captions absent. |
| black_video | present | black frames | present | present | Validator reports video present, audio present, captions present, and black-frame criteria pass. |
| frozen_video | present | static/repeated frame | present | present | Validator reports video present, audio present, captions present, static frame content, and advancing timestamps. |

## Validation Capabilities

| Capability | Acceptance evidence |
|---|---|
| Feed reachable. | Validator exits success and records endpoint reachable. |
| Video track present. | Validator reports a video stream for modes that require video. |
| Video track absent. | Validator reports no video stream for audio_only. |
| Audio track present. | Validator reports an audio stream for modes that require audio. |
| Audio track absent. | Validator reports no audio stream for video_only. |
| Captions present. | Validator reports captions for modes that require captions. |
| Captions absent. | Validator reports no captions for no_captions and audio_only. |
| Black video detected. | At least 95% of sampled pixels are below the black threshold. |
| Frozen video detected. | Frames sampled across at least 10 seconds are visually identical or nearly identical while timestamps advance. |
| Feed stopped/unreachable. | Validator fails reachability with a clear stopped/unreachable result. |

## Milestone Status

| Milestone | Status | Human-visible output |
|---|---|---|
| 0 Repo and product contract | complete | Docs exist and contract test passes. |
| 1 CLI SRT video feed | complete | CLI starts/stops/restarts a synthetic video SRT listener and Docker live test proves receiver video detection. |
| 2 Add audio | complete | CLI emits audio/video SRT and Docker live test proves receiver consumes H.264 video plus AAC audio. |
| 3 Add closed captions | complete | CLI inserts generated CEA-608 captions into H.264 and Docker live test extracts them; no-caption negative test passes. |
| 4 Feed profiles | complete | Flat YAML normal profile loads, validates schema version, maps to feed config, and drives CLI command generation. |
| 5 Static outage profiles | complete | Six sample profiles validate live in Docker, including track absence, no captions, black frames, and frozen frames. |
| 6 Automated validation tool | complete | `videosim validate` emits human/JSON reports and Docker live tests cover all modes plus stopped feed. |
| 7 Minimal Linux GUI | complete | Local browser GUI launches, shows copyable endpoint/logs/state, starts/stops normal feed, and Docker live test validates the GUI-started feed. |
| 8 GUI outage mode selection | not started | Pending mode selector. |
| 9 Runtime fault controls | not started | Pending restart-backed toggles. |
| 10 Observability and troubleshooting | not started | Pending diagnostics. |
| 11 Receiver compatibility | not started | Pending compatibility report. |
| 12 Soak and stability | not started | Pending soak harness. |
