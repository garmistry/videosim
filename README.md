# Video Feed Simulator

Video Feed Simulator generates local SRT and DASH live video feeds for testing receivers,
monitoring systems, and outage handling. The MVP target is a GUI that can start,
stop, and validate normal and fault-mode SRT feeds.

## Current Status

Milestones 0 through 11 are complete. The CLI and React-enhanced local browser
GUI can create, list, open, update, delete, start, stop, restart, and validate
multiple synthetic SRT or DASH feeds through GStreamer. DASH feed generation is
available for the same six simulation modes, with MPD/TS output and WebVTT
captions served by the GUI.

## MVP Scope

The critical MVP must support:

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

Still out of scope until the critical MVP is done: packet/jitter simulation,
REST API, Prometheus metrics, and release packaging such as AppImage/Flatpak.
Docker is present as a Linux test harness, not a release package.

## Architecture

The planned layers are:

1. GUI layer.
2. Feed orchestration layer.
3. Feed profile/config layer.
4. Media pipeline layer.
5. Protocol output layer.
6. Validation layer.
7. Observability/logging layer.

The current media stack choice is GStreamer, with FFmpeg/ffprobe used for
receiver compatibility and validation where useful. GStreamer is favored because
it exposes SRT, MPEG-TS muxing, test sources, and caption insertion elements as
pipeline pieces that map cleanly to the required modes.

## Setup

Install system dependencies:

```sh
scripts/install-deps.sh
```

The installer supports common Linux package managers and Homebrew on macOS.

## Verification

Run the current checks:

```sh
python3 -m unittest tests.test_docs_contract tests.test_cli_video_feed tests.test_live_srt
```

The live SRT test is skipped locally unless `VIDEOSIM_LIVE_SRT=1` is set.

Run the Linux/Docker gates:

```sh
docker compose run --build --rm test
docker compose run --build --rm live-srt
```

## Run Current CLI

Start a synthetic audio/video SRT listener feed:

```sh
python3 -m videosim start --port 9000
```

Use `--audio-frequency 1000` to change the generated tone, `--no-audio` for the
earlier video-only feed, or `--no-captions` to disable caption insertion.

Start from the sample normal profile:

```sh
python3 -m videosim start --profile profiles/srt-normal.yaml
```

Start a DASH feed with the same normal/fault profile modes:

```sh
python3 -m videosim start --profile profiles/dash-normal.yaml --dash-dir /tmp/videosim-dash
```

Validate a running feed against a profile:

```sh
python3 -m videosim validate --profile profiles/srt-normal.yaml --port 9000
python3 -m videosim validate --profile profiles/srt-normal.yaml --port 9000 --json
python3 -m videosim validate --profile profiles/dash-normal.yaml --dash-dir /tmp/videosim-dash --json
```

Run a timed soak with periodic validation:

```sh
python3 -m videosim soak --profile profiles/srt-normal.yaml --port 9000 --duration-seconds 86400 --validation-interval-seconds 900 --json
```

Static outage profile files also exist for `audio_only`, `video_only`,
`no_captions`, `black_video`, and `frozen_video`; Docker live validation covers
all six profiles.

Launch the local browser GUI:

```sh
python3 -m videosim gui --http-port 8080 --feed-port 9000
```

Then open `http://127.0.0.1:8080`.

The GUI starts with zero configured feeds. Use the create-feed form as the entry
point, then manage feeds from the stream list:

- Create a named SRT or DASH feed.
- List streams in the left navigation.
- Read/open a stream at `/feeds/<stream-id>` to see endpoint, status, validation, and logs.
- Update selected stream name, protocol, or mode.
- Delete the selected stream.

Each feed detail page can be bookmarked directly and does not show the create
feed form; return to `/` for the create/list workflow. Each feed can be started,
stopped, validated, and copied independently. The protocol and mode selectors
support SRT or DASH for normal, audio-only, video-only, no-captions,
black-video, and frozen-video feeds. Changing fault controls while a feed is
running uses a controlled stream restart.

The GUI also shows status, intentional outage state, per-feed estimated bit
rate, outbound total, uptime, generated video frame count, last error, logs,
validation output, a copyable endpoint, and a downloadable diagnostics text
file. Video-present modes include a visible running clock overlay for receiver
testing. When a video-capable feed is running, the React GUI opens a preview
panel that refreshes a local frame matching the active mode. Use Validate to
prove actual stream state.

Deploy the GUI and SRT listener together with Docker Compose:

```sh
docker compose up --build app
```

Open `http://127.0.0.1:8080`. The GUI and feed subprocesses run inside the
`videosim-app-1` container. The default Compose file publishes UDP 9000-9010 for
multiple SRT streams and TCP 8080 for the GUI/DASH server. Verbose container
logs show feed mode, profile, endpoint, subprocess PID, and the GStreamer
pipeline:

```sh
docker compose logs -f app
```

Receiver URL:

```text
srt://127.0.0.1:9000?mode=caller
```

DASH feeds are served by the GUI at:

```text
http://127.0.0.1:8080/dash/<stream-id>/manifest.mpd
```

The SRT listener accepts receiver clients at that caller URL. This Docker image
does not pass a `maxconn` URI option to GStreamer's `srtsink`; inspection showed
that option is not a supported property in the packaged plugin and it can crash
the listener.

Stop the feed with Ctrl-C.

## Documentation

- [CODEX_GOALS.md](CODEX_GOALS.md) - milestone plan.
- [TEST_PLAN.md](TEST_PLAN.md) - weighted test plan and requirement mapping.
- [ACCEPTANCE_MATRIX.md](ACCEPTANCE_MATRIX.md) - acceptance criteria by mode and milestone.
- [TEST_GAPS.md](TEST_GAPS.md) - missing tests and allowed gaps.
- [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) - current limitations.
- [COMPATIBILITY_REPORT.md](COMPATIBILITY_REPORT.md) - receiver compatibility evidence.
- [STABILITY_REPORT.md](STABILITY_REPORT.md) - soak harness and pending long-run evidence.
- [RUNBOOK.md](RUNBOOK.md) - install, run, verify, and troubleshoot steps.
