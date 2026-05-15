import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from rag.store import init_rag_table, ingest

router = APIRouter(tags=["documents"])


def _bot_namespace(bot_id: int) -> str:
    return f"bot_{bot_id}"


@router.post("/bots/{bot_id}/documents")
async def upload_document(
    bot_id: int,
    file: UploadFile,
    db: Session = Depends(get_db),
):
    bot = db.query(models.Bot).filter(models.Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail=f"Bot {bot_id} not found.")

    suffix = Path(file.filename).suffix if file.filename else ""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        namespace = _bot_namespace(bot_id)
        init_rag_table(namespace)
        chunks = ingest(tmp_path, namespace, source_name=file.filename)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return {"bot_id": bot_id, "filename": file.filename, "chunks_ingested": chunks}
