from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


_PROBE_DEADLINE: ContextVar[float | None] = ContextVar(
    "videosim_probe_deadline",
    default=None,
)


@contextmanager
def use_probe_deadline(deadline: float | None) -> Iterator[None]:
    token = _PROBE_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _PROBE_DEADLINE.reset(token)


def probe_timeout(max_seconds: float) -> float:
    deadline = _PROBE_DEADLINE.get()
    if deadline is None:
        return max_seconds
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("stream probe budget exhausted")
    return min(max_seconds, remaining)
