# Receiver Compatibility Report

Milestone 11 compatibility is tested in Docker with
`docker compose run --build --rm live-srt`.

## Tested Receivers

| Receiver | Coverage | Evidence |
|---|---|---|
| GStreamer receiver | Consumes all required modes through `srtsrc`, `tsdemux`, `h264parse`, and/or `aacparse`. | `tests.test_live_srt.LiveSrtTest.test_receiver_compatibility_required_modes` |
| ffprobe | Detects expected video/audio stream presence for all required modes. | `tests.test_live_srt.LiveSrtTest.test_receiver_compatibility_required_modes` |
| ffplay | Opens and consumes all required modes in headless Docker using dummy SDL audio/video drivers. | `tests.test_live_srt.LiveSrtTest.test_receiver_compatibility_required_modes` |

## Required Modes

| Mode | GStreamer | ffprobe | ffplay |
|---|---|---|---|
| normal | pass | pass | pass |
| audio_only | pass | pass | pass |
| video_only | pass | pass | pass |
| no_captions | pass | pass | pass |
| black_video | pass | pass | pass |
| frozen_video | pass | pass | pass |

## Limits

- ffprobe is used for video/audio stream detection, not CEA-608 extraction or
  black/frozen visual analysis.
- ffplay is used as a receiver consumption smoke test, not a semantic validator.
- Caption presence, caption absence, black video, and frozen video remain proven
  by `python3 -m videosim validate`.
- VLC is not tested in the minimal Docker harness; add it when a GUI-capable or
  agreed headless VLC target is available.
- No project-specific target receiver has been defined.
