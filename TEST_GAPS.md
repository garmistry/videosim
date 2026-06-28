# TEST_GAPS.md

# Test Gaps

## Current Gate

Milestones 0 and 1 have no known P0 test gaps. Milestone 1 live SRT receiver and
restart proof runs in Docker with `docker compose run --build --rm live-srt`.

## Deferred Until Later Milestones

The following gaps are expected because the application implementation has not
started yet:

| Gap | Priority | Planned milestone |
|---|---|---|
| Audio stream detection tests. | P0 | Milestone 2 |
| Caption generation and detection tests. | P0 | Milestone 3 |
| Profile validation tests. | P0 | Milestone 4 |
| Static outage profile tests for all required modes. | P0 | Milestone 5 |
| Automated validator tests for track absence, black video, frozen video, and stopped feed. | P0 | Milestone 6 |
| GUI launch/start/stop tests. | P0 | Milestone 7 |
| GUI outage selection tests. | P0 | Milestone 8 |
| Runtime fault toggle tests. | P0 | Milestone 9 |
| Log/error/diagnostic export tests. | P0 | Milestone 10 |
| Receiver compatibility tests. | P0 | Milestone 11 |
| Soak and restart stability tests. | P0 | Milestone 12 |

These are not skipped for their owning milestones. They are blocked by missing
implementation and must pass before those milestones advance.
