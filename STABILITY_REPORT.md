# STABILITY_REPORT.md

# Soak And Stability Report

Milestone 12 is in progress. The repo now includes a timed soak harness:

```sh
python3 -m videosim soak --profile profiles/srt-normal.yaml --port 9000 --duration-seconds 86400 --validation-interval-seconds 900 --json
```

Run the full M12 feed soak script:

```sh
scripts/run-m12-soak.sh
```

Run the same script in Docker:

```sh
docker compose run --build --rm m12-soak
```

For fast Docker proof, the live suite runs the same harness with an 8-second
duration for normal and outage profiles.

## Current Evidence

| Check | Status | Evidence |
|---|---|---|
| Short normal soak harness | pass | `docker compose run --build --rm live-srt` |
| Short outage soak harness | pass | `docker compose run --build --rm live-srt` |
| GUI responsive while feed runs | pass | `docker compose run --build --rm live-srt` |
| Periodic validation support | pass | `videosim soak --validation-interval-seconds ...` |
| One-command full feed soak runner | implemented | `scripts/run-m12-soak.sh`; `docker compose run --build --rm m12-soak` |
| Repeated start/stop | pass | Existing Docker live restart tests |
| RSS memory sampling | implemented | `videosim soak --json` reports start/end/growth MB when available |

## Pending Gate Evidence

| Required M12 item | Status |
|---|---|
| Normal feed 24-hour soak | pending |
| Required outage mode long soak tests | pending |
| GUI remains responsive during long run | pending; short running-feed proof passes |
| Resource usage over 24 hours under threshold | pending |
| Validation every 15 minutes during 24-hour soak | pending |

## Resource Notes

- Linux RSS is read from `/proc/<pid>/status`.
- macOS and other systems fall back to `ps -o rss= -p <pid>`.
- Memory growth is reported as `memory_end_mb - memory_start_mb`.
- The suggested threshold remains less than 200 MB growth over 24 hours unless
  a future soak run justifies a different limit.
- `NORMAL_DURATION_SECONDS`, `OUTAGE_DURATION_SECONDS`, and
  `VALIDATION_INTERVAL_SECONDS` can shorten or lengthen the full soak runner.
