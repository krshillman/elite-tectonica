"""
edsm_backoff.py — Shared rate-limit backoff logic for EDSM fetchers.

When EDSM signals we are over budget we back off in escalating steps —
5 min, then 10 min, then 15 min… capped at 60 min — and retry the same
request. Any successful request resets the backoff back to zero.

EDSM uses a leaky-bucket limiter (~360 requests/hour). Being rate limited
manifests two ways:
  * HTTP 429 Too Many Requests (current behaviour), or
  * an HTTP 200 with a completely EMPTY body (historic behaviour).
Fetchers raise ``RateLimitError`` for the empty-body case so both paths
funnel through ``is_rate_limited``.

The wait is performed in short slices so callers running inside a worker
thread can still be cancelled via their ``progress`` callback (returning
False from the callback aborts the wait, and hence the fetch).
"""

from __future__ import annotations

import time
import urllib.error
from typing import Callable, Optional

BACKOFF_STEP_S = 300      # escalate in 5-minute increments
BACKOFF_MAX_S = 3600      # never wait longer than 60 minutes
_SLICE_S = 1.0            # sleep granularity so cancellation stays snappy


class RateLimitError(Exception):
    """Raised when EDSM signals rate limiting without an HTTP 429
    (historically an empty 200 response body)."""


def is_rate_limited(exc: Exception) -> bool:
    """True if the exception represents an EDSM rate limit (429 or empty body)."""
    if isinstance(exc, RateLimitError):
        return True
    return isinstance(exc, urllib.error.HTTPError) and exc.code == 429


def backoff_duration_s(level: int) -> int:
    """Wait time in seconds for a given backoff level (1-based)."""
    return min(level * BACKOFF_STEP_S, BACKOFF_MAX_S)


def backoff_wait(
    level: int,
    tag: str,
    progress: Optional[Callable[[int, int], bool]] = None,
    done: int = 0,
    total: int = 0,
) -> bool:
    """
    Sleep for the backoff duration of ``level`` (5 min × level, capped).

    ``progress(done, total)`` is pinged every slice so the UI stays
    responsive; if it returns False the wait aborts immediately.

    Returns True if the full wait completed, False if cancelled.
    """
    wait_s = backoff_duration_s(level)
    print(
        f"[{tag}] rate limited — backing off "
        f"{wait_s // 60} min (attempt {level})"
    )
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(min(_SLICE_S, max(0.0, deadline - time.monotonic())))
        if progress is not None and progress(done, total) is False:
            print(f"[{tag}] backoff cancelled by user")
            return False
    return True
