"""Chat history fetch.

A reloaded browser tab needs to recover the conversation it was in;
the frontend keeps `chat_id` in memory only, so without a fetch the
history is lost. This endpoint replays the persisted chat_messages for
a given chat_id so the UI can rehydrate.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.auth import require_api_key
from app.database import get_db

router = APIRouter(prefix="/chats", tags=["chats"])

_ROLE_OUT = {"user": "user", "model": "agent", "agent": "agent"}


@router.get("")
def list_chats(
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """Return chat sessions for the current tenant, most-recent first.

    Scoped by Chat.tenant_id directly (not a JOIN through Bot) so that
    tenant-default chats — which have bot_id NULL and would be dropped by an
    inner join — are included.

    Message counts and last-message previews are computed with grouped
    aggregate subqueries instead of a query per chat (was N+1).
    """
    chats = (
        db.query(models.Chat)
        .filter(models.Chat.tenant_id == current_tenant.id)
        .order_by(models.Chat.created_at.desc())
        .all()
    )
    if not chats:
        return []

    chat_ids = [c.id for c in chats]

    # One grouped query for counts.
    count_rows = (
        db.query(
            models.ChatMessage.chat_id,
            func.count(models.ChatMessage.id),
        )
        .filter(models.ChatMessage.chat_id.in_(chat_ids))
        .group_by(models.ChatMessage.chat_id)
        .all()
    )
    counts = {chat_id: n for chat_id, n in count_rows}

    # One query for the latest message per chat via a window over created_at.
    latest_rows = (
        db.query(
            models.ChatMessage.chat_id,
            models.ChatMessage.content,
            func.row_number()
            .over(
                partition_by=models.ChatMessage.chat_id,
                order_by=models.ChatMessage.created_at.desc(),
            )
            .label("rn"),
        )
        .filter(models.ChatMessage.chat_id.in_(chat_ids))
        .subquery()
    )
    last_rows = (
        db.query(latest_rows.c.chat_id, latest_rows.c.content)
        .filter(latest_rows.c.rn == 1)
        .all()
    )
    last_message = {chat_id: content for chat_id, content in last_rows}

    return [
        {
            "chat_id": chat.id,
            "bot_id": chat.bot_id,
            "created_at": chat.created_at.isoformat() if chat.created_at else None,
            "message_count": counts.get(chat.id, 0),
            "last_message": (last_message.get(chat.id) or "")[:100] or None,
        }
        for chat in chats
    ]


@router.get("/{chat_id}/messages")
def get_chat_messages(
    chat_id: str,
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    """Return all persisted messages for a chat in chronological order."""
    # Scope by Chat.tenant_id directly so tenant-default chats (bot_id NULL)
    # are reachable — an inner join on Bot would silently drop them.
    chat = (
        db.query(models.Chat)
        .filter(
            models.Chat.id == chat_id,
            models.Chat.tenant_id == current_tenant.id,
        )
        .first()
    )
    if not chat:
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
