"""Structured JSON logs + request-ID middleware.

JSON output means logs ship cleanly into any log aggregator without
per-field grep wizardry. The request-ID middleware tags every log line
emitted while handling a request with the same `request_id`, so one
chat turn (HTTP + downstream LLM + DB lines) can be reconstructed from
the aggregator with a single filter.
"""

import json
import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"
_request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_request_id() -> str | None:
    return _request_id_ctx.get()


class JsonFormatter(logging.Formatter):
    """Minimal JSON formatter — no external dep."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = current_request_id()
        if rid:
            payload["request_id"] = rid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Surface any structured fields passed via extra=
        for k, v in record.__dict__.items():
            if k in payload or k.startswith("_"):
                continue
            if k in (
                "args", "msg", "levelname", "levelno", "pathname", "filename",
                "module", "exc_info", "exc_text", "stack_info", "lineno",
                "funcName", "created", "msecs", "relativeCreated", "thread",
                "threadName", "processName", "process", "name", "asctime",
                "taskName",
            ):
                continue
            try:
                json.dumps(v)
            except TypeError:
                v = repr(v)
            payload[k] = v
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(json_logs: bool) -> None:
    """Replace the root logger handlers with one configured the right way."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler()
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)-9s │ %(message)s",
            datefmt="%H:%M:%S",
        ))
    root.addHandler(handler)

    # httpx logs every request at INFO — redundant with llm.client.
    logging.getLogger("httpx").setLevel(logging.WARNING)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Set/propagate an X-Request-ID for each request.

    If the incoming request already carries the header we trust it
    (useful when a load balancer assigns one); otherwise we mint a
    short uuid4 hex. The same value is echoed back on the response and
    bound to the contextvar so every log line emitted in the request
    handler picks it up via `JsonFormatter`.
    """

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        token = _request_id_ctx.set(rid)
        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception:
            logging.getLogger("http").exception(
                "unhandled error",
                extra={
                    "path": request.url.path,
                    "method": request.method,
                },
            )
            raise
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            logging.getLogger("http").info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": duration_ms,
                },
            )
            _request_id_ctx.reset(token)
        response.headers[REQUEST_ID_HEADER] = rid
        return response
