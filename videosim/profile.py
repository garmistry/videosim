from __future__ import annotations

from pathlib import Path

from .feed import VideoFeedConfig


class ProfileError(Exception):
    pass


REQUIRED_KEYS = {"schema_version", "mode"}
ALLOWED_KEYS = {
    "schema_version",
    "mode",
    "protocol",
    "port",
    "width",
    "height",
    "framerate",
    "pattern",
    "video",
    "audio",
    "audio_frequency",
    "captions",
    "frozen",
    "dash_dir",
    "dash_base_url",
    "dash_manifest",
}
INT_KEYS = {"schema_version", "port", "width", "height", "framerate", "audio_frequency"}
BOOL_KEYS = {"video", "audio", "captions", "frozen"}
MODE_PRESETS = {
    "normal": {},
    "audio_only": {"video": False, "audio": True, "captions": False},
    "video_only": {"video": True, "audio": False, "captions": True},
    "no_captions": {"video": True, "audio": True, "captions": False},
    "black_video": {"video": True, "audio": True, "captions": True, "pattern": "black"},
    "frozen_video": {"video": True, "audio": True, "captions": True, "frozen": True},
}


def load_profile(path: str | Path) -> VideoFeedConfig:
    values = _parse_flat_yaml(Path(path))
    missing = REQUIRED_KEYS - values.keys()
    if missing:
        raise ProfileError(f"Missing required profile field: {', '.join(sorted(missing))}")
    if values["schema_version"] != 1:
        raise ProfileError("Unsupported profile schema_version")
    if values["mode"] not in MODE_PRESETS:
        raise ProfileError(f"Unsupported profile mode: {values['mode']}")

    config_values = MODE_PRESETS[values["mode"]] | {
        key: value for key, value in values.items() if key not in {"schema_version", "mode"}
    }
    try:
        return VideoFeedConfig(**config_values)
    except ValueError as exc:
        raise ProfileError(str(exc)) from exc


def _parse_flat_yaml(path: Path) -> dict[str, object]:
    values = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            raise ProfileError(f"Invalid profile line {line_number}: expected key: value")
        key, raw_value = [part.strip() for part in line.split(":", 1)]
        if key not in ALLOWED_KEYS:
            raise ProfileError(f"Unknown profile field: {key}")
        if raw_value == "":
            raise ProfileError(f"Missing value for profile field: {key}")
        values[key] = _coerce_value(key, raw_value)
    return values


def _coerce_value(key: str, raw_value: str):
    if key in INT_KEYS:
        try:
            return int(raw_value)
        except ValueError as exc:
            raise ProfileError(f"{key} must be an integer") from exc
    if key in BOOL_KEYS:
        if raw_value.lower() == "true":
            return True
        if raw_value.lower() == "false":
            return False
        raise ProfileError(f"{key} must be true or false")
    return raw_value
