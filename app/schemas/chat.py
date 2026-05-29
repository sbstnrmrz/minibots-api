from datetime import datetime
from pydantic import BaseModel


class ChatMessageResponse(BaseModel):
    id: int
    bot_id: int
    role: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class SendMessageRequest(BaseModel):
    content: str
    role: str = "user"
    chat_id: str | None = None
    bot_id: int | None = None


class SendMessageResponse(BaseModel):
    content: str
    role: str
