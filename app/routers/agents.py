import json
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app import models
from app.auth import require_api_key
from app.database import get_db
from app.config import DEFAULT_TENANT_ID, GARAGE_BUCKET
from app.services.storage import delete_file, get_client, get_presigned_url
from rag.store import clear_namespace, ingest, init_rag_table

router = APIRouter(prefix="/agents", tags=["agents"], dependencies=[Depends(require_api_key)])


class Link(BaseModel):
    label: str
    url: str


class FAQ(BaseModel):
    question: str
    answer: str


class SocialMedia(BaseModel):
    instagram: str | None = None
    facebook: str | None = None
    linkedin: str | None = None
    tiktok: str | None = None
    website: str | None = None


class GeneralInfo(BaseModel):
    description: str | None = None
    services: str | None = None
    mission: str | None = None
    vision: str | None = None
    sales_pitch: str | None = None
    faq: list[FAQ] = []
    additional_info: str | None = None
    social_media: SocialMedia | None = None


class ContactInfo(BaseModel):
    name: str | None = None
    phone: str | None = None
    company_name: str | None = None


class SetupPayload(BaseModel):
    general: GeneralInfo | None = None
    contact: ContactInfo | None = None
    links: list[Link] = []

    model_config = {"populate_by_name": True, "alias_generator": None}


@router.post("/setup")
async def setup(
    payload: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    try:
        raw = json.loads(payload)
        # normalize frontend camelCase keys
        if "general" in raw:
            g = raw["general"]
            if "sales-pitch" in g:
                g["sales_pitch"] = g.pop("sales-pitch")
            if "additional-info" in g:
                g["additional_info"] = g.pop("additional-info")
            if "socialMedia" in g:
                g["social_media"] = g.pop("socialMedia")
        data = SetupPayload.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as e:
        raise HTTPException(status_code=422, detail=str(e))

    print(f"[agents/setup] payload={data.model_dump()}, files={[f.filename for f in files]}")

    tenant_id = DEFAULT_TENANT_ID  # TODO: derive from authenticated tenant once per-tenant keys land

    tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    # contact
    if data.contact:
        if data.contact.name:
            tenant.contact_name = data.contact.name
        if data.contact.phone:
            tenant.contact_phone = data.contact.phone

    # agent_config + links
    links_data = [link.model_dump() for link in data.links]
    if tenant.agent_config_id:
        agent_config = db.query(models.AgentConfig).filter(
            models.AgentConfig.id == tenant.agent_config_id
        ).first()
        agent_config.links = links_data
    else:
        agent_config = models.AgentConfig(
            name=tenant.name,
            agent_type="generic_info",
            links=links_data,
        )
        db.add(agent_config)
        db.flush()
        tenant.agent_config_id = agent_config.id

    # general info
    if data.general:
        general = db.query(models.AgentGeneralInfo).filter(
            models.AgentGeneralInfo.agent_config_id == agent_config.id
        ).first()
        g = data.general
        info_data = dict(
            description=g.description,
            services=g.services,
            mission=g.mission,
            vision=g.vision,
            sales_pitch=g.sales_pitch,
            faq=[f.model_dump() for f in g.faq] if g.faq else None,
            social_media=g.social_media.model_dump() if g.social_media else None,
            additional_info=g.additional_info,
        )
        if general:
            for k, v in info_data.items():
                setattr(general, k, v)
        else:
            db.add(models.AgentGeneralInfo(agent_config_id=agent_config.id, **info_data))

    # delete existing files for this tenant
    existing_files = db.query(models.TenantFile).filter(
        models.TenantFile.tenant_id == tenant_id
    ).all()
    for existing in existing_files:
        ext = Path(existing.filename).suffix if existing.filename else ""
        delete_file(f"{tenant_id}/agent_docs/{existing.id}{ext}")
        db.delete(existing)

    # clear and re-ingest RAG namespace
    namespace = f"agent_{agent_config.id}"
    init_rag_table(namespace)
    clear_namespace(namespace)

    # ingest links as text
    for link in data.links:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".md", mode="w") as tmp:
            tmp.write(f"# {link.label}\n\n{link.url}\n")
            tmp_path = tmp.name
        try:
            ingest(tmp_path, namespace, source_name=link.label)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # upload files to Garage and ingest to RAG
    uploaded = []
    for file in files:
        file_id = uuid.uuid4()
        ext = Path(file.filename).suffix if file.filename else ""
        key = f"{tenant_id}/agent_docs/{file_id}{ext}"
        contents = await file.read()

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(contents)
            tmp_path = tmp.name
        try:
            get_client().put_object(
                Bucket=GARAGE_BUCKET,
                Key=key,
                Body=contents,
                ContentType=file.content_type or "application/octet-stream",
            )
            ingest(tmp_path, namespace, source_name=file.filename)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        url = get_presigned_url(key)
        db.add(models.TenantFile(
            id=file_id,
            tenant_id=tenant_id,
            agent_config_id=agent_config.id,
            filename=file.filename,
            content_type=file.content_type or "application/octet-stream",
            status=models.TenantFileStatus.ingested,
        ))
        uploaded.append({"filename": file.filename, "key": key, "url": url})

    db.commit()

    return {
        "links": links_data,
        "files": uploaded,
    }
