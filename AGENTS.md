# Repository Rules

- On successful task completion, create a git commit that includes only the files changed for that task.
- Keep [docs/work-log.md](docs/work-log.md) updated with concise dated entries for completed work, validation run, and any skipped checks.
- Do not commit broken or unverified work; if verification cannot run, log the reason.

## Project

Video Feed Simulator is a Linux application that generates local live video feeds for testing receivers, monitoring systems, and outage handling.

The MVP focuses on SRT.

## Final MVP Requirements

### Critical MVP

The application must:

1. Run on Linux.
2. Provide a visual GUI.
3. Generate local SRT feeds.
4. Generate a normal feed with:
   - Video present.
   - Audio present.
   - Closed captions present.
5. Support fault modes:
   - Audio only: audio present, video absent.
   - Video only: video present, audio absent.
   - No captions: video/audio present, captions absent.
   - Black video: video track present, black frames.
   - Frozen video: video track present, repeated/static frame content.
6. Show a copyable SRT endpoint URL.
7. Provide start/stop controls.
8. Provide basic logs.
9. Provide clear error states.
10. Provide validation that proves actual stream state.
11. Include install/run documentation.
12. Include tests for all critical behavior.

### Nice-to-have, not MVP blocking

Do not implement these unless all critical MVP requirements are done:

- Multiple protocols beyond SRT.
- RTP, RIST, RTMP, HLS, DASH.
- Multiple simultaneous feeds.
- Seamless fault toggles without stream restart.
- Local preview window.
- Packet loss simulation.
- Jitter simulation.
- Caption delay simulation.
- Caption corruption simulation.
- REST API.
- Prometheus metrics.
- AppImage, Flatpak, or Docker packaging.
- Multi-language captions.
- User roles/authentication.

## Architecture Preference

Prefer an architecture with these layers:

1. GUI layer.
2. Feed orchestration layer.
3. Feed profile/config layer.
4. Media pipeline layer.
5. Protocol output layer.
6. Validation layer.
7. Observability/logging layer.

The implementation may use GStreamer, FFmpeg, or another suitable Linux media stack, but the selected approach must be documented.

## Required Feed Modes

| Mode | Video track | Video content | Audio track | Captions |
|---|---|---|---|---|
| normal | present | moving/generated | present | present |
| audio_only | absent | n/a | present | absent or explicitly unsupported |
| video_only | present | moving/generated | absent | present |
| no_captions | present | moving/generated | present | absent |
| black_video | present | black frames | present | present |
| frozen_video | present | static/repeated frame | present | present |

## Testing Policy

A milestone may advance only when:

1. 100% of P0 critical tests for that milestone are implemented.
2. 100% of P0 critical tests are passing.
3. Overall weighted test coverage is at least 85%.
4. No blocker defects remain.
5. No critical defects remain.
6. Any missing non-critical tests are documented in TEST_GAPS.md.
7. The milestone has a human-visible demo path.

For core outage milestones, use stricter gates:

- Static outage profiles: at least 95% weighted coverage.
- GUI outage selection: at least 95% weighted coverage.
- Runtime fault toggles: at least 95% weighted coverage.
- Version 1.0: at least 98% weighted coverage.

## Test Priority Weights

| Priority | Weight | Meaning |
|---|---:|---|
| P0 | 5 | Critical MVP behavior |
| P1 | 3 | Important reliability or UX behavior |
| P2 | 2 | Useful but non-blocking behavior |
| P3 | 1 | Polish or edge behavior |

Weighted coverage formula:

covered test weight / total expected test weight

## Required Validation Capabilities

The validation tool must be able to verify:

1. Feed reachable.
2. Video track present.
3. Video track absent.
4. Audio track present.
5. Audio track absent.
6. Captions present.
7. Captions absent.
8. Black video detected.
9. Frozen video detected.
10. Feed stopped/unreachable.

## Black Video Acceptance Criteria

A black-video feed passes validation when:

1. Video track is present.
2. Sampled frames are black or near-black.
3. At least 95% of sampled pixels are below the configured black threshold.
4. Audio remains present unless intentionally disabled.
5. Captions remain present unless intentionally disabled.

## Frozen Video Acceptance Criteria

A frozen-video feed passes validation when:

1. Video track is present.
2. Sampled frames over time are visually identical or nearly identical.
3. Timestamps continue advancing.
4. Audio remains present unless intentionally disabled.
5. Captions remain present unless intentionally disabled.
6. Validation samples across at least 10 seconds.

## Required Documentation

Maintain these files:

- README.md
- CODEX_GOALS.md
- TEST_PLAN.md
- TEST_GAPS.md
- ACCEPTANCE_MATRIX.md
- KNOWN_LIMITATIONS.md
- RUNBOOK.md

## Development Rules

1. Do not skip tests for P0 behavior.
2. Do not claim a milestone is complete without test evidence.
3. Do not hide unsupported caption behavior.
4. Do not let GUI state diverge from validated stream state.
5. Prefer small vertical slices over large untested rewrites.
6. If a milestone cannot fully complete, leave a clear progress note and keep the goal active.
7. Keep changes reviewable.
8. Update docs as implementation changes.
