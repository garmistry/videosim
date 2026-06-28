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
| Soak and restart stability tests. | P0 | Milestone 12 |

These are not skipped for their owning milestones. They are blocked by missing
implementation and must pass before those milestones advance.
