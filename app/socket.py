import logging

import socketio
from pydantic import BaseModel, ValidationError

from app.auth import validate_api_token
from app.config import ALLOWED_ORIGINS, DEFAULT_TENANT_ID as _ENV_DEFAULT_TENANT_ID
from app.rate_limit import socket_limiter
from app.services.chat_handler import handle_chat_turn

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

    # TODO: derive tenant_id from authenticated socket session
    tenant_id = DEFAULT_TENANT_ID

    try:
        reply = await handle_chat_turn(
            message=payload.content,
            bot_id=payload.bot_id,
            chat_id=payload.chat_id,
            tenant_id=tenant_id,
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
