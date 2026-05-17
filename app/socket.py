import socketio
import logging
from pydantic import BaseModel, ValidationError

logger = logging.getLogger("uvicorn")

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=[])
socket_app = socketio.ASGIApp(sio)


class Message(BaseModel):
    content: str
    role: str = "user"


@sio.event
async def connect(sid, environ, auth):
    logger.info(f"Socket client {sid} connected")


@sio.event
async def disconnect(sid, reason):
    logger.info(f"Socket client {sid} disconnected")


@sio.event
async def send_message(sid, data):
    try:
        payload = Message(**data)
    except ValidationError as e:
        await sio.emit("error", {"detail": str(e)}, to=sid)
        return

    logger.info(f"Message received: {payload.content}")
    await sio.emit("new_message", {"content": payload.content, "role": payload.role}, to=sid)
