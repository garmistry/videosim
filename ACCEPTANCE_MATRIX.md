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

## Added DASH Protocol Option

| Capability | Acceptance evidence |
|---|---|
| DASH feeds can be generated. | `videosim start --profile profiles/dash-normal.yaml --dash-dir ...` writes an MPD and media segments. |
| DASH supports required simulation modes. | Live smoke validation passes for normal, audio_only, video_only, no_captions, black_video, and frozen_video DASH profiles. |
| DASH captions are present when expected. | Validator detects a WebVTT subtitle adaptation and `captions.vtt` for caption-enabled DASH profiles. |
| DASH endpoint is copyable in GUI. | GUI state exposes `http://127.0.0.1:<http-port>/dash/<stream-id>/manifest.mpd` when protocol is DASH. |

## Added Multi-Feed GUI Operations

| Capability | Acceptance evidence |
|---|---|
| Create stream. | GUI state can add a named SRT or DASH stream record with an independent endpoint. |
| Read/open stream. | GUI state exposes the selected stream detail, endpoint, status, logs, validation output, and `/feeds/<stream-id>` deep link. |
| Update stream. | GUI can change selected stream name, protocol, or mode. |
| Delete stream. | GUI can stop and remove a selected stream record. |
| List streams. | GUI payload and left navigation expose all configured stream records. |
| Separate create from detail. | `/` exposes the create/list workflow; selected feed detail pages omit the create-feed form. |
| Run multiple streams. | Unit tests prove separate stream records launch independent feed subprocesses. |
| Start empty. | Fresh GUI state has no configured streams, no endpoint, and no running feed until the user creates one. |
| Show per-feed metrics. | GUI state and React UI expose per-stream estimated bit rate, outbound total data, uptime, and generated frame count with polling updates. |

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
| 8 GUI outage mode selection | complete | GUI mode selector starts all six required modes and Docker live validation confirms selected stream state. |
| 9 Runtime fault controls | complete | GUI runtime controls toggle video, audio, captions, black video, and frozen video via controlled restart, with Docker live validation after each transition. |
| 10 Observability and troubleshooting | complete | GUI shows status, intentional outage state, last error, logs, validation output, and exports diagnostics text. |
| 11 Receiver compatibility | complete | Docker live test proves GStreamer, ffprobe, and ffplay compatibility for all required modes; limitations are documented in `COMPATIBILITY_REPORT.md`. |
| 12 Soak and stability | in progress | Timed soak harness plus short Docker normal/outage and GUI responsiveness proofs exist; 24-hour evidence remains pending. |
