# KNOWN_LIMITATIONS.md

# Known Limitations

- No CLI, GUI, SRT pipeline, profile loader, or validator is implemented yet.
- Docker packaging is preferred for portability but is not part of the critical
  MVP gate in AGENTS.md; revisit after core feed generation and validation work.
- macOS is treated as a development target. Linux remains the required runtime
  target.
- Caption behavior must be proven as a real stream, not burned-in text, before
  Milestone 3 can close.
- Audio-only caption behavior is explicitly allowed to be absent/unsupported and
  must be reported clearly by validation.
- Receiver compatibility is unproven until Milestone 11.
