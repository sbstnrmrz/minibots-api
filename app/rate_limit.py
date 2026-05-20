"""HTTP and socket rate limiting.

`limiter` is the slowapi Limiter used by FastAPI routes via the
`@limiter.limit("...")` decorator. Mounted into the app in main.py.

`SocketRateLimiter` is a tiny in-memory token-bucket per socket sid for
the `send_message` event — slowapi doesn't speak socket.io, so we run
our own check there. Both fail open if rate-limit storage is unset; the
backstop is the per-event check.
"""

import logging
import time
from collections import defaultdict, deque

from slowapi import Limiter
from slowapi.util import get_remote_address

logger = logging.getLogger("ratelimit")

# Default budget for HTTP routes. Per-route overrides via @limiter.limit.
DEFAULT_HTTP_LIMIT = "60/minute"

# Socket budget: events per window per sid. The default is generous for
# a human typing chat messages but stops loop-driven floods.
SOCKET_LIMIT_EVENTS = 30
SOCKET_LIMIT_WINDOW_SECONDS = 60


limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[DEFAULT_HTTP_LIMIT],
    headers_enabled=True,
)


class SocketRateLimiter:
    """In-memory sliding-window limiter keyed by socket sid."""

    def __init__(
        self,
        max_events: int = SOCKET_LIMIT_EVENTS,
        window_seconds: int = SOCKET_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self._max = max_events
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, sid: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        bucket = self._hits[sid]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True

    def forget(self, sid: str) -> None:
        self._hits.pop(sid, None)


socket_limiter = SocketRateLimiter()
