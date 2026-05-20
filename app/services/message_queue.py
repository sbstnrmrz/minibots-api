"""Per-(sid, chat_id) message coalescer.

A user who types in bursts ("hi", "wait", "actually, can you also...")
shouldn't trigger one full workflow per Enter press. After each message
we wait `window_seconds`. Anything that arrives inside the window is
appended to the buffer and the timer resets. When the window finally
elapses, all buffered messages are joined with a blank line and
dispatched as a single chat turn.

Buffer key is `(sid, chat_id)` so two browser tabs (different sids)
never share a buffer and a chat-switch inside one tab starts a fresh
buffer.
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger("message_queue")


FlushFn = Callable[[str, str | None, int | None, str], Awaitable[None]]
"""Called with (sid, chat_id, bot_id, combined_content)."""


@dataclass
class _PendingMessage:
    content: str
    bot_id: int | None


@dataclass
class _Buffer:
    messages: list[_PendingMessage] = field(default_factory=list)
    task: asyncio.Task | None = None


class MessageCoalescer:
    def __init__(self, flush: FlushFn, window_seconds: float) -> None:
        self._flush = flush
        self._window = window_seconds
        self._buffers: dict[tuple[str, str | None], _Buffer] = defaultdict(_Buffer)
        self._locks: dict[tuple[str, str | None], asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    async def enqueue(
        self,
        sid: str,
        chat_id: str | None,
        bot_id: int | None,
        content: str,
    ) -> None:
        key = (sid, chat_id)
        async with self._locks[key]:
            buf = self._buffers[key]
            buf.messages.append(_PendingMessage(content=content, bot_id=bot_id))
            if buf.task and not buf.task.done():
                buf.task.cancel()
            buf.task = asyncio.create_task(self._wait_and_flush(sid, chat_id))

    async def _wait_and_flush(self, sid: str, chat_id: str | None) -> None:
        try:
            await asyncio.sleep(self._window)
        except asyncio.CancelledError:
            # New message arrived inside the window; the new task takes
            # over and this one exits without flushing.
            return

        key = (sid, chat_id)
        async with self._locks[key]:
            buf = self._buffers.pop(key, None)
        if not buf or not buf.messages:
            return

        combined = "\n\n".join(m.content for m in buf.messages)
        # If a client passes different bot_ids inside the same window
        # we honour the most recent one — the user's last expressed
        # intent wins.
        bot_id = buf.messages[-1].bot_id
        try:
            await self._flush(sid, chat_id, bot_id, combined)
        except Exception:
            logger.exception(
                "coalesced flush failed for sid=%s chat_id=%s", sid, chat_id
            )

    def forget(self, sid: str) -> None:
        """Drop every buffer + pending task for a disconnected sid."""
        stale_keys = [k for k in list(self._buffers.keys()) if k[0] == sid]
        for k in stale_keys:
            buf = self._buffers.pop(k, None)
            if buf and buf.task and not buf.task.done():
                buf.task.cancel()
            self._locks.pop(k, None)
