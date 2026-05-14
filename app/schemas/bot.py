from pydantic import BaseModel


class BotCreate(BaseModel):
    name: str
    bot_type: str = "zen_coach"
    system_prompt: str | None = None
    spreadsheet_id: str | None = None
    documents_urls: list[str] | None = None


class BotResponse(BaseModel):
    id: int
    name: str
    bot_type: str
    system_prompt: str | None
    spreadsheet_id: str | None
    documents_urls: list[str] | None

    model_config = {"from_attributes": True}
