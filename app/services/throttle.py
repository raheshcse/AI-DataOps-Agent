"""A tiny in-process sliding-window limiter for FAILED authentication attempts (brute-force damping).

Single process only, memory only: it is a speed bump, not a distributed rate limiter. It keys on the client address
and never stores credentials.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Callable


class AuthThrottle:
    def __init__(self, *, max_failures: int = 10, window_seconds: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_failures, self.window, self.clock = max_failures, window_seconds, clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str) -> deque[float]:
        q, now = self._failures[key], self.clock()
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def retry_after(self, key: str) -> int | None:
        """Seconds to wait if ``key`` is currently blocked, else None."""
        with self._lock:
            q = self._prune(key)
            if len(q) >= self.max_failures:
                return max(1, int(self.window - (self.clock() - q[0])) + 1)
            return None

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._prune(key).append(self.clock())
            if len(self._failures) > 10000:  # bound memory
                self._failures.clear()

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
