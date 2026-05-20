import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.database import get_db
from app import models
from app.services.sheets import fetch_sheet

router = APIRouter(tags=["products"], dependencies=[Depends(require_api_key)])


@router.get("/products", response_model=list[str])
async def get_products(bot_id: int, db: Session = Depends(get_db)):
    bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if not bot.spreadsheet_id:
        raise HTTPException(status_code=400, detail="Bot has no spreadsheet_id")

    csv_text = await fetch_sheet(bot.spreadsheet_id)
    if not csv_text:
        raise HTTPException(status_code=502, detail="Failed to fetch sheet")

    reader = csv.DictReader(io.StringIO(csv_text))
    return [row["Nombre"] for row in reader if row.get("Nombre")]
