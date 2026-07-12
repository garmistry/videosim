# Implementation Notes

This document holds project and implementation detail that should not crowd the
top-level README.

## Current Status

Milestones 0 through 11 are complete. The CLI and React-enhanced browser GUI
can create, list, open, update, delete, start, stop, restart, and validate
multiple synthetic SRT or DASH feeds through GStreamer. DASH generation supports
the same six simulation modes as SRT, with MPD/TS output and WebVTT captions
served by the GUI.

## MVP Scope

The critical MVP supports:

- Linux runtime, with macOS development support where practical.
- Visual GUI.
- Local SRT feed generation with a running clock overlay on video modes.
- Normal feed with video, audio, and closed captions.
- Fault modes: audio only, video only, no captions, black video, frozen video.
- Copyable SRT endpoint URL.
- Start/stop controls.
- Basic logs and clear errors.
- Validation proving actual stream state.
- Install/run documentation and tests for critical behavior.

Packet loss, jitter simulation, Prometheus metrics, and release packaging such
as AppImage or Flatpak remain outside the critical MVP. Docker is used as a
Linux test harness and operator-friendly run path, not as release packaging.

## Architecture

The planned layers are:

1. GUI layer.
2. Feed orchestration layer.
3. Feed profile/config layer.
4. Media pipeline layer.
5. Protocol output layer.
6. Validation layer.
7. Observability/logging layer.

The current media stack is GStreamer, with FFmpeg/ffprobe used for receiver
compatibility and validation where useful. GStreamer exposes SRT, MPEG-TS
muxing, test sources, and caption insertion elements as pipeline pieces that
map cleanly to the required modes.

The distributed refit is documented in
[distributed-architecture.md](distributed-architecture.md). The first slice
keeps the GUI as the master control plane and adds worker nodes that poll
assignments, run existing monitor checks, and report alarms/events back to the
master.

## Feed Registration

Feed registrations are persisted in SQLite by default when the GUI is launched
from the CLI. The database is `$XDG_DATA_HOME/videosim/feeds.sqlite3` or
`~/.local/share/videosim/feeds.sqlite3`; set `VIDEOSIM_DB_PATH` to use another
location.

When `VIDEOSIM_DATABASE_URL` is set, the CLI selects the bounded-pool PostgreSQL
adapter instead. Apply migrations first. The production Compose overlay does
this automatically and an idempotent `import-sqlite-feeds` command supports
cutover. See [durable-control-plane.md](durable-control-plane.md). The GUI stores
feed definitions and alert profiles through the same interface. Local
subprocess runtime state is recreated after restart.

Generated feed forms show protocol, mode, and frame rate. External feed forms
show protocol and URL only. External SRT URLs and external DASH manifest URLs
are ingested as-is, with no configured mode or frame-rate expectation. External
feeds are still validated and monitored; absence alarms are raised only for
alert checks enabled in that feed's alert profile.

## GUI Behavior

The React/Vite GUI follows the `design_docs` model: IBM Plex Sans UI text, IBM
Plex Mono for endpoints/logs/metrics, warm dark/light neutrals, broadcast amber
actions, and dot-plus-label status badges.

The primary GUI view is an active-feed table with status, endpoint, metrics,
actions, and a preview thumbnail for each generated video feed. Create feed
opens a modal. Feed detail pages can be bookmarked directly.

Generated feeds can be started, stopped, validated, and copied independently.
Normal, audio-only, video-only, no-captions, black-video, and frozen-video
generated modes can be selected at 23.97, 24, 25, 50, 59.94, or 60 fps.
Changing generated feed controls while a feed is running uses a controlled
stream restart.

The GUI shows status, intentional outage state, estimated bit rate, outbound
total, uptime, generated video frame count, last error, logs, validation output,
a copyable endpoint, and diagnostics text. Feed detail pages plot bit rate and
outbound data over a rolling five-minute client-side metrics window.

## Monitoring

The monitor app can run as a separate process/container, poll GUI feed state,
validate running feeds, and write alarm/event history for the GUI. Each stream
can enable selected alarms, disable alarms, and delay alarm raising until an
issue persists.

Current alarms cover feed reachability, video absence, audio absence, caption
absence, black-video validation, frozen-video validation, parser-backed TR 101
290 indicators, measured frame-rate mismatches, and audio loudness alarms for
ITU-R BS.1770, EBU R 128, and ATSC A/85.

See [monitoring.md](monitoring.md) for the full monitor catalogue and alarm
behavior.

## Docker Runtime Notes

The default Compose file publishes TCP 8080 for the GUI/DASH server and UDP
9000-9010 for GUI-managed SRT streams.

Generated SRT listener pipelines accept receiver clients at
`srt://127.0.0.1:9000?mode=caller` and keep running when receivers disconnect.
Do not add a `maxconn` URI option to the GStreamer `srtsink` command in the
Docker image; the packaged plugin does not expose that as a supported property
and it can crash the listener.

Generated DASH feeds are served by the GUI at:

```text
http://127.0.0.1:8080/dash/<stream-id>/manifest.mpd
```
