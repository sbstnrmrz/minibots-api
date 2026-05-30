import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app import models
from app.auth import require_api_key
from app.schemas.chat import SendMessageRequest, SendMessageResponse
from app.services.chat_handler import BotNotFound, handle_chat_turn

logger = logging.getLogger("chat_router")

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post(
    "/message",
    response_model=SendMessageResponse,
    status_code=status.HTTP_200_OK,
)
async def send_message(
    body: SendMessageRequest,
    current_tenant: models.Tenant = Depends(require_api_key),
) -> SendMessageResponse:
    try:
        reply = await handle_chat_turn(
            message=body.content,
            bot_id=body.bot_id,
            chat_id=body.chat_id,
            tenant_id=str(current_tenant.id),
        )
    except BotNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="bot not found",
        )
    except Exception:
        logger.exception(
            "chat turn failed tenant=%s chat_id=%s", current_tenant.id, body.chat_id
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while generating reply",
        )

    return SendMessageResponse(content=reply, role="agent")
