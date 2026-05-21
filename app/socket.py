import logging

import socketio
from pydantic import BaseModel, ValidationError

from app.auth import get_tenant_by_token, validate_api_token
from app.config import (
    CHAT_COALESCE_WINDOW_SECONDS,
    DEFAULT_TENANT_ID,
    ENVIRONMENT,
)
from app.rate_limit import socket_limiter
from app.services.chat_handler import handle_chat_turn
from app.services.message_queue import MessageCoalescer

logger = logging.getLogger("uvicorn")

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=[],
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
    session = await sio.get_session(sid)
    tenant_id = session.get("tenant_id", DEFAULT_TENANT_ID)

    try:
        reply = await handle_chat_turn(
            message=combined_content,
            bot_id=bot_id,
            chat_id=chat_id,
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


coalescer = MessageCoalescer(
    flush=_run_turn,
    window_seconds=CHAT_COALESCE_WINDOW_SECONDS,
)


@sio.event
async def connect(sid, environ, auth):
    token = None
    if isinstance(auth, dict):
        token = auth.get("token") or auth.get("api_key")

    # Resolve tenant_id from per-tenant DB token
    tenant_id: str | None = None
    if token:
        from app.database import db_context
        with db_context() as db:
            tenant = get_tenant_by_token(token, db)
            if tenant:
                tenant_id = str(tenant.id)

    # Dev fallback: env-level token → DEFAULT_TENANT_ID
    if not tenant_id and validate_api_token(token):
        tenant_id = DEFAULT_TENANT_ID

    if not tenant_id:
        logger.warning("socket %s rejected: no tenant matched token", sid)
        raise socketio.exceptions.ConnectionRefusedError("unauthorized")

    await sio.save_session(sid, {"tenant_id": tenant_id})
    logger.info("socket %s connected tenant_id=%s", sid, tenant_id)


@sio.event
async def disconnect(sid, reason):
    coalescer.forget(sid)
    socket_limiter.forget(sid)
    logger.info("socket %s disconnected", sid)


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
