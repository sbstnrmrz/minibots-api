from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app import models
from app.schemas.bot import BotCreate, BotResponse
from app.schemas.chat import ChatMessageResponse
from app.templates import TEMPLATES

router = APIRouter(prefix="/bots", tags=["bots"])


@router.post("", response_model=BotResponse)
def create_bot(body: BotCreate, db: Session = Depends(get_db)):
    template = TEMPLATES.get(body.bot_type)
    system_prompt = body.system_prompt or (template["system_prompt"] if template else None)
    bot = models.Bot(
        name=body.name,
        bot_type=body.bot_type,
        system_prompt=system_prompt,
        spreadsheet_id=body.spreadsheet_id,
        documents_urls=body.documents_urls,
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return bot


@router.get("", response_model=list[BotResponse])
def get_bots(db: Session = Depends(get_db)):
    return db.query(models.Bot).all()


@router.get("/{bot_id}", response_model=BotResponse)
def get_bot(bot_id: int, db: Session = Depends(get_db)):
    bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot


@router.get("/{bot_id}/messages", response_model=list[ChatMessageResponse])
def get_messages(bot_id: int, db: Session = Depends(get_db)):
    return (
        db.query(models.ChatMessage)
        .filter(models.ChatMessage.bot_id == bot_id)
        .order_by(models.ChatMessage.created_at)
        .all()
    )
