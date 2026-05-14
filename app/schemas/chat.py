from datetime import datetime
from pydantic import BaseModel


class ChatMessageResponse(BaseModel):
    id: int
    bot_id: int
    role: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}
