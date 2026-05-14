from fastapi import APIRouter

from app.templates import TEMPLATES

router = APIRouter(tags=["templates"])


@router.get("/templates")
def get_templates():
    return list(TEMPLATES.values())
