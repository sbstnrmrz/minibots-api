from pydantic import BaseModel


class BotCreate(BaseModel):
    name: str
    bot_type: str = "rag_info"
    spreadsheet_id: str | None = None
    workflow_id: int | None = None


class BotResponse(BaseModel):
    id: int
    name: str
    bot_type: str
    spreadsheet_id: str | None
    workflow_id: int | None

    model_config = {"from_attributes": True}
