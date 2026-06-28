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

## Troubleshooting

- Missing `gst-launch-1.0`: rerun `scripts/install-deps.sh` or install
  GStreamer tools manually.
- Missing `srtsink` or `srtsrc`: install the GStreamer bad plugins package for
  the platform.
- Missing `ffprobe`: install FFmpeg.
- FFmpeg reports `Protocol not found` for SRT: use GStreamer tools or install an
  FFmpeg build with SRT enabled.
