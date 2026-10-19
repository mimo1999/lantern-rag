import time
from collections import defaultdict, deque
from functools import lru_cache


class RateLimiter:
    """Sliding-window limiter keyed by tenant id.

    ponytail: in-process, so with N workers a tenant gets N times the limit; move the window
    into Redis or Postgres if the API runs more than one worker.
    """

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)

    def retry_after(self, key: str) -> int | None:
        """Record a request; return seconds to wait if the key is over its limit, else None."""
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return max(1, int(self._window - (now - hits[0])) + 1)
        hits.append(now)
        return None


@lru_cache(maxsize=4)
def get_rate_limiter(limit: int) -> RateLimiter:
    return RateLimiter(limit)
