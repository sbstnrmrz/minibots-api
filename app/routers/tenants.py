from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import require_service_token
from app.database import get_db
from app import models

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantCreate(BaseModel):
    id: str
    name: str
    slug: str
    contact_name: str | None = None
    contact_phone: str | None = None


class TenantResponse(BaseModel):
    id: str
    name: str
    slug: str
    contact_name: str | None
    contact_phone: str | None

    class Config:
        from_attributes = True


@router.post(
    "",
    response_model=TenantResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_service_token)],
)
def create_tenant(body: TenantCreate, db: Session = Depends(get_db)):
    if db.query(models.Tenant).filter(models.Tenant.id == body.id).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="tenant already exists",
        )
    if db.query(models.Tenant).filter(models.Tenant.slug == body.slug).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="slug already taken",
        )
    tenant = models.Tenant(
        id=body.id,
        name=body.name,
        slug=body.slug,
        contact_name=body.contact_name,
        contact_phone=body.contact_phone,
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant
