"""Rate limiting and safe error reporting for a publicly reachable demo.

Scope, stated honestly: this is an in-process sliding window. It is sized to
stop one browser hammering Run, not to survive a distributed attack, and it
does not share state across worker processes. For a single-container demo that
is the right amount of machinery; anything stronger belongs in front of the app,
not inside it.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from . import config

__all__ = ["RateLimiter", "runs", "SafeError"]


class SafeError(Exception):
    """An error whose message is safe to show a browser.

    Everything else is reported as a generic failure with a run id, so internal
    detail (schema, driver messages, file paths) never reaches a public page.
    """

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class RateLimiter:
    """Fixed-capacity sliding window, keyed by opaque session id."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        now = time.monotonic()
        window = self._hits[key]
        cutoff = now - self._window
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= self._limit:
            return False, max(0.0, self._window - (now - window[0]))

        window.append(now)
        self._prune(now)
        return True, 0.0

    def _prune(self, now: float) -> None:
        """Drop sessions that have gone quiet.

        Without this the dict is an unbounded map keyed by attacker-suppliable
        cookie values -- a slow memory leak on a public URL.
        """
        if len(self._hits) < 4096:
            return
        cutoff = now - self._window
        for key in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
            del self._hits[key]

    def reset(self) -> None:
        self._hits.clear()


runs = RateLimiter(config.RATE_LIMIT_RUNS, config.RATE_LIMIT_WINDOW_SECONDS)
