from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.database import get_db
from app import models
from app.schemas.bot import BotCreate, BotResponse
from app.schemas.chat import ChatMessageResponse

router = APIRouter(prefix="/bots", tags=["bots"])


@router.post("", response_model=BotResponse)
def create_bot(
    body: BotCreate,
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    bot = models.Bot(
        tenant_id=current_tenant.id,
        name=body.name,
        bot_type=body.bot_type,
        spreadsheet_id=body.spreadsheet_id,
        workflow_id=body.workflow_id,
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return bot


@router.get("", response_model=list[BotResponse])
def get_bots(
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Bot)
        .filter(models.Bot.tenant_id == current_tenant.id)
        .all()
    )


@router.get("/{bot_id}", response_model=BotResponse)
def get_bot(
    bot_id: int,
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    bot = (
        db.query(models.Bot)
        .filter(models.Bot.id == bot_id, models.Bot.tenant_id == current_tenant.id)
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot


@router.get("/{bot_id}/messages", response_model=list[ChatMessageResponse])
def get_messages(
    bot_id: int,
    current_tenant: models.Tenant = Depends(require_api_key),
    db: Session = Depends(get_db),
):
    bot = (
        db.query(models.Bot)
        .filter(models.Bot.id == bot_id, models.Bot.tenant_id == current_tenant.id)
        .first()
    )
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    return (
        db.query(models.ChatMessage)
        .filter(models.ChatMessage.bot_id == bot_id)
        .order_by(models.ChatMessage.created_at)
        .all()
    )
