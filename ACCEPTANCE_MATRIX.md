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
| 1 CLI SRT video feed | not started | Pending CLI feed command. |
| 2 Add audio | not started | Pending audio/video feed. |
| 3 Add closed captions | not started | Pending generated captions. |
| 4 Feed profiles | not started | Pending profile schema and loader. |
| 5 Static outage profiles | not started | Pending six sample profiles. |
| 6 Automated validation tool | not started | Pending validator command. |
| 7 Minimal Linux GUI | not started | Pending GUI shell. |
| 8 GUI outage mode selection | not started | Pending mode selector. |
| 9 Runtime fault controls | not started | Pending restart-backed toggles. |
| 10 Observability and troubleshooting | not started | Pending diagnostics. |
| 11 Receiver compatibility | not started | Pending compatibility report. |
| 12 Soak and stability | not started | Pending soak harness. |
