"""Chat history fetch.

A reloaded browser tab needs to recover the conversation it was in;
the frontend keeps `chat_id` in memory only, so without a fetch the
history is lost. This endpoint replays the persisted chat_messages for
a given chat_id so the UI can rehydrate.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import models
from app.auth import require_api_key
from app.database import get_db

router = APIRouter(prefix="/chats", tags=["chats"], dependencies=[Depends(require_api_key)])


# Map persisted ChatMessage.role ("user" / "model") to the role names
# the socket emits and the frontend expects ("user" / "agent").
_ROLE_OUT = {"user": "user", "model": "agent", "agent": "agent"}


@router.get("/{chat_id}/messages")
def get_chat_messages(chat_id: str, db: Session = Depends(get_db)):
    """Return all persisted messages for a chat in chronological order."""
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    if not chat:
        # No row yet — empty conversation, not a 404. The frontend can
        # mount on a fresh chat_id and call this immediately.
        return {"chat_id": chat_id, "messages": []}

    rows = (
        db.query(models.ChatMessage)
        .filter(models.ChatMessage.chat_id == chat_id)
        .order_by(models.ChatMessage.created_at)
        .all()
    )
    return {
        "chat_id": chat_id,
        "bot_id": chat.bot_id,
        "messages": [
            {
                "role": _ROLE_OUT.get(m.role, m.role),
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in rows
        ],
    }
