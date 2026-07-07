# TEST_GAPS.md

# Test Gaps

## Current Gate

Milestones 0 through 11 have no known P0 test gaps. Milestone 1 through 11 live SRT
receiver proof runs in Docker with `docker compose run --build --rm live-srt`.

## Deferred Until Later Milestones

The following gaps are expected because their owning milestone is not complete
or the gap is non-critical for a completed milestone:

| Gap | Priority | Planned milestone |
|---|---|---|
| Audio continuity for 30 minutes. | P1 | Milestone 2 |
| Caption text update-over-time verification. | P1 | Milestone 3 |
| Normal feed 24-hour soak run. | P0 | Milestone 12 |
| Required outage mode long soak runs. | P0 | Milestone 12 |
| GUI responsiveness during long soak. | P0 | Milestone 12 |
| 24-hour resource threshold evidence. | P1 | Milestone 12 |

These are not skipped for their owning milestones. They are blocked by missing
implementation and must pass before those milestones advance.

## Monitoring

- TR 101 290 priority 1/2 indicators and parser-backed priority 3 PSI/SI plus
  unreferenced-PID and T-STD timing indicators have unit coverage, and malformed
  DASH fixture sampling is covered through the monitor alarm path for every
  indicator. A Docker `monitor-fixtures` gate is defined for that fixture suite.
  Malformed live SRT/DASH fixture streams are not yet generated for end-to-end
  Docker proof of every indicator.
- Audio loudness alarms for ITU-R BS.1770 measurement availability, EBU R 128,
  and ATSC A/85 have unit coverage. Full-program loudness compliance runs are
  not part of the live monitor slice; the monitor samples live audio for alarm
  detection.
