from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.sql import func
from app.database import Base

class Bot(Base):
    __tablename__ = "bots"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    bot_type = Column(String, nullable=False, server_default="zen_coach")
    system_prompt = Column(String, nullable=True)
    spreadsheet_id = Column(String, nullable=True)
    documents_urls = Column(ARRAY(String), nullable=True)

class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    bot_id = Column(Integer, ForeignKey("bots.id"), nullable=False)
    role = Column(String, nullable=False)  # "user" or "model"
    content = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
