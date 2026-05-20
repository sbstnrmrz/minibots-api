import logging

import socketio
from pydantic import BaseModel, ValidationError

from app.auth import validate_api_token
from app.config import (
    ALLOWED_ORIGINS,
    CHAT_COALESCE_WINDOW_SECONDS,
    DEFAULT_TENANT_ID as _ENV_DEFAULT_TENANT_ID,
)
from app.rate_limit import socket_limiter
from app.services.chat_handler import handle_chat_turn
from app.services.message_queue import MessageCoalescer

DEFAULT_TENANT_ID = _ENV_DEFAULT_TENANT_ID

logger = logging.getLogger("uvicorn")

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=ALLOWED_ORIGINS,
)
socket_app = socketio.ASGIApp(sio)


class Message(BaseModel):
    content: str
    role: str = "user"
    bot_id: int | None = None
    chat_id: str | None = None


async def _run_turn(
    sid: str,
    chat_id: str | None,
    bot_id: int | None,
    combined_content: str,
) -> None:
    """Buffer-flush callback: run one chat turn and emit the reply."""
    try:
        reply = await handle_chat_turn(
            message=combined_content,
            bot_id=bot_id,
            chat_id=chat_id,
            # TODO: derive tenant_id from authenticated socket session
            tenant_id=DEFAULT_TENANT_ID,
        )
    except Exception as e:
        logger.exception("chat turn failed for sid=%s: %s", sid, e)
        await sio.emit(
            "error",
            {"detail": "internal error while generating reply"},
            to=sid,
        )
        return
    await sio.emit("new_message", {"content": reply, "role": "agent"}, to=sid)


coalescer = MessageCoalescer(
    flush=_run_turn,
    window_seconds=CHAT_COALESCE_WINDOW_SECONDS,
)


@sio.event
async def connect(sid, environ, auth):
    token = None
    if isinstance(auth, dict):
        token = auth.get("token") or auth.get("api_key")
    if not validate_api_token(token):
        logger.warning("socket %s rejected: invalid api token", sid)
        raise socketio.exceptions.ConnectionRefusedError("unauthorized")
    logger.info(f"Socket client {sid} connected")


@sio.event
async def disconnect(sid, reason):
    coalescer.forget(sid)
    socket_limiter.forget(sid)
    logger.info(f"Socket client {sid} disconnected")


@sio.event
async def send_message(sid, data):
    if not socket_limiter.allow(sid):
        await sio.emit(
            "error",
            {"detail": "rate limit exceeded; slow down"},
            to=sid,
        )
        return
    try:
        payload = Message(**data)
    except ValidationError:
        await sio.emit("error", {"detail": "invalid payload"}, to=sid)
        return

    await coalescer.enqueue(
        sid=sid,
        chat_id=payload.chat_id,
        bot_id=payload.bot_id,
        content=payload.content,
    )
