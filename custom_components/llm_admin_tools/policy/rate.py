"""Limit how often something happens, which caps a runaway loop of a client."""

from collections import deque
import time

from ..errors import invalid_input


class RateLimit:
    """Allow so many times within a window of seconds, and refuse what goes beyond."""

    def __init__(self, limit: int, window_seconds: float, refusal: str) -> None:
        """Initialize with nothing counted yet, and the key of the refusal."""
        self._limit = limit
        self._window = window_seconds
        self._refusal = refusal
        self._recent: deque[float] = deque()

    def require_capacity(self) -> None:
        """Raise if the limit is reached, and count this time otherwise."""
        now = time.monotonic()
        while self._recent and now - self._recent[0] > self._window:
            self._recent.popleft()
        if len(self._recent) >= self._limit:
            raise invalid_input(self._refusal)
        self._recent.append(now)
