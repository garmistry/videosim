# KNOWN_LIMITATIONS.md

# Known Limitations

- The CLI can start, stop, and restart an audio/video/caption SRT listener feed.
  GUI, fault modes, and standalone validator are not implemented yet.
- Profile parsing supports only the flat YAML shape used by the current normal
  and outage profiles.
- The Milestone 2 30-minute audio continuity P1 test is not implemented.
- Captions start after a fixed 3-second delay so live SRT video caps negotiate
  before caption bytes reach `cccombiner`.
- The Milestone 3 caption update-over-time P1 test is not implemented.
- The Dockerfile is a Linux test harness, not release packaging.
- Docker packaging is preferred for portability but is not part of the critical
  MVP gate in AGENTS.md; revisit after core feed generation and validation work.
- macOS is treated as a development target. Linux remains the required runtime
  target.
- Audio-only caption behavior is explicitly allowed to be absent/unsupported and
  must be reported clearly by validation.
- Receiver compatibility is unproven until Milestone 11.
