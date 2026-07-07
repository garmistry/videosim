from __future__ import annotations


DEFAULT_ALERT_DELAY_SECONDS = 0


def normalize_alert_delay(value) -> int:
    if value in (None, ""):
        return DEFAULT_ALERT_DELAY_SECONDS
    try:
        delay = int(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("alert delay must be a number of seconds") from exc
    if delay < 0:
        raise ValueError("alert delay must be zero or greater")
    return delay


def normalize_enabled_alerts(values) -> list[str] | None:
    if values is None:
        return None
    seen = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def alert_profile_payload(enabled_ids: list[str] | None, delay_seconds: int) -> dict:
    return {
        "allEnabled": enabled_ids is None,
        "enabledMonitorIds": enabled_ids,
        "delaySeconds": normalize_alert_delay(delay_seconds),
    }


def alert_profile_from_stream(stream: dict) -> dict:
    profile = stream.get("alertProfile") or {}
    return alert_profile_payload(
        normalize_enabled_alerts(profile.get("enabledMonitorIds")),
        profile.get("delaySeconds"),
    )
