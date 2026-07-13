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
| Create stream. | GUI state can add a named SRT or DASH stream record with an independent endpoint; PostgreSQL replicas use UUID-backed IDs, atomically allocate generated SRT ports, and fail closed on ID collision or port exhaustion. |
| Read/open stream. | GUI state exposes the selected stream detail, endpoint, status, logs, validation output, and `/feeds/<stream-id>` deep link. |
| Update stream. | GUI can change selected stream name, protocol, or mode. |
| Delete stream. | GUI can stop and remove a selected stream record. |
| List streams. | SQLite GUI state exposes local records; PostgreSQL GUI state pages the shared catalog in 100-row keyset pages with a 200-row API maximum. |
| Separate create from detail. | `/` exposes the create/list workflow; selected feed detail pages omit the create-feed form. |
| Run multiple streams. | Unit tests prove separate stream records launch independent feed subprocesses. |
| Start empty. | Fresh GUI state has no configured streams, no endpoint, and no running feed until the user creates one. |
| Boot and validate the stack. | The environment-gated startup workflow boots app/worker services, drives create/start/validate/stop through HTTP, proves normal-feed video/audio/captions, and captures Docker logs; the fixture-domain workflow separately boots load services, checks DASH HTTP plus real SRT/DASH media, and captures process/log evidence. The runbook also defines a Chrome check. |
| Show per-feed metrics. | GUI state and React UI expose per-stream estimated bit rate, outbound total data, uptime, and generated frame count with polling updates, plus five-minute detail-page traffic graphs. |
| Monitor running feeds. | A separate monitor process/container polls GUI state, validates running feeds, writes alarm/event history, and repeats active alarm events every 5 seconds. |
| Review alarms in UI. | GUI state and React UI expose monitor alarms plus per-stream event audit history. |
| Configure alert profiles per stream. | Each stream exposes enabled monitor IDs and alarm delay, the GUI can create/update profiles before monitor state exists, users can enable or disable monitors, and the monitor filters, delays, raises, and clears alarms from the profile. |
| TR 101 290 monitoring. | Parser-backed MPEG-2 TS analyzer raises monitor alarms for priority 1/2 indicators plus priority 3 PSI/SI, unreferenced-PID, and T-STD timing indicators. |
| Frame-rate validation. | Feed creation/update exposes 23.97, 24, 25, 50, 59.94, and 60 fps choices, uses rational GStreamer caps for fractional rates, and raises a monitor alarm when measured video frame rate differs from the configured rate. |
| Loudness monitoring. | Audio-present feeds raise alarms for ITU-R BS.1770 measurement failure, EBU R 128 integrated/true-peak loudness violations, and ATSC A/85 integrated loudness violations. |

## Distributed Scale Evidence

| Capability | Acceptance evidence |
|---|---|
| Assignment consistency at the F5 candidate shape. | `verify-assignments` checks a read-only repeatable PostgreSQL snapshot for exact 1,320-stream authority, capacity, protocol/domain balance, and bounded one-domain ownership movement. Independent-host capture remains pending. |
| Alarm consistency at the F5 candidate shape. | P0 PostgreSQL coverage projects 1,320 unhealthy results through current check/alarm state, immutable transition payloads, and outbox rows; an inconclusive follow-up preserves active state and a false clear without a matching edge fails. `verify-alarm-consistency` emits the operator evidence report. Independent-host 24-hour evidence remains pending. |
| Durable control-plane load at the F5 candidate shape. | `control-plane-load` uses an empty disposable PostgreSQL database to sustain the exact workload's leases, heartbeats, profile-cadenced fenced reports, current state, and accepted-result outbox evidence. P0 coverage proves one 1,320-stream/33-worker tick and a compressed real-expiry recovery from one 11-worker loss. Retained evidence with the configured 30-second candidate default moved all 440 affected owners to 22 survivors at 29.888/29.889-second p95/p99, kept healthy ownership stable, and fenced the stale failed-owner report. Independent media, endpoint faults, physical domains, and 24-hour admission remain pending. |

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
| 11 Receiver compatibility | complete | Docker live test proves GStreamer, ffprobe, and ffplay compatibility for all required modes; limitations are documented in `docs/compatibility-report.md`. |
| 12 Soak and stability | in progress | Timed soak harness plus short Docker normal/outage and GUI responsiveness proofs exist; 24-hour evidence remains pending. |
