# CODEX_GOALS.md

# Video Feed Simulator Codex Goal Plan

## Master Goal

Build the Video Feed Simulator MVP for Linux.

The MVP is complete when a user can launch a GUI, create an SRT feed, consume it with a receiver, and validate these six states:

1. Normal feed: video, audio, and captions present.
2. Audio-only feed: audio present, video absent.
3. Video-only feed: video present, audio absent.
4. No-caption feed: video/audio present, captions absent.
5. Black-video feed: video track present, black frames.
6. Frozen-video feed: video track present, static/repeated video frames.

## Additive Protocol Goal: DASH

After the SRT MVP baseline, the app should also let a user create DASH feeds
with the same six simulation modes. DASH output is generated as an MPD plus
media segments, served by the GUI HTTP server, and validated through the same
track/fault reporting interface. DASH captions are represented as a WebVTT
subtitle adaptation.

## Additive Multi-Feed Goal

The GUI should manage multiple feed instances for every supported protocol. A
user should be able to create, list, open/read, update, delete, start, stop, and
validate independent SRT or DASH streams from clear navigation. Each stream owns
its endpoint, subprocess, logs, validation output, and selected simulation mode.
The GUI should start with zero configured/running feeds; feed records are created
only through the create-feed workflow.

## Global Milestone Gate

A milestone may close only when:

- All P0 tests are implemented.
- All P0 tests pass.
- Weighted test coverage is above the required threshold.
- Human-visible output value is demonstrable.
- Acceptance criteria are documented.
- Known gaps are documented.
- The app remains runnable.

---

# Milestone 0: Repo and product contract

## Human-visible value

A developer or stakeholder can inspect the repo and understand exactly what will be built, how it will be tested, and what counts as done.

## Deliverables

- Repo structure.
- README.md.
- AGENTS.md.
- CODEX_GOALS.md.
- TEST_PLAN.md.
- ACCEPTANCE_MATRIX.md.
- TEST_GAPS.md.
- KNOWN_LIMITATIONS.md.

## Acceptance criteria

- Critical MVP requirements are listed.
- Nice-to-haves are separated.
- Outage truth table exists.
- Testing policy exists.
- Milestone gates exist.

## Required tests

Documentation consistency check:

- Every critical MVP requirement maps to at least one planned acceptance test.
- Every required feed mode has expected video/audio/caption behavior.

## Gate

- 100% of critical requirements mapped to tests.

---

# Milestone 1: CLI SRT video feed

## Human-visible value

A user can run one command and generate a consumable SRT video feed.

## Deliverables

- CLI command to start an SRT listener feed.
- Synthetic video generation.
- Start/stop behavior.
- Basic logs.
- Receiver command in docs.

## Acceptance criteria

- CLI starts feed on a configured port.
- External receiver can connect and display video.
- Feed stops cleanly.
- Feed restarts on the same port.
- Invalid port errors are clear.

## Required tests

P0:

- Start SRT video feed.
- Receiver detects video.
- Stop feed.
- Restart feed.

P1:

- Invalid port handling.
- Log output includes start/stop/failure events.

## Gate

- 100% P0 passing.
- 85% weighted coverage.

---

# Milestone 2: Add audio

## Human-visible value

A user can generate a realistic audio/video test feed.

## Deliverables

- Synthetic audio source.
- Audio/video SRT feed.
- Probe/validation detection for video and audio.

## Acceptance criteria

- Receiver sees video.
- Receiver hears or detects audio.
- Probe detects video stream.
- Probe detects audio stream.
- Feed runs for at least 30 minutes.

## Required tests

P0:

- Normal feed contains video.
- Normal feed contains audio.
- Receiver consumes audio/video feed.

P1:

- Audio continuity for 30 minutes.
- Audio config error handling.

## Gate

- 100% P0 passing.
- 85% weighted coverage.

---

# Milestone 3: Add closed captions

## Human-visible value

A user can test caption workflows, caption monitoring, and caption receiver behavior.

## Deliverables

- Generated closed-caption text.
- Caption insertion into normal feed.
- Caption validation.

## Acceptance criteria

- Normal feed contains video.
- Normal feed contains audio.
- Normal feed contains closed captions.
- Captions are generated continuously.
- Captions are not merely burned-in text.
- At least one validation method detects captions.

## Required tests

P0:

- Captions present in normal feed.
- Captions can be detected.
- Video/audio still present when captions are enabled.

P1:

- Caption text updates over time.
- Caption disabled negative test.

## Gate

- 100% P0 passing.
- 85% weighted coverage.

---

# Milestone 4: Feed profiles

## Human-visible value

A user can save and reproduce feed configurations.

## Deliverables

- Feed profile schema.
- Profile loader.
- Sample normal profile.
- Schema validation.

## Acceptance criteria

- Valid profile runs.
- Invalid profile fails with clear error.
- Profile output matches actual generated stream.
- Same profile produces repeatable output.

## Required tests

P0:

- Valid normal profile runs.
- Invalid profile fails safely.
- Profile maps to expected video/audio/caption state.

P1:

- Missing fields reported clearly.
- Schema version checked.

## Gate

- 100% P0 passing.
- 85% weighted coverage.

---

# Milestone 5: Static outage profiles

## Human-visible value

A user can run all required MVP feed scenarios before the GUI is complete.

## Deliverables

Sample profiles:

- srt-normal.yaml
- srt-audio-only.yaml
- srt-video-only.yaml
- srt-no-captions.yaml
- srt-black-video.yaml
- srt-frozen-video.yaml

## Acceptance criteria

| Profile | Video | Audio | Captions | Special validation |
|---|---|---|---|---|
| srt-normal.yaml | yes | yes | yes | moving video |
| srt-audio-only.yaml | no | yes | no/unsupported | video absent |
| srt-video-only.yaml | yes | no | yes | audio absent |
| srt-no-captions.yaml | yes | yes | no | captions absent |
| srt-black-video.yaml | yes | yes | yes | black frames |
| srt-frozen-video.yaml | yes | yes | yes | repeated frames |

## Required tests

P0:

- Normal profile validates.
- Audio-only profile validates.
- Video-only profile validates.
- No-caption profile validates.
- Black-video profile validates.
- Frozen-video profile validates.

P1:

- Unknown outage mode fails clearly.
- Receiver does not crash on any outage scenario.

## Gate

- 100% P0 passing.
- 95% weighted coverage.

---

# Milestone 6: Automated validation tool

## Human-visible value

A user can prove what the simulator is actually outputting.

## Deliverables

- Validation command.
- Machine-readable validation output.
- Human-readable validation output.
- Tests for normal and outage scenarios.

## Acceptance criteria

The validator can detect:

- Feed reachable.
- Video present.
- Video absent.
- Audio present.
- Audio absent.
- Captions present.
- Captions absent.
- Black video.
- Frozen video.
- Unreachable feed.

## Required tests

P0:

- Validate normal feed.
- Validate audio-only feed.
- Validate video-only feed.
- Validate no-caption feed.
- Validate black-video feed.
- Validate frozen-video feed.
- Validate stopped/unreachable feed.

P1:

- False-positive check.
- False-negative check.
- Clear validation failure messages.

## Gate

- 100% P0 passing.
- 90% weighted coverage.
- No false positives on critical stream state.

---

# Milestone 7: Minimal Linux GUI

## Human-visible value

A non-developer can start and stop a normal feed from a GUI.

## Deliverables

- GUI app shell.
- Start normal feed.
- Stop normal feed.
- Display endpoint URL.
- Display feed state.
- Display basic logs.

## Acceptance criteria

- GUI launches on Linux.
- User can start a normal SRT feed.
- Receiver can consume GUI-started feed.
- User can stop feed.
- GUI shows running/stopped/failed state.
- GUI shows copyable SRT URL.

## Required tests

P0:

- GUI launches.
- GUI starts normal feed.
- Receiver consumes GUI-started feed.
- GUI stops feed.

P1:

- Logs shown in GUI.
- Backend start failure shown clearly.

## Gate

- 100% P0 passing.
- 85% weighted coverage.

---

# Milestone 8: GUI outage mode selection

## Human-visible value

A non-developer can launch all required outage scenarios from the GUI.

## Deliverables

GUI mode selector with:

- Normal.
- Audio only.
- Video only.
- No closed captions.
- Black video.
- Frozen video.

## Acceptance criteria

- GUI can start each mode.
- Actual stream state matches selected GUI mode.
- Validation confirms selected mode.
- Unsupported combinations are blocked or clearly explained.

## Required tests

P0:

- GUI starts normal mode.
- GUI starts audio-only mode.
- GUI starts video-only mode.
- GUI starts no-caption mode.
- GUI starts black-video mode.
- GUI starts frozen-video mode.
- GUI state matches validation output.

P1:

- Mode labels are understandable.
- Invalid combinations are blocked.

## Gate

- 100% P0 passing.
- 95% weighted coverage.

---

# Milestone 9: Runtime fault controls

## Human-visible value

A user can interactively simulate incidents while a receiver is connected.

## Deliverables

GUI controls for:

- Video enabled/disabled.
- Audio enabled/disabled.
- Captions enabled/disabled.
- Black video enabled/disabled.
- Frozen video enabled/disabled.

Controlled stream restart is acceptable for MVP if clearly shown.

## Acceptance criteria

- Toggling video off creates audio-only output.
- Toggling audio off creates video-only output.
- Toggling captions off removes captions.
- Toggling black video creates black-frame output.
- Toggling frozen video creates repeated-frame output.
- GUI state matches validated stream state.
- Toggle failures recover to a known safe state.

## Required tests

P0:

- Toggle video off/on.
- Toggle audio off/on.
- Toggle captions off/on.
- Toggle black video off/on.
- Toggle frozen video off/on.
- GUI state equals validation result.

P1:

- Toggle event logging.
- Contradictory states prevented.

## Gate

- 100% P0 passing.
- 95% weighted coverage.

---

# Milestone 10: Observability and troubleshooting

## Human-visible value

A user can understand what is running, what failed, and how to collect diagnostics.

## Deliverables

- Per-feed logs.
- Last error display.
- Feed status.
- Validation result display.
- Diagnostic export.
- Clear distinction between intentional outage and app failure.

## Acceptance criteria

- Port conflict is clear.
- Missing dependency is clear.
- Pipeline failure is visible.
- Intentional outage is not shown as app crash.
- Logs can be exported.

## Required tests

P0:

- Failed feed shows error.
- Intentional outage shown as intentional.
- Logs visible.
- Logs exportable.

P1:

- Validation output visible in GUI.
- Error messages actionable.

## Gate

- 100% P0 passing.
- 85% weighted coverage.

---

# Milestone 11: Receiver compatibility

## Human-visible value

A stakeholder can trust that feeds work with real receiver tools.

## Deliverables

Compatibility report for:

- ffplay.
- ffprobe.
- GStreamer receiver.
- VLC if feasible.
- Any project-specific target receiver if available.

## Acceptance criteria

Required receivers correctly handle:

- Normal.
- Audio only.
- Video only.
- No captions.
- Black video.
- Frozen video.

## Required tests

P0:

- ffplay consumes required modes where applicable.
- ffprobe detects required tracks/states where applicable.
- GStreamer receiver consumes required modes.
- Required target receivers pass if defined.

P1:

- VLC smoke test.
- Known limitations documented.

## Gate

- 100% required receiver tests passing.
- 95% weighted coverage.
- All receiver-specific limitations documented.

---

# Milestone 12: Soak and stability

## Human-visible value

A user can rely on the simulator for real test sessions, not just quick demos.

## Deliverables

- Soak test harness.
- Stability report.
- Resource usage notes.

## Acceptance criteria

- Normal feed runs for 24 hours.
- Required outage modes run for agreed soak duration.
- No unexplained crashes.
- Memory growth remains under threshold.
- Repeated restart succeeds.

## Required tests

P0:

- Normal feed 24-hour soak.
- Required outage soak tests.
- Repeated start/stop test.
- GUI remains responsive during long run.

P1:

- Resource usage report.
- Validation every 15 minutes during soak.

## Gate

- 100% P0 passing.
- 95% weighted coverage.

Suggested thresholds:

- 0 unexplained crashes.
- 99% restart success.
- Less than 200 MB memory growth over 24 hours unless justified.
- Feed validates every 15 minutes.

---

# Milestone 13: Internal alpha

## Human-visible value

Internal users can install and use the app without developer help.

## Deliverables

- Installable Linux package or clear run instructions.
- Quick-start guide.
- Known limitations.
- Feedback template.

## Acceptance criteria

- Clean install works.
- GUI launches.
- Normal feed works.
- All critical fault modes work.
- Validation works.
- Logs can be collected.
- Docs are usable.

## Required tests

P0:

- Clean install.
- GUI launch.
- Normal feed.
- All five fault modes.
- Validation.
- Log collection.

P1:

- Uninstall.
- Docs walkthrough.

## Gate

- 100% P0 passing.
- 90% weighted coverage.

---

# Milestone 14: External beta

## Human-visible value

Real users can validate whether the product solves their feed simulation needs.

## Deliverables

- Beta package.
- Release notes.
- Beta guide.
- Known limitations.
- Diagnostic collection path.

## Acceptance criteria

- External user can install.
- External user can generate normal feed.
- External user can simulate all MVP outage/fault modes.
- External user can validate feed state.
- External user can send useful diagnostics.

## Required tests

P0:

- External install.
- First normal feed.
- All fault modes.
- Validation.
- Diagnostic export.

P1:

- Documentation usability.
- Receiver compatibility in beta environment.

## Gate

- 100% P0 passing.
- 95% weighted coverage.
- No open blocker or critical bug.

---

# Milestone 15: Version 1.0

## Human-visible value

A production-ready SRT-focused Video Feed Simulator is available for real use.

## Deliverables

- Final Linux build.
- GUI.
- SRT feed generation.
- Normal feed.
- Audio-only mode.
- Video-only mode.
- No-caption mode.
- Black-video mode.
- Frozen-video mode.
- Validation tool.
- Logs and diagnostics.
- User documentation.
- Test evidence.

## Acceptance criteria

- All critical MVP requirements pass.
- Required receivers pass.
- Soak testing passes.
- GUI state matches actual stream state.
- Validation agrees with actual stream state.
- Documentation is complete enough for independent use.

## Required tests

P0:

- Full critical regression suite.
- Clean install.
- Normal feed.
- Audio-only feed.
- Video-only feed.
- No-caption feed.
- Black-video feed.
- Frozen-video feed.
- GUI state accuracy.
- Validation accuracy.
- Receiver compatibility.
- Soak test.

## Gate

- 100% P0 passing.
- 98% weighted coverage.
- 0 blocker bugs.
- 0 critical bugs.
- No undocumented critical limitations.
