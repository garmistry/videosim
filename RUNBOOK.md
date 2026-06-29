# RUNBOOK.md

# Runbook

## Install Dependencies

```sh
scripts/install-deps.sh
```

Supported paths:

- Linux: apt, dnf, or pacman based distributions.
- macOS: Homebrew.

The script installs Python/Tk, FFmpeg/ffprobe, and GStreamer components needed
for the planned GUI, SRT output, MPEG-TS muxing, H.264 video, and captions.

## Verify Current Repository Contract

```sh
python3 -m unittest tests.test_docs_contract tests.test_cli_video_feed tests.test_live_srt
```

Expected result: all current contract and CLI checks pass; the live SRT test is
skipped unless `VIDEOSIM_LIVE_SRT=1` is set.

Run the Linux/Docker gates:

```sh
docker compose run --build --rm test
docker compose run --build --rm live-srt
```

## Run The Current CLI

Start an audio/video SRT listener feed:

```sh
python3 -m videosim start --port 9000
```

Change the generated audio tone:

```sh
python3 -m videosim start --port 9000 --audio-frequency 1000
```

Disable audio for the earlier video-only path:

```sh
python3 -m videosim start --port 9000 --no-audio
```

Disable generated CEA-608 captions:

```sh
python3 -m videosim start --port 9000 --no-captions
```

Start from the sample normal profile:

```sh
python3 -m videosim start --profile profiles/srt-normal.yaml
```

Available static profiles:

```text
profiles/srt-normal.yaml
profiles/srt-audio-only.yaml
profiles/srt-video-only.yaml
profiles/srt-no-captions.yaml
profiles/srt-black-video.yaml
profiles/srt-frozen-video.yaml
```

## Validate A Running Feed

```sh
python3 -m videosim validate --profile profiles/srt-normal.yaml --port 9000
python3 -m videosim validate --profile profiles/srt-normal.yaml --port 9000 --json
```

## Run A Soak

Short smoke:

```sh
python3 -m videosim soak --profile profiles/srt-normal.yaml --port 9000 --duration-seconds 60 --validation-interval-seconds 15 --json
```

Full M12 normal-feed run:

```sh
python3 -m videosim soak --profile profiles/srt-normal.yaml --port 9000 --duration-seconds 86400 --validation-interval-seconds 900 --json
```

Full M12 feed soak runner:

```sh
scripts/run-m12-soak.sh
docker compose run --build --rm m12-soak
```

Check completed soak reports:

```sh
python3 -m videosim soak-check --report-dir reports/m12-soak --memory-growth-threshold-mb 200
```

Run GUI responsiveness soak:

```sh
python3 -m videosim gui-soak --duration-seconds 86400 --validation-interval-seconds 900 --poll-interval-seconds 30 --json
docker compose run --build --rm m12-gui-soak
```

## Launch The GUI

```sh
python3 -m videosim gui --http-port 8080 --feed-port 9000
```

Open `http://127.0.0.1:8080`.

Use the mode selector to start normal, audio-only, video-only, no-captions,
black-video, or frozen-video feeds. Use the runtime fault controls to toggle
video, audio, captions, black video, or frozen video while the GUI is running;
the MVP applies those changes with a controlled stream restart.

Use the Validate button to run the current profile validation from the GUI.
Use Download diagnostics to export status, mode, endpoint, last error,
validation output, and recent logs as text.

Video-present modes include a running clock overlay in the encoded SRT video so
receivers can visually prove live motion and timing.

When a running mode has video, the GUI opens a preview panel automatically. The
preview uses GStreamer to render a local frame matching the active mode without
attaching another receiver to the SRT listener. Use Validate to prove actual SRT
stream state.

## Deploy With Docker Compose

```sh
docker compose up --build app
```

Open `http://127.0.0.1:8080`. The app service publishes the GUI on TCP 8080 and
the SRT listener on UDP 9000. The GUI and SRT feed subprocess run inside the
`videosim-app-1` container. Override host ports with `VIDEOSIM_HTTP_PORT` and
`VIDEOSIM_FEED_PORT`.

Generated SRT listener pipelines accept receiver clients at
`srt://127.0.0.1:9000?mode=caller`. Do not add a `maxconn` URI option to the
GStreamer `srtsink` command in this Docker image; the packaged plugin does not
expose that as a supported property and it can crash the listener.

Verbose logging is enabled by default for the Compose app. Watch feed creation,
container status, subprocess PID, and the exact GStreamer pipeline:

```sh
docker compose logs -f app
```

Print the GStreamer command without starting a feed:

```sh
python3 -m videosim start --port 9000 --print-command
```

Stop a running feed with Ctrl-C.

## Planned Receiver Commands

```sh
ffplay "srt://127.0.0.1:9000?mode=caller"
ffprobe -hide_banner "srt://127.0.0.1:9000?mode=caller"
gst-launch-1.0 srtsrc uri="srt://127.0.0.1:9000?mode=caller" ! tsdemux ! fakesink
```

Receiver compatibility evidence is maintained in
[COMPATIBILITY_REPORT.md](COMPATIBILITY_REPORT.md).

## Troubleshooting

- Missing `gst-launch-1.0`: rerun `scripts/install-deps.sh` or install
  GStreamer tools manually.
- Missing `srtsink` or `srtsrc`: install the GStreamer bad plugins package for
  the platform.
- Missing `ffprobe`: install FFmpeg.
- FFmpeg reports `Protocol not found` for SRT: use GStreamer tools or install an
  FFmpeg build with SRT enabled.
