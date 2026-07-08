# Feed Monitoring

The monitor is a separate application process that polls the GUI feed inventory,
ingests running feeds with the existing validator, writes alarm state to JSON,
and lets the GUI display active alarms plus event audit history.

ETSI TR 101 290 V1.4.1 is the source for the MPEG-2 TS monitor catalogue:
https://www.etsi.org/deliver/etsi_tr/101200_101299/101290/01.04.01_60/tr_101290v010401p.pdf

Audio loudness monitoring uses FFmpeg's `ebur128` filter as the local
ITU-R BS.1770-compatible meter. EBU R 128 is the source for the -23 LUFS
target and -1 dBTP true-peak ceiling:
https://tech.ebu.ch/publications/r128/

ITU-R BS.1770-5 defines the K-weighted gated loudness algorithm used for LKFS
and true-peak measurement:
https://www.itu.int/dms_pubrec/itu-r/rec/bs/R-REC-BS.1770-5-202311-I!!PDF-E.pdf

The U.S. loudness monitor uses ATSC A/85 practice with a -24 LKFS target. The
implementation treats the request's "ARSC A/8" as ATSC A/85:
https://www.atsc.org/atsc-documents/a85-techniques-for-establishing-and-maintaining-audio-loudness-for-digital-television/

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
- First failing poll emits `alarm_raised` after the stream's alert-profile
  delay has elapsed.
- Continued failure emits `alarm_active` every repeat interval.
- Recovery emits `alarm_cleared`, marks the alarm `steady`, and resets active state.
- Event history is retained in the shared monitor JSON file.

The GUI reads `VIDEOSIM_MONITOR_STATE` or `/tmp/videosim-monitor/state.json`
and shows active alarms plus stream-specific event audit history.

## Alert Profiles

Each stream payload includes an `alertProfile` with `enabledMonitorIds` and
`delaySeconds`. The stream detail page always renders the built-in monitor
catalogue, so alert profiles can be created before the monitor service has
written state. Users can save a selected set, enable all alerts, or disable all
alerts for the selected stream. Disabled monitor IDs do not raise alarms for
that stream and clear any existing active alarm on the next poll. Enabled issues
are stored as pending until they persist for the configured delay, then they
raise normally.

## Implemented Monitors

| Monitor | Severity | Source | Status |
|---|---|---|---|
| Feed reachable | critical | Platform validator | implemented |
| Video present | critical | Platform validator | implemented |
| Audio present | critical | Platform validator | implemented |
| Captions present | major | Platform validator | implemented |
| Black video detected | major | Platform validator | implemented |
| Frozen video detected | major | Platform validator | implemented |
| Video frame rate match | major | FFprobe | implemented |
| ITU-R BS.1770 loudness measurement | major | FFmpeg ebur128 | implemented |
| EBU R 128 integrated loudness | major | FFmpeg ebur128 | implemented |
| EBU R 128 true peak | major | FFmpeg ebur128 | implemented |
| ATSC A/85 integrated loudness | major | FFmpeg ebur128 | implemented |

## Audio Loudness Monitors

Audio-present feeds are sampled through FFmpeg and measured with the `ebur128`
filter. For SRT, the monitor reads a short live sample from the SRT endpoint.
For DASH, it measures the latest audio `.ts` segment in the shared DASH volume.

| Monitor | Alarm condition |
|---|---|
| ITU-R BS.1770 loudness measurement | FFmpeg cannot produce finite integrated loudness and true-peak values. |
| EBU R 128 integrated loudness | Measured integrated loudness is outside -23 LUFS +/- 1 LU. |
| EBU R 128 true peak | Measured true peak is above -1 dBTP. |
| ATSC A/85 integrated loudness | Measured integrated loudness is outside -24 LKFS +/- 2 LU. |

These are live monitor samples, not full-program compliance certificates.
Program-level acceptance should still be measured across the full item when
that matters.

## Frame Rate Monitor

The GUI stores the configured frame rate for each stream. Supported GUI choices
are 23.97, 24, 25, 50, 59.94, and 60 fps; GStreamer receives rational caps for
the fractional rates. The monitor probes video-present feeds with FFprobe and
raises `Video frame rate match` when the measured rate differs from the
configured rate by more than 0.15 fps, or when frame-rate measurement fails.

## TR 101 290 Monitors

The monitor samples MPEG-2 TS bytes from SRT feeds, or up to the latest 15 DASH
`.ts` segments from the shared DASH volume, and parses the following TR 101 290
indicators.
Priority 3 is application-dependent; the current parser covers PSI/SI syntax,
observed SI repetition/presence windows, EIT P/F pairing, unreferenced PIDs, and
T-STD timing checks estimated from sample byte rate and PES PTS.

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
| Buffer_error | 3 | parser-backed T-STD timing approximation |
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
| Empty_buffer_error | 3 | parser-backed T-STD timing approximation |
| Data_delay_error | 3 | parser-backed T-STD timing approximation |

## Extending

Add a `MonitorSpec` in `videosim.monitor_catalog.MONITOR_SPECS`, emit a `MonitorIssue`
from `issues_for_report`, the TR 101 analyzer, or a new probe, then add one
focused test in `tests/test_monitor.py` or `tests/test_tr101.py`.
