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

## TR 101 290 Monitors

The monitor samples MPEG-2 TS bytes from SRT feeds, or DASH `.ts` segments from
the shared DASH volume, and parses the following TR 101 290 indicators.
Priority 3 is application-dependent; the current parser covers PSI/SI syntax,
observed SI repetition/presence windows, EIT P/F pairing, and unreferenced PIDs.
The T-STD buffer-model checks are catalogued but not implemented.

| Indicator | Priority | Status |
|---|---|---|
| TS_sync_loss | 1 | parser-backed |
| Sync_byte_error | 1 | parser-backed |
| PAT_error | 1 | parser-backed |
| PAT_error_2 | 1 | parser-backed |
| Continuity_count_error | 1 | parser-backed |
| PMT_error | 1 | parser-backed |
| PMT_error_2 | 1 | parser-backed |
| PID_error | 1 | parser-backed |
| Transport_error | 2 | parser-backed |
| CRC_error | 2 | parser-backed |
| PCR_error | 2 | parser-backed |
| PCR_repetition_error | 2 | parser-backed |
| PCR_discontinuity_indicator_error | 2 | parser-backed |
| PCR_accuracy_error | 2 | parser-backed from sample packet rate |
| PTS_error | 2 | parser-backed |
| CAT_error | 2 | parser-backed |
| NIT_error | 3 | parser-backed for table-id, observed presence, and repetition |
| NIT_actual_error | 3 | parser-backed for table-id, observed presence, and repetition |
| NIT_other_error | 3 | parser-backed when NIT_other is present |
| SI_repetition_error | 3 | parser-backed for observed SI timing windows |
| Buffer_error | 3 | catalogued; T-STD model pending |
| Unreferenced_PID | 3 | parser-backed |
| Unreferenced_PID 2 | 3 | parser-backed |
| SDT_error | 3 | parser-backed for table-id, observed presence, and repetition |
| SDT_actual_error | 3 | parser-backed for table-id, observed presence, and repetition |
| SDT_other_error | 3 | parser-backed when SDT_other is present |
| EIT_error | 3 | parser-backed for table-id, observed presence, and repetition |
| EIT_actual_error | 3 | parser-backed for table-id, observed presence, and repetition |
| EIT_other_error | 3 | parser-backed when EIT_other is present |
| EIT_PF_error | 3 | parser-backed |
| RST_error | 3 | parser-backed for table-id and repetition |
| TDT_error | 3 | parser-backed for table-id, observed presence, and repetition |
| Empty_buffer_error | 3 | catalogued; T-STD model pending |
| Data_delay_error | 3 | catalogued; T-STD model pending |

## Extending

Add a `MonitorSpec` in `videosim.monitor.MONITOR_SPECS`, emit a `MonitorIssue`
from `issues_for_report`, the TR 101 analyzer, or a new probe, then add one
focused test in `tests/test_monitor.py` or `tests/test_tr101.py`.
