# STABILITY_REPORT.md

# Soak And Stability Report

Milestone 12 is in progress. The repo now includes a timed soak harness:

```sh
python3 -m videosim soak --profile profiles/srt-normal.yaml --port 9000 --duration-seconds 86400 --validation-interval-seconds 900 --json
```

For fast Docker proof, `tests.test_live_srt.LiveSrtTest.test_short_soak_harness_validates_normal_feed`
runs the same harness with an 8-second duration.

## Current Evidence

| Check | Status | Evidence |
|---|---|---|
| Short normal soak harness | pass | `docker compose run --build --rm live-srt` |
| Periodic validation support | pass | `videosim soak --validation-interval-seconds ...` |
| Repeated start/stop | pass | Existing Docker live restart tests |
| RSS memory sampling | implemented | `videosim soak --json` reports start/end/growth MB when available |

## Pending Gate Evidence

| Required M12 item | Status |
|---|---|
| Normal feed 24-hour soak | pending |
| Required outage mode soak tests | pending |
| GUI remains responsive during long run | pending |
| Resource usage over 24 hours under threshold | pending |
| Validation every 15 minutes during 24-hour soak | pending |

## Resource Notes

- Linux RSS is read from `/proc/<pid>/status`.
- macOS and other systems fall back to `ps -o rss= -p <pid>`.
- Memory growth is reported as `memory_end_mb - memory_start_mb`.
- The suggested threshold remains less than 200 MB growth over 24 hours unless
  a future soak run justifies a different limit.
