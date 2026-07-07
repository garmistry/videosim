# KNOWN_LIMITATIONS.md

# Known Limitations

- The CLI can start, stop, and restart audio/video/caption SRT listener feeds
  and DASH MPD/segment feeds.
- Runtime fault controls use a controlled stream restart; seamless in-place
  toggles are intentionally out of MVP scope.
- Profile parsing supports only the flat YAML shape used by the current normal
  and outage profiles.
- The Milestone 2 30-minute audio continuity P1 test is not implemented.
- Captions start after a fixed 3-second delay so live SRT video caps negotiate
  before caption bytes reach `cccombiner`.
- The Milestone 3 caption update-over-time P1 test is not implemented.
- The Dockerfile is a Linux test harness, not release packaging.
- Docker packaging is preferred for portability but is not part of the critical
  MVP gate in AGENTS.md; revisit after core feed generation and validation work.
- macOS is treated as a development target. Linux remains the required runtime
  target.
- Audio-only caption behavior is explicitly allowed to be absent/unsupported and
  must be reported clearly by validation.
- SRT captions are embedded CEA-608 in H.264. DASH captions are currently
  emitted as a WebVTT subtitle adaptation in the MPD, not embedded CEA-608.
- Docker Compose publishes UDP 9000-9010 for GUI-managed SRT streams by default.
  Additional SRT streams need more host UDP ports published.
- ffprobe and ffplay compatibility tests are receiver smoke checks; semantic
  caption, black-video, and frozen-video proof remains in `videosim validate`.
- VLC is not tested in the minimal headless Docker harness.
- No project-specific target receiver has been defined.
- The soak harness is implemented, but 24-hour normal/outage soak evidence is
  still pending.
- The GUI preview is a local refreshed frame matching the active mode, not
  native browser SRT playback and not validation proof of the SRT output.
  Audio-only mode and external feeds have no video preview.
- GUI feed metrics are estimates from configured media tracks and elapsed run
  time for generated feeds. They are not actual SRT socket byte counters,
  external-feed ingress counters, or per-receiver telemetry.
- SQLite feed registration persists feed definitions and alert profiles only.
  Local feed subprocesses are intentionally not restored as running processes
  after a GUI restart.
- External DASH validation supports reachable MPDs with common `SegmentURL` or
  `SegmentTemplate` media references. Unusual DASH packaging may need a new
  resolver in the validation layer.

## Monitoring

- The separate monitor app uses a shared JSON state file rather than a database
  or event broker.
- Feed definitions and alert profiles are persisted by the GUI, but monitor
  alarm/event history still lives in the shared JSON monitor state file.
- TR 101 290 PCR accuracy is estimated from the sampled packet rate. It is good
  for simulator regression alarms, not a replacement for calibrated lab
  measurement equipment.
- TR 101 290 priority 3 T-STD buffer, empty-buffer, and data-delay checks are
  parser-backed timing approximations from sample byte rate and PES PTS, not a
  calibrated ISO decoder buffer model.
- Frame-rate alarms use FFprobe-reported stream rates from the live endpoint or
  latest DASH video segment. They detect configured-rate mismatches, not
  long-term cadence jitter.
- Audio loudness alarms use short live samples through FFmpeg `ebur128`.
  They are useful for operational alarms but are not full-program EBU R 128 or
  ATSC A/85 compliance certificates.
