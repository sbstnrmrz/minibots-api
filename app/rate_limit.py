"""HTTP rate limiting.

`limiter` is the slowapi Limiter used by FastAPI routes (default
60/minute by remote address) and mounted into the app in main.py.

The socket.io path — and its in-memory per-sid SocketRateLimiter — was
removed when chat moved to HTTP POST /chat/message. HTTP routes are
covered by slowapi alone.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

# Default budget for HTTP routes. Per-route overrides via @limiter.limit.
DEFAULT_HTTP_LIMIT = "60/minute"


limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[DEFAULT_HTTP_LIMIT],
    headers_enabled=True,
)
