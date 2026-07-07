# MONITORING.md

# Feed Monitoring

The monitor is a separate application process that polls the GUI feed inventory,
ingests running feeds with the existing validator, writes alarm state to JSON,
and lets the GUI display active alarms plus event audit history.

ETSI TR 101 290 V1.4.1 is the source for the MPEG-2 TS monitor catalogue:
https://www.etsi.org/deliver/etsi_tr/101200_101299/101290/01.04.01_60/tr_101290v010401p.pdf

## Run

Docker Compose starts the GUI and monitor as separate services:

```sh
docker compose up --build app monitor
```

Local one-shot smoke:

```sh
python3 -m videosim monitor --gui-state-url http://127.0.0.1:8080/state.json --state-path /tmp/videosim-monitor/state.json --once
```

Docker uses `--srt-host app` so the monitor container validates SRT feeds at
`srt://app:<port>?mode=caller` instead of `127.0.0.1`.

## Alarm Behavior

- Poll interval: 5 seconds by default.
- Repeat interval: 5 seconds by default.
- First failing poll emits `alarm_raised`.
- Continued failure emits `alarm_active` every repeat interval.
- Recovery emits `alarm_cleared`, marks the alarm `steady`, and resets active state.
- Event history is retained in the shared monitor JSON file.

The GUI reads `VIDEOSIM_MONITOR_STATE` or `/tmp/videosim-monitor/state.json`
and shows active alarms plus stream-specific event audit history.

## Implemented Monitors

| Monitor | Severity | Source | Status |
|---|---|---|---|
| Feed reachable | critical | Platform validator | implemented |
| Video present | critical | Platform validator | implemented |
| Audio present | critical | Platform validator | implemented |
| Captions present | major | Platform validator | implemented |
| Black video detected | major | Platform validator | implemented |
| Frozen video detected | major | Platform validator | implemented |

## TR 101 290 Catalogue

The monitor state includes these TR 101 290 indicators so UI/API consumers can
see the intended alert taxonomy before each parser is implemented.

| Indicator | Priority | Status |
|---|---|---|
| TS_sync_loss | 1 | catalogued |
| Sync_byte_error | 1 | catalogued |
| PAT_error | 1 | catalogued |
| PAT_error_2 | 1 | catalogued |
| Continuity_count_error | 1 | catalogued |
| PMT_error | 1 | catalogued |
| PMT_error_2 | 1 | catalogued |
| PID_error | 1 | catalogued |
| Transport_error | 2 | catalogued |
| CRC_error | 2 | catalogued |
| PCR_error | 2 | catalogued |
| PCR_repetition_error | 2 | catalogued |
| PCR_discontinuity_indicator_error | 2 | catalogued |
| PCR_accuracy_error | 2 | catalogued |
| PTS_error | 2 | catalogued |
| CAT_error | 2 | catalogued |

## Extending

Add a `MonitorSpec` in `videosim.monitor.MONITOR_SPECS`, emit a
`MonitorIssue` from `issues_for_report` or a new probe, then add one focused
test in `tests/test_monitor.py`.
