# Video Feed Simulator

Video Feed Simulator will generate local SRT live video feeds for testing receivers,
monitoring systems, and outage handling. The MVP target is a GUI that can start,
stop, and validate normal and fault-mode SRT feeds.

## Current Status

Milestones 0 through 8 are complete. The CLI and local browser GUI can start,
stop, restart, and validate a
synthetic audio/video/caption SRT listener feed through GStreamer, and Docker
live tests prove a GStreamer receiver can consume H.264 video, AAC audio, and
extract CEA-608 captions from the H.264 stream.

## MVP Scope

The critical MVP must support:

- Linux runtime, with macOS development support where practical.
- Visual GUI.
- Local SRT feed generation.
- Normal feed with video, audio, and closed captions.
- Fault modes: audio only, video only, no captions, black video, frozen video.
- Copyable SRT endpoint URL.
- Start/stop controls.
- Basic logs and clear errors.
- Validation proving actual stream state.
- Install/run documentation and tests for critical behavior.

Out of scope until the critical MVP is done: protocols beyond SRT, multiple
simultaneous feeds, local preview, packet/jitter simulation, REST API, metrics,
and release packaging such as AppImage/Flatpak. Docker is present as a Linux
test harness, not a release package.

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

Validate a running feed against a profile:

```sh
python3 -m videosim validate --profile profiles/srt-normal.yaml --port 9000
python3 -m videosim validate --profile profiles/srt-normal.yaml --port 9000 --json
```

Static outage profile files also exist for `audio_only`, `video_only`,
`no_captions`, `black_video`, and `frozen_video`; Docker live validation covers
all six profiles.

Launch the local browser GUI:

```sh
python3 -m videosim gui --http-port 8080 --feed-port 9000
```

Then open `http://127.0.0.1:8080`.

The GUI mode selector supports normal, audio-only, video-only, no-captions,
black-video, and frozen-video feeds.

Receiver URL:

```text
srt://127.0.0.1:9000?mode=caller
```

Stop the feed with Ctrl-C.

## Documentation

- [CODEX_GOALS.md](CODEX_GOALS.md) - milestone plan.
- [TEST_PLAN.md](TEST_PLAN.md) - weighted test plan and requirement mapping.
- [ACCEPTANCE_MATRIX.md](ACCEPTANCE_MATRIX.md) - acceptance criteria by mode and milestone.
- [TEST_GAPS.md](TEST_GAPS.md) - missing tests and allowed gaps.
- [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) - current limitations.
- [RUNBOOK.md](RUNBOOK.md) - install, run, verify, and troubleshoot steps.
