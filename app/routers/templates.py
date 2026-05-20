from fastapi import APIRouter, Depends

from app.auth import require_api_key
from app.templates import TEMPLATES

router = APIRouter(tags=["templates"], dependencies=[Depends(require_api_key)])


@router.get("/templates")
def get_templates():
    return list(TEMPLATES.values())
